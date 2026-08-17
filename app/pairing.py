"""插件配对码：浏览器插件与工作台的一键配置机制。

流程：
1. 用户在工作台（设置页或 API）生成 6 位配对码（5 分钟有效，一次性）
2. 在插件中输入配对码 → 插件调用 /api/v1/pair/exchange 换取 API Key
3. 插件保存密钥完成配置，全程无需手动复制粘贴长密钥

安全：配对码短时效 + 一次性 + 尝试次数上限（5 次）；
exchange 接口不需要 API Key（配对码本身就是凭证），仅限本机默认部署场景。
"""
import secrets
import time
from typing import Optional

# 内存态：code -> {api_key, expires_at, used, attempts}
_codes: dict[str, dict] = {}

TTL_SECONDS = 300
MAX_ATTEMPTS = 5


def create_pairing(api_key: str) -> tuple[str, int]:
    """生成配对码，返回 (code, ttl)。"""
    # 清理过期
    now = time.time()
    for c in [k for k, v in _codes.items() if v["expires_at"] < now]:
        _codes.pop(c)
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    _codes[code] = {"api_key": api_key, "expires_at": now + TTL_SECONDS,
                    "used": False, "attempts": 0}
    return code, TTL_SECONDS


def exchange(code: str) -> Optional[str]:
    """用配对码换取 API Key；无效/过期/已用/尝试过多返回 None。"""
    item = _codes.get((code or "").strip().upper())
    if not item:
        return None
    if item["used"] or time.time() > item["expires_at"]:
        _codes.pop((code or "").strip().upper(), None)
        return None
    item["attempts"] += 1
    if item["attempts"] > MAX_ATTEMPTS:
        _codes.pop((code or "").strip().upper(), None)
        return None
    item["used"] = True
    return item["api_key"]
