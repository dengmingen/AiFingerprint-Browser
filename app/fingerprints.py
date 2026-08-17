"""每个环境（profile）固定指纹的生成与序列化。

Camoufox 每次启动会随机生成 BrowserForge 指纹与 canvas/audio/字体噪声种子，
而产品要求「同一环境每次启动指纹一致」，因此：
- 创建环境时生成一次 BrowserForge 指纹并序列化入库；
- 同时生成三个固定噪声种子，启动时通过 config 参数预置
  （camoufox 的 set_into 只在键不存在时写入，因此用户提供的种子优先生效）。
"""
import dataclasses
import json
import random
from typing import Any, get_type_hints

from browserforge.fingerprints import Fingerprint

_MAX_SEED = 4_294_967_295  # camoufox 内部使用 randrange(1, 2**32)


def _dump_dataclass(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dump_dataclass(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _dump_dataclass(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dump_dataclass(v) for v in obj]
    return obj


def _load_dataclass(cls: type, data: dict) -> Any:
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name)
        if dataclasses.is_dataclass(ftype) and isinstance(value, dict):
            value = _load_dataclass(ftype, value)
        else:
            origin = getattr(ftype, "__origin__", None)
            if origin is not None and hasattr(ftype, "__args__"):
                for arg in ftype.__args__:
                    if dataclasses.is_dataclass(arg) and isinstance(value, dict):
                        value = _load_dataclass(arg, value)
                        break
        kwargs[f.name] = value
    return cls(**kwargs)


def generate_profile_fingerprint(target_os: str) -> dict[str, Any]:
    """生成一套属于该环境的指纹：BrowserForge 指纹 + 三个固定噪声种子。"""
    import re

    from camoufox.fingerprints import generate_fingerprint
    from camoufox.pkgman import installed_verstr

    fp = generate_fingerprint(os=target_os)
    # camoufox 启动时会把 UA 的 Firefox 版本号强制对齐到已安装内核版本
    # （防止 UA 与真实内核行为不一致被识破）。存储前做同样对齐，
    # 保证「库里存的指纹 == 浏览器实际呈现的指纹」。
    try:
        major = installed_verstr().split(".", 1)[0]
        ua = re.sub(r"Firefox/\d+\.0", f"Firefox/{major}.0", fp.navigator.userAgent)
        ua = re.sub(r"rv:\d+\.0", f"rv:{major}.0", ua)
        fp.navigator.userAgent = ua
    except Exception:
        pass

    # 字体子集与语音列表默认每次启动随机生成（camoufox 的防关联策略），
    # 但对「同一环境=同一台设备」的产品语义会造成跨重启漂移，
    # 因此在创建环境时生成一次并固定存储。
    fonts: list[str] = []
    voices: list[dict] = []
    try:
        from camoufox.fingerprints import (
            _generate_random_font_subset,
            _generate_random_voice_subset,
        )

        fonts = _generate_random_font_subset(target_os)
        voices = _generate_random_voice_subset(target_os)
    except Exception:
        pass

    return {
        "fingerprint": _dump_dataclass(fp),
        "seeds": {
            "canvas": random.randint(1, _MAX_SEED),
            "audio": random.randint(1, _MAX_SEED),
            "font_spacing": random.randint(1, _MAX_SEED),
        },
        "fonts": fonts,
        "voices": voices,
    }


def load_fingerprint(fingerprint_data: dict[str, Any]) -> Fingerprint:
    """把库里的 JSON 还原为 BrowserForge Fingerprint 对象。"""
    return _load_dataclass(Fingerprint, fingerprint_data["fingerprint"])


def seeds_to_config(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """把固定噪声种子（及语音列表）转成 camoufox launch_options 的 config 参数。"""
    seeds = fingerprint_data.get("seeds", {})
    config: dict[str, Any] = {
        "canvas:seed": int(seeds.get("canvas", 1)),
        "audio:seed": int(seeds.get("audio", 1)),
        "fonts:spacing_seed": int(seeds.get("font_spacing", 1)),
    }
    voices = fingerprint_data.get("voices")
    if voices:
        config["voices"] = voices
    return config


def fingerprint_summary(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """给列表页展示用的指纹摘要。"""
    fp = fingerprint_data.get("fingerprint", {})
    navigator = fp.get("navigator", {})
    screen = fp.get("screen", {})
    video_card = fp.get("videoCard") or {}
    return {
        "user_agent": navigator.get("userAgent"),
        "platform": navigator.get("platform"),
        "languages": navigator.get("languages"),
        "hardware_concurrency": navigator.get("hardwareConcurrency"),
        "device_memory": navigator.get("deviceMemory"),
        "screen": (
            f"{screen.get('width')}x{screen.get('height')}@{screen.get('colorDepth')}"
            if screen.get("width") else None
        ),
        "webgl_renderer": video_card.get("renderer"),
        "fonts_count": len(fp.get("fonts", [])),
        "seeds": fingerprint_data.get("seeds"),
    }


def validate_fingerprint_json(raw: str) -> bool:
    try:
        data = json.loads(raw)
        load_fingerprint(data)
        return True
    except Exception:
        return False
