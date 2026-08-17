"""Camoufox 的 Playwright Server 桥：为环境暴露标准 Playwright 连接端点。

实现原理：camoufox 包内置 launchServer.js，可借助 playwright-python 自带的
Node 驱动启动 `playwright.firefox.launchServer()`，把我们的指纹配置
（CAMOU_CONFIG 环境变量：固定种子/字体/语音/UA 等）完整注入浏览器进程。
外部 Python/Node Playwright 通过 `browserType.connect(ws_endpoint)` 直连，
获得与该环境一致的反检测指纹。

与"交互式持久化模式"的区别：
- Playwright Server：面向自动化，每次连接创建全新 context（无本地存储），
  登录态请用 context.storage_state() 自行管理；指纹 = 环境固定指纹
- 交互式启动：面向人工使用，Cookie 等持久保留（同一环境两者可同时运行）
"""
import asyncio
import base64
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .fingerprint_engine import build_launch_kwargs
from .kernels.camoufox_kernel import _default_addons_kwargs
from .models import LaunchConfig, ProxyConfig
from .risk_presets import apply_preset

log = logging.getLogger(__name__)

_WS_RE = re.compile(r"(ws://[^\s\x1b]+)")

# 每个环境最多一个 server 实例
_servers: dict[str, dict[str, Any]] = {}


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _node_and_driver() -> tuple[str, Path]:
    from camoufox.server import get_nodejs

    nodejs = get_nodejs()
    driver_package = Path(nodejs).parent / "package"
    return nodejs, driver_package


async def start_server(
    profile: dict,
    port: Optional[int] = None,
) -> dict[str, Any]:
    """为 camoufox 环境启动 Playwright Server，返回 ws 端点等信息。"""
    profile_id = profile["id"]
    if profile_id in _servers:
        stop_server(profile_id)

    launch = apply_preset(LaunchConfig(**(profile.get("launch") or {})))
    proxy = ProxyConfig(**profile["proxy"]) if profile.get("proxy") else None

    fp_kwargs = build_launch_kwargs(profile["fingerprint"])
    kwargs: dict[str, Any] = dict(
        headless=launch.headless,
        os=profile["target_os"],
        i_know_what_im_doing=True,
        **_default_addons_kwargs(disable_adblock=launch.disable_adblock),
        **{k: v for k, v in fp_kwargs.items() if k != "config"},
    )
    kwargs["config"] = fp_kwargs.get("config", {})
    if launch.locale:
        kwargs["locale"] = launch.locale
    if launch.timezone:
        kwargs["config"]["timezone"] = launch.timezone
    if launch.preset == "cloudflare":
        kwargs["config"].setdefault("headers.Accept-Encoding", "gzip, deflate")
    if proxy is not None:
        kwargs["proxy"] = proxy.to_playwright()
        if launch.geoip:
            kwargs["geoip"] = True
    if port:
        kwargs["port"] = int(port)  # launchServer 参数：固定端口

    # launch_options 含指纹生成/geoip 网络请求，放线程执行
    from camoufox.utils import launch_options

    options = await asyncio.to_thread(
        lambda: launch_options(**kwargs))

    nodejs, driver_package = _node_and_driver()
    from camoufox.pkgman import LOCAL_DATA

    launch_script = LOCAL_DATA / "launchServer.js"
    if not launch_script.exists():
        # 兼容包内路径
        import camoufox

        launch_script = Path(camoufox.__file__).parent / "launchServer.js"

    payload_dict = {
        _camel(k): v for k, v in options.items()  # node launchServer 需要 camelCase
    }
    payload = base64.b64encode(
        json.dumps(payload_dict, default=str).encode()
    ).decode()

    proc = await asyncio.create_subprocess_exec(
        nodejs, str(launch_script), str(driver_package),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(driver_package),
    )
    assert proc.stdin
    proc.stdin.write(payload.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # 从 stdout 解析 "Websocket endpoint: ws://..."
    ws_endpoint: Optional[str] = None
    deadline = time.time() + 90
    output_lines: list[str] = []

    async def _read_stdout() -> None:
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            output_lines.append(text)

    reader = asyncio.create_task(_read_stdout())
    try:
        while time.time() < deadline:
            if proc.returncode is not None:
                raise RuntimeError(
                    f"Playwright Server 进程退出（exit={proc.returncode}）: {''.join(output_lines)[-300:]}"
                )
            for text in output_lines:
                m = _WS_RE.search(text)
                if m:
                    ws_endpoint = m.group(1).strip()
                    break
            if ws_endpoint:
                break
            await asyncio.sleep(0.3)
    finally:
        pass
    if not ws_endpoint:
        _kill(proc)
        reader.cancel()
        raise RuntimeError(
            f"90 秒内未获得 Playwright Server 端点: {''.join(output_lines)[-300:]}"
        )

    _servers[profile_id] = {
        "process": proc,
        "reader": reader,
        "ws_endpoint": ws_endpoint,
        "started_at": time.time(),
        "port": port,
    }
    log.info("环境 %s 的 Playwright Server 已启动: %s", profile_id, ws_endpoint)
    return {
        "profile_id": profile_id,
        "ws_endpoint": ws_endpoint,
        "port": port,
        "protocol": "playwright",
        "note": "使用 playwright.firefox.connect(ws_endpoint) 连接；"
                "每次连接创建全新 context，登录态用 storage_state 管理。",
    }


def is_running(profile_id: str) -> bool:
    inst = _servers.get(profile_id)
    return bool(inst and inst["process"].returncode is None)


def get_info(profile_id: str) -> Optional[dict[str, Any]]:
    if not is_running(profile_id):
        _servers.pop(profile_id, None)
        return None
    inst = _servers[profile_id]
    return {
        "profile_id": profile_id,
        "ws_endpoint": inst["ws_endpoint"],
        "port": inst.get("port"),
        "protocol": "playwright",
        "started_at": inst["started_at"],
    }


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(  # nosec B609
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
        )
    else:
        proc.terminate()


async def stop_server(profile_id: str) -> bool:
    inst = _servers.pop(profile_id, None)
    if not inst:
        return False
    _kill(inst["process"])
    inst["reader"].cancel()
    try:
        await asyncio.wait_for(inst["process"].wait(), timeout=10)
    except Exception:
        pass
    log.info("环境 %s 的 Playwright Server 已停止", profile_id)
    return True


async def stop_all() -> None:
    for pid in list(_servers.keys()):
        await stop_server(pid)


def list_servers() -> list[dict[str, Any]]:
    return [get_info(pid) for pid in list(_servers.keys()) if get_info(pid)]
