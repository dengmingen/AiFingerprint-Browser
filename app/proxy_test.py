"""代理连通性测试：多端点回退探测出口 IP 与归属地，并识别 IP 质量。

端点顺序：ip-api.com（含 proxy/hosting/mobile 质量字段）→ ipwho.is → ipinfo.io。
出口 IP 的时区/国家信息同时用于提示用户该代理与指纹配置是否匹配。
"""
import time
from typing import Any, Optional

import httpx

from .models import ProxyConfig

# (url, 字段归一化函数)；归一化失败/数据无效返回 None 触发下一端点
_PROVIDERS: list[tuple[str, Any]] = [
    (
        "http://ip-api.com/json/?fields=status,message,query,country,countryCode,"
        "city,timezone,isp,proxy,hosting,mobile",
        lambda d: {
            "exit_ip": d.get("query"), "country": d.get("country"),
            "country_code": d.get("countryCode"), "city": d.get("city"),
            "timezone": d.get("timezone"), "isp": d.get("isp"),
            "ip_proxy": d.get("proxy"), "ip_hosting": d.get("hosting"),
            "ip_mobile": d.get("mobile"),
        } if d.get("status") == "success" and d.get("query") else None,
    ),
    (
        "https://ipwho.is/?fields=ip,success,country,country_code,city,timezone,connection",
        lambda d: {
            "exit_ip": d.get("ip"), "country": d.get("country"),
            "country_code": d.get("country_code"), "city": d.get("city"),
            "timezone": (d.get("timezone") or {}).get("id")
            if isinstance(d.get("timezone"), dict) else d.get("timezone"),
            "isp": (d.get("connection") or {}).get("isp"),
            "ip_proxy": None, "ip_hosting": None, "ip_mobile": None,
        } if d.get("success") and d.get("ip") else None,
    ),
    (
        "https://ipinfo.io/json",
        lambda d: {
            "exit_ip": d.get("ip"), "country": d.get("country"),
            "country_code": d.get("country"), "city": d.get("city"),
            "timezone": d.get("timezone"), "isp": d.get("org"),
            "ip_proxy": None, "ip_hosting": None, "ip_mobile": None,
        } if d.get("ip") else None,
    ),
]


def _ip_type_label(ip_proxy: Optional[bool], ip_hosting: Optional[bool],
                   ip_mobile: Optional[bool]) -> Optional[str]:
    """ip-api 质量字段 → 可读标签（机房 IP 是风控重扣分项）。"""
    if ip_proxy is None and ip_hosting is None and ip_mobile is None:
        return None
    if ip_mobile:
        return "移动网络"
    if ip_hosting or ip_proxy:
        return "机房/数据中心（风控高风险）"
    return "住宅/原生"


async def test_proxy(proxy: Optional[ProxyConfig], timeout: float = 15) -> dict:
    mode = "proxy" if proxy else "direct"
    t0 = time.perf_counter()
    errors: list[str] = []
    for url, extract in _PROVIDERS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy.to_url() if proxy else None, timeout=timeout
            ) as client:
                resp = await client.get(url)
            data = extract(resp.json())
            if data:
                data.update({
                    "ok": True,
                    "mode": mode,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "ip_type": _ip_type_label(data.pop("ip_proxy"),
                                              data.pop("ip_hosting"),
                                              data.pop("ip_mobile")),
                })
                return data
            errors.append(f"{url.split('/')[2]}: 数据无效")
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
    return {
        "ok": False,
        "mode": mode,
        "error": f"全部探测端点失败（{'; '.join(errors)}）",
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }
