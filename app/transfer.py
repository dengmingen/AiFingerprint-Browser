"""环境的导入/导出与整机备份恢复。

导出格式（可移植 JSON）：配置 + 指纹 + 可选的用户数据目录 zip（base64）。
整机备份：全部环境的配置与指纹（不含数据目录，体积可控）。
注意：导出内容包含代理密码明文与 Cookie（含 data_archive 时），文件需妥善保管。
"""
import base64
import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import db
from .config import PROFILE_ROOT
from .fingerprint_engine import create_fingerprint

FORMAT_VERSION = 1


def export_profile(profile: dict, include_data: bool = False) -> dict:
    out = {
        "format_version": FORMAT_VERSION,
        "profile": {
            "name": profile["name"],
            "group_name": profile["group_name"],
            "notes": profile["notes"],
            "kernel": profile["kernel"],
            "target_os": profile["target_os"],
            "proxy": profile["proxy"],
            "launch": profile["launch"],
            "fingerprint": profile["fingerprint"],
        },
    }
    if include_data:
        user_dir = PROFILE_ROOT / profile["id"]
        if user_dir.exists():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in user_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(user_dir).as_posix())
            out["data_archive"] = base64.b64encode(buf.getvalue()).decode()
    return out


def import_profile(data: dict, name_override: str | None = None,
                   owner: str = "admin") -> dict:
    """从导出数据创建新环境（生成新 id；数据目录归档可选恢复）。"""
    profile = data["profile"]
    # 指纹缺失或格式不符时重新生成，避免导入旧版/外部数据崩溃
    fingerprint = profile.get("fingerprint")
    if not isinstance(fingerprint, dict) or not fingerprint.get("seeds"):
        fingerprint = create_fingerprint(profile.get("target_os", "windows"))
    imported = db.create_profile(
        name=(name_override or profile.get("name") or "导入的环境"),
        group_name=profile.get("group_name") or "导入",
        notes=profile.get("notes") or "",
        kernel=profile.get("kernel") or "camoufox",
        target_os=profile.get("target_os") or "windows",
        proxy=profile.get("proxy"),
        fingerprint=fingerprint,
        launch=profile.get("launch") or {"start_url": "about:blank"},
        owner=owner,
    )
    archive = data.get("data_archive")
    if archive:
        try:
            user_dir = PROFILE_ROOT / imported["id"]
            user_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(archive))) as zf:
                zf.extractall(user_dir)
        except Exception:
            shutil.rmtree(PROFILE_ROOT / imported["id"], ignore_errors=True)
            db.delete_profile(imported["id"])
            raise ValueError("数据目录归档损坏，导入已回滚")
    return imported


def backup_all() -> dict:
    profiles = db.list_profiles()
    return {
        "format_version": FORMAT_VERSION,
        "kind": "fpworkbench-backup",
        "count": len(profiles),
        "created_at": profiles[0]["created_at"] if profiles else None,
        "profiles": [
            {"name": p["name"], "group_name": p["group_name"], "notes": p["notes"],
             "kernel": p["kernel"], "target_os": p["target_os"], "proxy": p["proxy"],
             "launch": p["launch"], "fingerprint": p["fingerprint"]}
            for p in profiles
        ],
    }


def restore_all(backup: dict, *, wipe: bool = True) -> int:
    """整机恢复：默认清空现有环境后按备份重建（数据目录不恢复，仅配置+指纹）。"""
    if backup.get("kind") != "fpworkbench-backup":
        raise ValueError("不是有效的整机备份文件")
    if wipe:
        for p in db.list_profiles():
            db.delete_profile(p["id"])
            shutil.rmtree(PROFILE_ROOT / p["id"], ignore_errors=True)
    count = 0
    for item in backup.get("profiles", []):
        import_profile({"format_version": FORMAT_VERSION, "profile": item})
        count += 1
    return count
