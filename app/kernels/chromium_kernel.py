"""系统 Chromium/Edge 内核——降级与调试用。

不做任何指纹伪装，仅提供独立数据目录 + 代理 + CDP 调试端口，
用于开发期验证产品链路（profile 管理、启动、自动化对接）。
生产使用请选择 camoufox 内核。
"""
import asyncio
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from ..models import LaunchConfig, ProxyConfig

KERNEL_NAME = "chromium"
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _find_executable() -> Optional[str]:
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for path in _CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def is_available() -> tuple[bool, str]:
    exe = _find_executable()
    return (True, exe) if exe else (False, "未找到系统 Chrome/Edge")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def start(
    *,
    profile_id: str,
    user_data_dir: Path,
    proxy: Optional[ProxyConfig],
    launch: LaunchConfig,
    headless: Optional[bool] = None,
    start_url: Optional[str] = None,
) -> dict[str, Any]:
    exe = _find_executable()
    if not exe:
        raise RuntimeError("未找到系统 Chrome/Edge，无法使用 chromium 内核")

    port = _free_port()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--restore-last-session=false",
    ]
    if proxy is not None:
        # 注意：Chrome 命令行代理不支持账号密码，带认证的代理请用 camoufox 内核
        args.append(f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}")
    effective_headless = headless if headless is not None else launch.headless
    if effective_headless:
        args += ["--headless=new", "--disable-gpu"]
    args.append(start_url or launch.start_url or "about:blank")

    proc = subprocess.Popen(  # nosec B603 参数全部来自本地配置
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    # 等待 CDP 端口就绪
    ws_endpoint: Optional[str] = None
    async with httpx.AsyncClient() as client:
        for _ in range(40):
            await asyncio.sleep(0.5)
            if proc.poll() is not None:
                raise RuntimeError(f"浏览器进程提前退出（exit={proc.returncode}）")
            try:
                r = await client.get(
                    f"http://127.0.0.1:{port}/json/version", timeout=2
                )
                ws_endpoint = r.json().get("webSocketDebuggerUrl")
                if ws_endpoint:
                    break
            except Exception:
                continue
    if not ws_endpoint:
        _kill(proc)
        raise RuntimeError("CDP 调试端口在 20 秒内未就绪")

    return {
        "process": proc,
        "debug_port": port,
        "info": {
            "kernel": KERNEL_NAME,
            "debug_port": port,
            "ws_endpoint": ws_endpoint,
            "note": "调试内核，无指纹伪装。Selenium/Puppeteer/Playwright 可连接 ws_endpoint。",
        },
    }


def _kill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(  # nosec B609 参数为内部值
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.terminate()


async def stop(instance: dict[str, Any]) -> None:
    proc: subprocess.Popen = instance["process"]
    if proc.poll() is None:
        _kill(proc)
        await asyncio.to_thread(proc.wait, timeout=10)
