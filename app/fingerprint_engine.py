"""指纹引擎：环境指纹的双模式生成、一致性体检与启动参数装配。

模式:
- generate: BrowserForge 合成指纹（默认，无限独特组合）
- preset:   Camoufox 内置的真实设备指纹预设库（真实采集数据）

统一存储格式（fingerprint_json）:
{
  "mode": "generate" | "preset",
  "fingerprint": {...BrowserForge dump...} | null,
  "preset": {...camoufox preset dict...} | null,
  "seeds": {"canvas": int, "audio": int, "font_spacing": int},
  "fonts": [...该环境固定的字体子集...],
  "voices": [...] | null,        # preset 自带 speechVoices 时为 null（用预设的）
  "health": {"score": int, "warnings": [...]}
}
"""
import random
import re
from typing import Any, Optional

from . import fingerprints as fp_lib

_MAX_SEED = 4_294_967_295

_GPU_OS_HINTS = {
    "windows": ("Direct3D", "D3D11", "ANGLE (", "Google Inc."),
    "macos": ("Apple", "ANGLE (Apple", "Metal"),
    "linux": ("Mesa", "llvmpipe", "X11", "DRI"),
}


def create_fingerprint(target_os: str, mode: str = "generate") -> dict[str, Any]:
    """创建一套环境指纹（生成模式或真实预设模式）。"""
    data: dict[str, Any] = {
        "mode": mode,
        "fingerprint": None,
        "preset": None,
        "seeds": {
            "canvas": random.randint(1, _MAX_SEED),
            "audio": random.randint(1, _MAX_SEED),
            "font_spacing": random.randint(1, _MAX_SEED),
        },
        "fonts": [],
        "voices": None,
        "health": {"score": 100, "warnings": []},
    }

    if mode == "preset":
        preset = _random_preset(target_os)
        if preset is None:  # 预设库不可用时回退到合成模式
            data["mode"] = "generate"
        else:
            preset = _fix_preset_webgl(preset, target_os)
            data["preset"] = _align_preset_ua(preset)

    if data["mode"] == "generate":
        data["fingerprint"] = fp_lib.generate_profile_fingerprint(target_os)["fingerprint"]

    # 字体子集固定（两种模式都需要，否则每次启动随机导致度量漂移）
    try:
        from camoufox.fingerprints import _generate_random_font_subset
        data["fonts"] = _generate_random_font_subset(target_os)
    except Exception:
        pass

    # preset 模式且预设自带 speechVoices 时不注入生成的语音列表（用预设真实值）
    if not (data["mode"] == "preset" and data["preset"].get("speechVoices")):
        try:
            from camoufox.fingerprints import _generate_random_voice_subset
            data["voices"] = _generate_random_voice_subset(target_os)
        except Exception:
            pass

    data["health"] = check_health(data, target_os)
    return data


def _random_preset(target_os: str) -> Optional[dict]:
    try:
        from camoufox.fingerprints import get_random_preset
        return get_random_preset(os=target_os)
    except Exception:
        return None


def _fix_preset_webgl(preset: dict, target_os: str) -> dict:
    """部分真实预设的 WebGL 是泛化的 "Mozilla"（该设备的隐私设置所致），
    camoufox 的 WebGL 参数库无法为其采样，启动会报
    "No WebGL data found"。创建时替换为该 OS 的真实 GPU 参数并固定存储。
    """
    webgl = dict(preset.get("webgl") or {})
    vendor = str(webgl.get("unmaskedVendor") or "")
    renderer = str(webgl.get("unmaskedRenderer") or "")
    if vendor and vendor != "Mozilla" and renderer and renderer != "Mozilla":
        return preset
    try:
        from camoufox.webgl import sample_webgl

        os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(target_os, "win")
        sampled = sample_webgl(os_key)
        webgl["unmaskedVendor"] = sampled.get("webGl:vendor", vendor)
        webgl["unmaskedRenderer"] = sampled.get("webGl:renderer", renderer)
        preset = dict(preset)
        preset["webgl"] = webgl
    except Exception:
        pass
    return preset


def _align_preset_ua(preset: dict) -> dict:
    """把预设 UA 的 Firefox 版本号对齐到已安装内核（camoufox 启动时也会做同样处理）。"""
    preset = dict(preset)
    nav = dict(preset.get("navigator") or {})
    ua = nav.get("userAgent")
    if ua:
        try:
            from camoufox.pkgman import installed_verstr
            major = installed_verstr().split(".", 1)[0]
            ua = re.sub(r"Firefox/\d+\.0", f"Firefox/{major}.0", ua)
            ua = re.sub(r"rv:\d+\.0", f"rv:{major}.0", ua)
            nav["userAgent"] = ua
        except Exception:
            pass
    preset["navigator"] = nav
    return preset


def build_launch_kwargs(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """把存储的指纹转成 AsyncNewBrowser 的指纹相关 kwargs。"""
    mode = fingerprint_data.get("mode", "generate")
    kwargs: dict[str, Any] = {}

    config: dict[str, Any] = {}
    seeds = fingerprint_data.get("seeds") or {}
    if seeds.get("canvas"):
        config["canvas:seed"] = int(seeds["canvas"])
    if seeds.get("audio"):
        config["audio:seed"] = int(seeds["audio"])
    if seeds.get("font_spacing"):
        config["fonts:spacing_seed"] = int(seeds["font_spacing"])
    voices = fingerprint_data.get("voices")
    if voices:
        config["voices"] = voices

    if mode == "preset" and fingerprint_data.get("preset"):
        # 预设由 camoufox 转换（合并不覆盖既有键，我们的种子优先）
        kwargs["fingerprint_preset"] = fingerprint_data["preset"]
    else:
        kwargs["fingerprint"] = fp_lib.load_fingerprint(fingerprint_data)

    kwargs["config"] = config
    if fingerprint_data.get("fonts"):
        kwargs["fonts"] = fingerprint_data["fonts"]
    return kwargs


# ---------------------------------------------------------------- 一致性体检

def check_health(fingerprint_data: dict[str, Any], target_os: str) -> dict[str, Any]:
    """静态一致性体检：跨信号矛盾是反检测的头号破绽，创建/更新时检查并落库。"""
    warnings: list[str] = []

    if fingerprint_data.get("mode") == "preset":
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
        screen = (fingerprint_data.get("preset") or {}).get("screen") or {}
        webgl = (fingerprint_data.get("preset") or {}).get("webgl") or {}
        ua, platform = nav.get("userAgent", ""), nav.get("platform", "")
        renderer = str(webgl.get("unmaskedRenderer", ""))
        hw = nav.get("hardwareConcurrency")
    else:
        fp = fingerprint_data.get("fingerprint") or {}
        nav = fp.get("navigator") or {}
        screen = fp.get("screen") or {}
        vc = fp.get("videoCard") or {}
        ua, platform = nav.get("userAgent", ""), nav.get("platform", "")
        renderer = str(vc.get("renderer", ""))
        hw = nav.get("hardwareConcurrency")

    # UA 与 platform 与目标 OS 三方一致
    ua_os = "windows" if "Windows NT" in ua else "macos" if "Mac OS X" in ua else \
            "linux" if "X11; Linux" in ua else "unknown"
    if ua_os != "unknown" and ua_os != target_os:
        warnings.append(f"UA 声称 {ua_os} 但目标是 {target_os}")
    expected_platform = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}
    if platform and platform != expected_platform.get(target_os, platform):
        warnings.append(f"platform={platform} 与目标 OS {target_os} 不匹配")

    # GPU 渲染器与 OS 大方向匹配（宽松检查，只报明显矛盾）
    if renderer:
        hints = _GPU_OS_HINTS.get(target_os, ())
        if hints and not any(h.lower() in renderer.lower() for h in hints):
            warnings.append(f"WebGL 渲染器与 {target_os} 常见硬件不符: {renderer[:50]}")

    # 屏幕合理性
    try:
        w, h = int(screen.get("width", 0)), int(screen.get("height", 0))
        if w and (w > 7680 or h > 4320 or w < 640 or h < 480):
            warnings.append(f"屏幕尺寸异常: {w}x{h}")
        if screen.get("colorDepth") not in (None, 24, 30):
            warnings.append(f"色深异常: {screen.get('colorDepth')}")
    except (TypeError, ValueError):
        warnings.append("屏幕字段类型异常")

    # 硬件并发数
    try:
        if hw is not None and not 1 <= int(hw) <= 64:
            warnings.append(f"hardwareConcurrency 异常: {hw}")
    except (TypeError, ValueError):
        warnings.append(f"hardwareConcurrency 非数值: {hw}")

    # 固定要素齐全
    seeds = fingerprint_data.get("seeds") or {}
    for key in ("canvas", "audio", "font_spacing"):
        if not seeds.get(key):
            warnings.append(f"噪声种子缺失: {key}")
    if not fingerprint_data.get("fonts"):
        warnings.append("字体子集缺失")

    score = max(50, 100 - 12 * len(warnings))
    return {"score": score, "warnings": warnings}


def summarize(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """列表页摘要：UA、平台、屏幕、GPU、模式与健康分。"""
    mode = fingerprint_data.get("mode", "generate")
    if mode == "preset":
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
        screen = (fingerprint_data.get("preset") or {}).get("screen") or {}
        webgl = (fingerprint_data.get("preset") or {}).get("webgl") or {}
        renderer = webgl.get("unmaskedRenderer")
        fonts_count = None
    else:
        fp = fingerprint_data.get("fingerprint") or {}
        nav = fp.get("navigator") or {}
        screen = fp.get("screen") or {}
        renderer = (fp.get("videoCard") or {}).get("renderer")
        fonts_count = len(fp.get("fonts", []) or [])
    return {
        "mode": "真实预设" if mode == "preset" else "合成生成",
        "user_agent": nav.get("userAgent"),
        "platform": nav.get("platform"),
        "languages": nav.get("languages"),
        "hardware_concurrency": nav.get("hardwareConcurrency"),
        "screen": (
            f"{screen.get('width')}x{screen.get('height')}@{screen.get('colorDepth')}"
            if screen.get("width") else None
        ),
        "webgl_renderer": renderer,
        "fonts_count": fonts_count if fonts_count is not None else len(fingerprint_data.get("fonts", []) or []),
        "seeds": fingerprint_data.get("seeds"),
        "health": fingerprint_data.get("health", {"score": 100, "warnings": []}),
    }
