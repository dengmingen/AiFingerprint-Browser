"""指纹矩阵风控：分布统计、查重与关联风险扫描。

反检测产品的核心风控逻辑：单个环境的指纹再真实，矩阵内部若出现
"两套几乎一样的指纹"（同一 GPU 渲染器 + 屏幕 + UA 组合），平台侧
只需聚类即可将整个矩阵关联起来。本模块扫描全部环境，输出：
- 特征分布（OS/GPU/屏幕/内存），识别"指纹聚集"
- 精确重复组（同一 UA+屏幕+GPU 出现在多个环境）→ 高危
- 拥挤度（同一 GPU+屏幕 组合超过阈值）→ 中危
- 逐环境风险标记与处置建议（重新生成指纹）
"""
from collections import Counter
from typing import Any

from . import db
from .fingerprint_engine import create_fingerprint


def _features(profile: dict) -> dict[str, Any]:
    fp = profile.get("fingerprint") or {}
    if fp.get("mode") == "preset":
        nav = (fp.get("preset") or {}).get("navigator") or {}
        screen = (fp.get("preset") or {}).get("screen") or {}
        webgl = (fp.get("preset") or {}).get("webgl") or {}
    else:
        bf = fp.get("fingerprint") or {}
        nav = bf.get("navigator") or {}
        screen = bf.get("screen") or {}
        webgl = bf.get("videoCard") or {}
    screen_key = f"{screen.get('width')}x{screen.get('height')}" if screen.get("width") else "未知"
    return {
        "profile_id": profile["id"],
        "name": profile["name"],
        "os": profile["target_os"],
        "ua": nav.get("userAgent") or "未知",
        "screen": screen_key,
        "gpu": webgl.get("unmaskedRenderer") or webgl.get("renderer") or "未知",
        "hw": nav.get("hardwareConcurrency"),
    }


def build_report() -> dict[str, Any]:
    profiles = db.list_profiles()
    feats = [_features(p) for p in profiles]

    def counter(key: str) -> list[dict[str, Any]]:
        c = Counter(f[key] for f in feats)
        return [{"value": v, "count": n} for v, n in c.most_common()]

    # 精确重复：UA + 屏幕 + GPU 完全一致
    exact_groups: dict[tuple, list[dict]] = {}
    for f in feats:
        exact_groups.setdefault((f["ua"], f["screen"], f["gpu"]), []).append(f)
    duplicates = [
        {"size": len(members), "members": members, "signature": key}
        for key, members in exact_groups.items() if len(members) > 1
    ]

    # 种子复制：Canvas/Audio 噪声种子相同 = 指纹被整体复制（导入/克隆），高危
    seed_groups: dict[tuple, list[dict]] = {}
    for p, f in zip(profiles, feats):
        seeds = (p.get("fingerprint") or {}).get("seeds") or {}
        key = (seeds.get("canvas"), seeds.get("audio"))
        if key != (None, None):
            entry = {k: f[k] for k in ("profile_id", "name")}
            seed_groups.setdefault(key, []).append(entry)
    seed_dupes = [
        {"size": len(members), "members": members}
        for key, members in seed_groups.items() if len(members) > 1
    ]
    seed_dupe_ids = {m["profile_id"] for g in seed_dupes for m in g["members"]}

    # 拥挤度：同一 GPU+屏幕 组合 ≥4 个环境（跨 UA）
    crowd_counter: dict[tuple, list[dict]] = {}
    for f in feats:
        crowd_counter.setdefault((f["gpu"], f["screen"]), []).append(f)
    crowded = [
        {"size": len(members), "members": [m["profile_id"] for m in members]}
        for key, members in crowd_counter.items() if len(members) >= 4
    ]
    crowded_ids = {pid for g in crowded for pid in g["members"]}
    dup_ids = {m["profile_id"] for g in duplicates for m in g["members"]}

    risks = []
    for f in feats:
        if f["profile_id"] in seed_dupe_ids and f["profile_id"] not in dup_ids:
            risks.append({"profile_id": f["profile_id"], "name": f["name"],
                          "risk": "high", "reason": "噪声种子与其它环境相同（指纹被复制/克隆）"})
        elif f["profile_id"] in dup_ids:
            risks.append({"profile_id": f["profile_id"], "name": f["name"],
                          "risk": "high", "reason": "与其它环境指纹完全相同（矩阵关联风险）"})
        elif f["profile_id"] in crowded_ids:
            risks.append({"profile_id": f["profile_id"], "name": f["name"],
                          "risk": "medium", "reason": "GPU+屏幕组合过于拥挤（≥4 个环境共用）"})

    return {
        "total": len(profiles),
        "distribution": {
            "os": counter("os"),
            "gpu": counter("gpu"),
            "screen": counter("screen"),
            "hardware_concurrency": counter("hw"),
        },
        "duplicates": duplicates,
        "seed_duplicates": seed_dupes,
        "crowded": crowded,
        "risks": risks,
        "summary": {
            "high": sum(1 for r in risks if r["risk"] == "high"),
            "medium": sum(1 for r in risks if r["risk"] == "medium"),
            "clean": len(profiles) - len(risks),
        },
    }


def _feature_key(fp_data: dict, target_os: str) -> tuple:
    """矩阵去重键：(UA, 屏幕, GPU)。UA 版本号全矩阵一致，实际区分靠屏幕+GPU。"""
    from .fingerprint_engine import summarize

    s = summarize(fp_data)
    return (s.get("user_agent") or "", s.get("screen") or "", s.get("webgl_renderer") or "")


def regenerate_risky(profile_ids: list[str]) -> list[dict[str, Any]]:
    """为风险环境重新生成指纹（保持环境身份/Cookie 不变，只换指纹皮肤）。

    矩阵感知：WebGL/屏幕参数池有限，随机重生成可能再次撞上现有环境的
    (UA+屏幕+GPU) 组合。生成时避开已有组合，并偏好不拥挤的 GPU+屏幕配对。
    """
    from .fingerprint_engine import create_fingerprint

    profiles = db.list_profiles()
    existing_keys = {_feature_key(p["fingerprint"], p["target_os"]) for p in profiles}
    from collections import Counter

    crowd = Counter((k[1], k[2]) for k in existing_keys)

    out = []
    for pid in profile_ids:
        p = db.get_profile(pid)
        if not p:
            out.append({"profile_id": pid, "ok": False, "error": "环境不存在"})
            continue
        import json

        mode = (p["fingerprint"] or {}).get("mode", "generate")
        old_key = _feature_key(p["fingerprint"], p["target_os"])
        candidate = None
        for _ in range(8):  # 有界重试：先保证不与现有矩阵精确撞车
            cand = create_fingerprint(p["target_os"], mode)
            key = _feature_key(cand, p["target_os"])
            if key not in existing_keys and crowd[(key[1], key[2])] < 3:
                candidate = cand
                existing_keys.add(key)
                crowd[(key[1], key[2])] += 1
                break
            candidate = candidate or cand  # 兜底：至少用第一次的结果
        db.update_profile(pid, {
            "fingerprint_json": json.dumps(candidate, ensure_ascii=False),
        })
        existing_keys.discard(old_key)
        crowd[(old_key[1], old_key[2])] -= 1
        out.append({"profile_id": pid, "ok": True, "name": p["name"]})
    return out
