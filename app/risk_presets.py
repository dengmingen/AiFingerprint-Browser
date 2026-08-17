"""风控环境预设：针对不同风控体系的一键参数组合。

- cloudflare: Cloudflare Bot Management / Turnstile 强化
  * disable_coop：Turnstile 勾选框位于跨域 iframe，需关闭 COOP 才能交互
  * 类人鼠标轨迹默认开启并调高时长上限
  * 建议：有头模式 + 住宅代理（无头与机房 IP 是 CF 的重点打击对象）
- china: 极验 / 网易易盾等国内风控
  * locale 默认 zh-CN、时区默认 Asia/Shanghai（与国内 IP 属地一致）
  * 开启 geoip（代理出口属地自动对齐）
  * 建议：国内住宅/原生 IP；设备指纹连续性由本产品的固定指纹保证
"""
from .models import LaunchConfig

PRESET_DOC = {
    "standard": "通用：默认参数，适合大多数站点",
    "cloudflare": "Cloudflare 强化：关闭 COOP（Turnstile 可交互）+ 类人轨迹增强；建议有头模式与住宅代理",
    "china": "国内风控（极验/易盾）：zh-CN + Asia/Shanghai + geoip 对齐；建议国内住宅 IP",
}


def apply_preset(launch: LaunchConfig) -> LaunchConfig:
    """按预设补全启动参数（用户显式设置的值优先，不覆盖）。"""
    data = launch.model_dump()
    if launch.preset == "cloudflare":
        data["humanize"] = True
        data["humanize_max"] = launch.humanize_max if launch.humanize_max is not None else 2.0
        data["disable_coop"] = True
        # uBlock 会阻断 challenges.cloudflare.com，是 Turnstile
        # "Can't verify the user is human" 的常见元凶
        data["disable_adblock"] = True
    elif launch.preset == "china":
        data["locale"] = launch.locale or "zh-CN"
        data["timezone"] = launch.timezone or "Asia/Shanghai"
        data["geoip"] = True
    return LaunchConfig(**data)
