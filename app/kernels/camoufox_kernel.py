"""Camoufox 内核（Firefox 定制版，C++ 层指纹改写）——推荐的正式内核。

每个环境使用独立的 user_data_dir 持久化上下文（Cookie、存储跨重启保留），
并注入该环境固定的 BrowserForge 指纹与噪声种子，保证多次启动指纹一致。
"""
import logging
from pathlib import Path
from typing import Any, Optional

from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import BrowserContext, Page, Playwright

from ..models import LaunchConfig, ProxyConfig

log = logging.getLogger(__name__)

KERNEL_NAME = "camoufox"


def is_available() -> tuple[bool, str]:
    """检查 Camoufox 浏览器二进制是否已下载。"""
    try:
        from camoufox.pkgman import launch_path

        path = launch_path()
        return True, path
    except Exception as e:  # CamoufoxNotInstalled 等
        return False, str(e)


def _default_addons_kwargs(disable_adblock: bool = False) -> dict:
    """扩展策略：需要时（或 cloudflare 预设）主动排除 uBlock——它会阻断
    challenges.cloudflare.com，是 Turnstile 报 "Can't verify the user is human"
    的常见元凶；uBlock 下载失败时同样降级排除。
    """
    from camoufox.addons import ADDONS_DIR, DefaultAddons

    if disable_adblock:
        return {"exclude_addons": [DefaultAddons.UBO]}
    ubo_dir = ADDONS_DIR / DefaultAddons.UBO.name
    if not (ubo_dir.is_dir() and (ubo_dir / "manifest.json").exists()):
        return {"exclude_addons": [DefaultAddons.UBO]}
    return {}


async def start(
    *,
    playwright: Playwright,
    profile_id: str,
    user_data_dir: Path,
    fingerprint_data: dict,
    target_os: str,
    proxy: Optional[ProxyConfig],
    launch: LaunchConfig,
    headless: Optional[bool] = None,
    start_url: Optional[str] = None,
) -> dict[str, Any]:
    from ..fingerprint_engine import build_launch_kwargs

    fp_kwargs = build_launch_kwargs(fingerprint_data)
    config: dict[str, Any] = fp_kwargs.get("config", {})
    # 默认语音按环境实际 locale 重标，避免 voices 与 Intl locale 不一致
    locale = launch.locale or None
    if locale and config.get("voices"):
        voices = config["voices"]
        prefix = locale.split("-")[0].lower()
        idx = next(
            (i for i, v in enumerate(voices) if v.get("lang", "").lower() == locale.lower()),
            next(
                (i for i, v in enumerate(voices)
                 if v.get("lang", "").split("-")[0].lower() == prefix),
                -1,
            ),
        )
        if idx >= 0:
            for v in voices:
                v["isDefault"] = False
            voices[idx]["isDefault"] = True

    kwargs: dict[str, Any] = dict(
        persistent_context=True,
        user_data_dir=str(user_data_dir),
        os=target_os,
        i_know_what_im_doing=True,  # 固定指纹由本产品负责一致性
        headless=launch.headless if headless is None else headless,
        # 类人鼠标轨迹；preset=cloudflare 时带时长上限
        humanize=(launch.humanize_max or True) if launch.humanize else None,
        block_webrtc=True if launch.block_webrtc else None,
        # Turnstile 等跨域人机验证 iframe 交互所需
        disable_coop=True if launch.disable_coop else None,
        **_default_addons_kwargs(disable_adblock=launch.disable_adblock),
        **{k: v for k, v in fp_kwargs.items() if k != "config"},
    )
    kwargs["config"] = config
    # 显式时区（china 预设/用户指定）；geoip 的属地时区在代理场景自动对齐
    if launch.timezone:
        config["timezone"] = launch.timezone
    # Cloudflare 兼容（camoufox issue #574）：Accept-Encoding 去掉 br，
    # 修复 Turnstile 静默失败（"Can't verify the user is human"）
    if launch.preset == "cloudflare":
        config.setdefault("headers.Accept-Encoding", "gzip, deflate")
    if launch.locale:
        kwargs["locale"] = launch.locale
    if proxy is not None:
        kwargs["proxy"] = proxy.to_playwright()
        # 有代理时默认按出口 IP 自动对齐时区/经纬度/语言，避免环境穿帮
        if launch.geoip:
            kwargs["geoip"] = True

    context: BrowserContext = await AsyncNewBrowser(playwright, **kwargs)

    page: Page = context.pages[0] if context.pages else await context.new_page()
    url = start_url or launch.start_url
    if url and url != "about:blank":
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log.warning("环境 %s 打开起始页失败: %s", profile_id, e)

    return {
        "context": context,
        "page": page,
        "info": {
            "kernel": KERNEL_NAME,
            "note": "Camoufox 使用 Playwright(Juggler) 协议，无 CDP 端口；"
            "自动化请走本服务 REST API，或用 camoufox server 模式。",
        },
    }


async def stop(instance: dict[str, Any]) -> None:
    context: BrowserContext = instance["context"]
    playwright: Playwright = instance["playwright"]
    try:
        await context.close()
    finally:
        await playwright.stop()
