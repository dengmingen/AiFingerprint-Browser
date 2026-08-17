"""启动管理器：维护「环境 → 运行中的浏览器实例」的生命周期。"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .kernels import camoufox_kernel, chromium_kernel, fp_chromium_kernel
from .config import PROFILE_ROOT
from . import db
from .models import LaunchConfig, ProxyConfig
from .proxy_forwarder import AuthProxyForwarder, needs_forwarder
from .risk_presets import apply_preset

log = logging.getLogger(__name__)


class LaunchError(Exception):
    pass


@dataclass
class RunningInstance:
    profile_id: str
    kernel: str
    started_at: float
    info: dict
    stop: Callable[[], Awaitable[None]]
    # camoufox 内核持有的页面引用（供 navigate/screenshot/evaluate 使用）
    page: Any = None
    context: Any = None
    process: Any = None
    extra: dict = field(default_factory=dict)

    def is_alive(self) -> bool:
        if self.kernel == "camoufox":
            return self.context is not None and not self.context.is_closed()
        return self.process is not None and self.process.poll() is None


class LaunchManager:
    def __init__(self) -> None:
        self._running: dict[str, RunningInstance] = {}
        self._lock = asyncio.Lock()
        # 允许同时拉起多个实例（批量启动并行；限制并发防资源打满）
        self._launch_sem = asyncio.Semaphore(4)
        self._starting: set[str] = set()

    def is_running(self, profile_id: str) -> bool:
        inst = self._running.get(profile_id)
        return bool(inst and inst.is_alive())

    async def start(
        self,
        profile: dict[str, Any],
        headless: Optional[bool] = None,
        start_url: Optional[str] = None,
    ) -> dict[str, Any]:
        profile_id = profile["id"]
        async with self._lock:
            if self.is_running(profile_id):
                raise LaunchError(f"环境 {profile['name']} 已在运行")
            if profile_id in self._starting:
                raise LaunchError(f"环境 {profile['name']} 正在启动中")
            self._starting.add(profile_id)
        try:
            user_data_dir = PROFILE_ROOT / profile_id
            user_data_dir.mkdir(parents=True, exist_ok=True)

            launch = LaunchConfig(**(profile.get("launch") or {}))
            launch = apply_preset(launch)  # 风控预设参数组合
            proxy = (
                ProxyConfig(**profile["proxy"])
                if profile.get("proxy")
                else None
            )

            # Chromium 系内核不支持带账密代理命令行：本地起认证转发器桥接
            forwarder: AuthProxyForwarder | None = None
            kernel_proxy = proxy
            if (profile["kernel"] in (fp_chromium_kernel.KERNEL_NAME,
                                      chromium_kernel.KERNEL_NAME)
                    and needs_forwarder(proxy)):
                forwarder = await AuthProxyForwarder(proxy).start()
                kernel_proxy = ProxyConfig(
                    scheme="http", host="127.0.0.1", port=forwarder.port
                )

            async with self._launch_sem:
                if profile["kernel"] == camoufox_kernel.KERNEL_NAME:
                    from playwright.async_api import async_playwright

                    pw = await async_playwright().start()
                    try:
                        result = await camoufox_kernel.start(
                            playwright=pw,
                            profile_id=profile_id,
                            user_data_dir=user_data_dir,
                            fingerprint_data=profile["fingerprint"],
                            target_os=profile["target_os"],
                            proxy=proxy,
                            launch=launch,
                            headless=headless,
                            start_url=start_url,
                        )
                    except Exception:
                        await pw.stop()
                        raise
                    result["playwright"] = pw
                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=camoufox_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=lambda: camoufox_kernel.stop(result),
                        page=result["page"],
                        context=result["context"],
                    )
                elif profile["kernel"] == fp_chromium_kernel.KERNEL_NAME:
                    result = await fp_chromium_kernel.start(
                        profile_id=profile_id,
                        user_data_dir=user_data_dir,
                        fingerprint_data=profile["fingerprint"],
                        target_os=profile["target_os"],
                        proxy=kernel_proxy,
                        launch=launch,
                        headless=headless,
                        start_url=start_url,
                    )
                    _fwd = forwarder

                    async def _stop_fpchromium() -> None:
                        await fp_chromium_kernel.stop(result)
                        if _fwd:
                            await _fwd.stop()

                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=fp_chromium_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=_stop_fpchromium,
                        process=result["process"],
                    )
                else:
                    result = await chromium_kernel.start(
                        profile_id=profile_id,
                        user_data_dir=user_data_dir,
                        proxy=kernel_proxy,
                        launch=launch,
                        headless=headless,
                        start_url=start_url,
                    )
                    _fwd = forwarder

                    async def _stop_chromium() -> None:
                        await chromium_kernel.stop(result)
                        if _fwd:
                            await _fwd.stop()

                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=chromium_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=_stop_chromium,
                        process=result["process"],
                    )

            if forwarder:
                inst.extra["proxy_forwarder"] = True
                inst.info = {**inst.info,
                             "proxy_via": f"本地认证转发 127.0.0.1:{forwarder.port}"}

            async with self._lock:
                self._running[profile_id] = inst
        except Exception:
            if forwarder:
                await forwarder.stop()
            raise
        finally:
            async with self._lock:
                self._starting.discard(profile_id)

        # 环境使用统计（启动次数/最近启动）
        try:
            db.bump_usage(profile_id)
        except Exception:
            log.debug("使用统计更新失败", exc_info=True)

        return {
            "profile_id": profile_id,
            "profile_name": profile["name"],
            "started_at": inst.started_at,
            **inst.info,
        }

    async def stop(self, profile_id: str) -> None:
        async with self._lock:
            inst = self._running.pop(profile_id, None)
        if inst is None:
            return
        try:
            await inst.stop()
        except Exception as e:
            log.warning("停止环境 %s 时出错: %s", profile_id, e)

    async def stop_all(self) -> None:
        ids = list(self._running.keys())
        for pid in ids:
            await self.stop(pid)

    def active(self) -> list[dict[str, Any]]:
        # 清理已退出的实例（用户手动关掉浏览器窗口的情况）
        for pid in [p for p, i in self._running.items() if not i.is_alive()]:
            self._running.pop(pid, None)
        return [
            {
                "profile_id": i.profile_id,
                "kernel": i.kernel,
                "started_at": i.started_at,
                **i.info,
            }
            for i in self._running.values()
        ]

    def get_instance(self, profile_id: str) -> Optional[RunningInstance]:
        inst = self._running.get(profile_id)
        if inst and inst.is_alive():
            return inst
        return None
