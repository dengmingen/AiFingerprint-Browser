"""fingerprint-chromium 内核（第三内核）。

基于开源项目 adryfish/fingerprint-chromium（Ungoogled Chromium 打补丁），
在 Chromium 内核层伪造指纹（Canvas/WebGL/字体/ClientRects/GPU 等），
并保留标准 CDP 调试端口——Selenium/Puppeteer/Playwright 可直连。

与 chromium 调试内核的区别：真正具备指纹伪装能力，可用于生产。
安装：从 https://github.com/adryfish/fingerprint-chromium/releases 下载并解压，
通过环境变量 FPWB_FPCHROMIUM 指向 chrome 可执行文件，或放入 <项目>/tools/fp-chromium/。

指纹参数（命令行注入，全部由该内核在 C++ 层实现）：
  --fingerprint=<32位种子>            启用指纹伪造（种子驱动 canvas/webgl 等）
  --fingerprint-platform=<os>         伪装操作系统
  --fingerprint-platform-version=<v>  伪装 OS 具体版本（148+；缺省按种子稳定选取）
  --fingerprint-brand=Chrome          UA 品牌（Chrome/Edge/Opera/Vivaldi）
  --fingerprint-brand-version=<ver>   UA 品牌版本
  --fingerprint-hardware-concurrency  CPU 核心数
  --disable-spoofing=<list>           关闭单项伪装（font,audio,canvas,clientrects,gpu）
  --lang / --accept-lang / --timezone 语言与时区
"""
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import BASE_DIR
from ..models import LaunchConfig, ProxyConfig

KERNEL_NAME = "fp-chromium"

# 各 OS 的真实 platformVersion 取值域（Client Hints 语义；按种子确定性选取）
_PLATFORM_VERSIONS = {
    "windows": ["10.0.0", "13.0.0", "14.0.0", "15.0.0", "15.0.0", "10.0.0"],
    "macos": ["13.5.0", "14.4.1", "14.7.0", "14.7.0", "15.1.0", "15.3.0"],
    "linux": ["5.15.0", "6.1.0", "6.1.0", "6.5.0", "6.8.0", "6.8.0"],
}


def _stable_platform_version(target_os: str, seed: int) -> str:
    """同一种子永远得到同一版本号（跨启动稳定，避免指纹漂移）。"""
    pool = _PLATFORM_VERSIONS.get(target_os)
    return pool[seed % len(pool)] if pool else "10.0.0"

# Chrome 大版本 → 对应 fingerprint-chromium release 存在（132~148）
_EXE_NAMES = ("chrome.exe", "chrome")


def find_executable() -> Optional[str]:
    env = os.environ.get("FPWB_FPCHROMIUM")
    if env and Path(env).exists():
        return env
    tools = BASE_DIR / "tools" / "fp-chromium"
    if tools.is_dir():
        for name in _EXE_NAMES:
            for candidate in [tools / name, *tools.rglob(name)]:
                if candidate.exists():
                    return str(candidate)
    return None


def is_available() -> tuple[bool, str]:
    exe = find_executable()
    return (True, exe) if exe else (
        False,
        "未安装。从 github.com/adryfish/fingerprint-chromium/releases 下载解压后，"
        "设置环境变量 FPWB_FPCHROMIUM 指向 chrome 可执行文件，或放入 tools/fp-chromium/ 目录",
    )


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
    fingerprint_data: dict,
    target_os: str,
    proxy: Optional[ProxyConfig],
    launch: LaunchConfig,
    headless: Optional[bool] = None,
    start_url: Optional[str] = None,
) -> dict[str, Any]:
    import asyncio

    exe = find_executable()
    if not exe:
        raise RuntimeError(is_available()[1])

    # 指纹种子：直接使用该环境固定的 canvas 噪声种子（跨启动稳定）
    seed = int((fingerprint_data.get("seeds") or {}).get("canvas", 0) or 0)
    if seed <= 0:
        raise RuntimeError("环境指纹数据缺少种子，请重新生成指纹")

    nav = {}
    if fingerprint_data.get("mode") == "preset":
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
    else:
        nav = (fingerprint_data.get("fingerprint") or {}).get("navigator") or {}

    port = _free_port()
    args = [
        exe,
        f"--fingerprint={seed}",
        f"--fingerprint-platform={ {'windows': 'windows', 'macos': 'macos', 'linux': 'linux'}[target_os] }",
        f"--fingerprint-platform-version={launch.fp_platform_version or _stable_platform_version(target_os, seed)}",
        "--fingerprint-brand=Chrome",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--restore-last-session=false",
        "--disable-session-crashed-bubble",
        # WebRTC 默认禁用非代理 UDP，防 IP 泄露（该内核默认行为，显式声明）
        "--disable-non-proxied-udp",
    ]
    if launch.fp_disable_spoofing:
        args.append(f"--disable-spoofing={launch.fp_disable_spoofing}")
    hw = nav.get("hardwareConcurrency")
    if hw:
        args.append(f"--fingerprint-hardware-concurrency={int(hw)}")
    ua_version = None
    ua = nav.get("userAgent") or ""
    if "Chrome/" in ua:
        ua_version = ua.split("Chrome/")[1].split(" ")[0].split(".")[0]
    if ua_version:
        args.append(f"--fingerprint-brand-version={ua_version}")
    if launch.locale:
        args += [f"--lang={launch.locale}", f"--accept-lang={launch.locale}"]
    if launch.timezone:
        args.append(f"--timezone={launch.timezone}")
    if launch.block_webrtc:
        args.append("--disable-webrtc")
    if proxy is not None:
        # 该内核命令行代理不支持账号密码（Chromium 限制）
        args.append(f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}")
    effective_headless = headless if headless is not None else launch.headless
    if effective_headless:
        # 不加 --disable-gpu：软件渲染是风控特征（SwiftShader 渲染器）
        args.append("--headless=new")
    args.append(start_url or launch.start_url or "about:blank")

    proc = subprocess.Popen(  # nosec B603 参数来自本地受控配置
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    ws_endpoint: Optional[str] = None
    async with httpx.AsyncClient() as client:
        for _ in range(40):
            await asyncio.sleep(0.5)
            if proc.poll() is not None:
                raise RuntimeError(f"浏览器进程提前退出（exit={proc.returncode}）")
            try:
                r = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
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
            "fingerprint_seed": seed,
            "note": "fingerprint-chromium 内核：内核层指纹伪装 + 标准 CDP 端点，"
                    "Selenium/Puppeteer/Playwright 可直连 ws_endpoint。",
        },
    }


def _kill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(  # nosec B609
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
        )
    else:
        proc.terminate()


async def stop(instance: dict[str, Any]) -> None:
    import asyncio

    proc: subprocess.Popen = instance["process"]
    if proc.poll() is None:
        _kill(proc)
        await asyncio.to_thread(proc.wait, timeout=10)
