"""本地代理认证转发器。

Chromium 系内核的 --proxy-server 命令行不支持账号密码（Chromium 限制）。
本模块在本地起一个无认证的 HTTP 代理（仅绑定 127.0.0.1 随机端口），
把浏览器流量转发到真实上游代理并在上游侧注入认证，从而让
fp-chromium / chromium 内核也能使用带账密的代理。

支持上游类型：http/https（Basic 认证）、socks5（用户名密码认证）、socks4（userid）。
流量处理：CONNECT 隧道（全部 HTTPS 流量）做纯字节双向泵；
明文 HTTP 绝对 URI 请求经 httpx 转发（兼容所有上游类型）。
"""
import asyncio
import base64
import logging
from typing import Optional

import httpx

from .models import ProxyConfig

log = logging.getLogger(__name__)

_READ_CHUNK = 65536
_MAX_HTTP_BODY = 64 << 20


class AuthProxyForwarder:
    """每个浏览器实例一个转发器实例；用完必须 stop()。"""

    def __init__(self, upstream: ProxyConfig):
        self.upstream = upstream
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    @property
    def local_proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "AuthProxyForwarder":
        self._server = await asyncio.start_server(
            self._handle_client, host="127.0.0.1", port=0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server = None
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    # ---------------------------------------------------------------- 客户端侧

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        tunnel: Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = request_line.split(" ")
            method, target = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            if method.upper() == "CONNECT":
                try:
                    tunnel = await self._open_upstream_tunnel(target)
                except Exception as e:
                    log.warning("上游隧道建立失败 %s: %s", target, e)
                    try:
                        writer.write(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n")
                        await writer.drain()
                    except Exception:
                        pass
                    return
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await self._pump(reader, writer, tunnel[0], tunnel[1])
            elif method:
                await self._relay_plain_http(reader, writer, head, method, target)
            else:
                writer.close()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("代理转发连接处理异常", exc_info=True)
        finally:
            for w in (writer, tunnel[1] if tunnel else None):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if task:
                self._tasks.discard(task)

    def _upstream_auth_header(self) -> Optional[str]:
        if self.upstream.username and self.upstream.password is not None:
            token = base64.b64encode(
                f"{self.upstream.username}:{self.upstream.password}".encode()
            ).decode()
            return f"Basic {token}"
        return None

    # ---------------------------------------------------------------- 上游隧道

    async def _open_upstream_tunnel(
        self, target: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """连上上游并打通到 target 的隧道，返回上游 (reader, writer)。"""
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        port = int(port_s) if port_s.isdigit() else 443
        ur, uw = await asyncio.open_connection(self.upstream.host, self.upstream.port)
        scheme = self.upstream.scheme
        try:
            if scheme in ("http", "https"):
                req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                auth = self._upstream_auth_header()
                if auth:
                    req += f"Proxy-Authorization: {auth}\r\n"
                req += "\r\n"
                uw.write(req.encode())
                await uw.drain()
                resp = await ur.readuntil(b"\r\n\r\n")
                status = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                if " 200 " not in status:
                    raise RuntimeError(f"上游代理拒绝 CONNECT: {status.strip()}")
            elif scheme == "socks5":
                await self._socks5_handshake(ur, uw)
                await self._socks5_connect(ur, uw, host, port)
            elif scheme == "socks4":
                await self._socks4_connect(ur, uw, host, port)
            else:
                raise RuntimeError(f"不支持的上游代理类型: {scheme}")
            return ur, uw
        except Exception:
            try:
                uw.close()
            except Exception:
                pass
            raise

    async def _pump(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter,
                    ur: asyncio.StreamReader, uw: asyncio.StreamWriter) -> None:
        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(_READ_CHUNK)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            asyncio.create_task(pipe(cr, uw)),
            asyncio.create_task(pipe(ur, cw)),
        )

    # ---------------------------------------------------------------- SOCKS 上游

    async def _socks5_handshake(self, ur: asyncio.StreamReader,
                                uw: asyncio.StreamWriter) -> None:
        uw.write(b"\x05\x02\x00\x02")  # 支持无认证 + 用户名密码
        await uw.drain()
        resp = await ur.readexactly(2)
        if resp[0] != 0x05:
            raise RuntimeError("上游不是 SOCKS5 代理")
        if resp[1] == 0x02:
            user = (self.upstream.username or "").encode()
            pwd = (self.upstream.password or "").encode()
            uw.write(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
            await uw.drain()
            auth = await ur.readexactly(2)
            if auth[1] != 0x00:
                raise RuntimeError("SOCKS5 代理认证失败（检查用户名/密码）")
        elif resp[1] != 0x00:
            raise RuntimeError("SOCKS5 代理要求不支持的认证方式")

    async def _socks5_connect(self, ur: asyncio.StreamReader, uw: asyncio.StreamWriter,
                              host: str, port: int) -> None:
        host_b = host.encode()
        uw.write(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b +
                 port.to_bytes(2, "big"))
        await uw.drain()
        head = await ur.readexactly(4)
        if head[1] != 0x00:
            raise RuntimeError(f"SOCKS5 CONNECT 失败（code={head[1]}）")
        atyp = head[3]
        if atyp == 0x01:
            await ur.readexactly(4 + 2)
        elif atyp == 0x03:
            ln = (await ur.readexactly(1))[0]
            await ur.readexactly(ln + 2)
        elif atyp == 0x04:
            await ur.readexactly(16 + 2)

    async def _socks4_connect(self, ur: asyncio.StreamReader, uw: asyncio.StreamWriter,
                              host: str, port: int) -> None:
        import socket
        import struct

        try:
            ip = socket.inet_aton(host)
            req = struct.pack(">BBH", 4, 1, port) + ip
            tail = b""
        except OSError:  # 域名走 SOCKS4a：IP 段置无效 + 域名随请求携带
            req = struct.pack(">BBH", 4, 1, port) + b"\x00\x00\x00\xff"
            tail = host.encode() + b"\x00"
        userid = (self.upstream.username or "").encode()
        uw.write(req + userid + b"\x00" + tail)
        await uw.drain()
        resp = await ur.readexactly(8)
        if resp[1] != 0x5A:
            raise RuntimeError(f"SOCKS4 CONNECT 失败（code={resp[1]}）")

    # ---------------------------------------------------------------- 明文 HTTP（绝对 URI）

    async def _relay_plain_http(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter,
                                head: bytes, method: str, target: str) -> None:
        headers_blob = head.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        header_lines = headers_blob.split("\r\n")[1:]
        req_headers = {}
        for line in header_lines:
            if ":" in line:
                k, v = line.split(":", 1)
                req_headers[k.strip().lower()] = v.strip()
        body = b""
        try:
            clen = int(req_headers.get("content-length", "0") or 0)
            if clen > 0:
                body = await reader.readexactly(min(clen, _MAX_HTTP_BODY))
        except Exception:
            pass
        for h in ("proxy-authorization", "proxy-connection"):
            req_headers.pop(h, None)
        try:
            async with httpx.AsyncClient(
                proxy=self.upstream.to_url(), timeout=60, follow_redirects=False,
            ) as client:
                r = await client.request(method, target, headers=req_headers,
                                         content=body or None)
            status_line = f"HTTP/1.1 {r.status_code} {r.reason_phrase}\r\n"
            out_headers = "".join(f"{k}: {v}\r\n" for k, v in r.headers.items()
                                  if k.lower() != "transfer-encoding")
            writer.write(status_line.encode() + out_headers.encode() +
                         b"content-length: " + str(len(r.content)).encode() +
                         b"\r\n\r\n" + r.content)
            await writer.drain()
        except Exception as e:
            log.debug("HTTP 代理转发失败 %s: %s", target, e)
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
            except Exception:
                pass


def needs_forwarder(proxy: Optional[ProxyConfig]) -> bool:
    """仅当上游带账密时需要本地转发（无认证代理可直连）。"""
    return bool(proxy and proxy.username and proxy.password)
