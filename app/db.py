"""SQLite 数据层：环境（profile）的增删改查。

一张 profiles 表；代理、指纹、启动配置以 JSON 字符串列存储，
读写时在 dict 与模型之间转换。
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .config import DB_PATH, ensure_dirs

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    group_name       TEXT NOT NULL DEFAULT '默认分组',
    notes            TEXT NOT NULL DEFAULT '',
    kernel           TEXT NOT NULL DEFAULT 'camoufox',
    target_os        TEXT NOT NULL DEFAULT 'windows',
    proxy_json       TEXT,
    fingerprint_json TEXT NOT NULL,
    launch_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_group ON profiles(group_name);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    steps_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    task_name     TEXT NOT NULL,
    profile_id    TEXT NOT NULL,
    profile_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    error        TEXT,
    results_json TEXT NOT NULL DEFAULT '[]',
    started_at    TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_profile ON task_runs(profile_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);

CREATE TABLE IF NOT EXISTS members (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'operator',
    api_key     TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'daily',
    interval_minutes  INTEGER,
    daily_time        TEXT,
    profile_ids_json  TEXT NOT NULL DEFAULT '[]',
    headless          INTEGER NOT NULL DEFAULT 1,
    auto_close        INTEGER NOT NULL DEFAULT 1,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_run_at       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_deletes (
    sync_id     TEXT PRIMARY KEY,
    deleted_at  TEXT NOT NULL
);
"""

AUDIT_KEEP = 5000

# 旧库升级：profiles/tasks 表增量列（存在则跳过）
_MIGRATIONS = [
    ("profiles", "start_count", "INTEGER NOT NULL DEFAULT 0"),
    ("profiles", "last_started_at", "TEXT"),
    ("profiles", "sync_id", "TEXT"),
    ("profiles", "rev", "TEXT"),
    ("profiles", "owner", "TEXT NOT NULL DEFAULT 'admin'"),
    ("tasks", "webhook_url", "TEXT"),
    ("tasks", "owner", "TEXT NOT NULL DEFAULT 'admin'"),
    ("schedules", "timezone", "TEXT"),
    ("schedules", "weekdays_json", "TEXT"),
    ("schedules", "catchup", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(profiles)")}
    for table, column, decl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # 存量环境补发稳定的同步 ID
    for (pid,) in conn.execute("SELECT id FROM profiles WHERE sync_id IS NULL"):
        conn.execute(
            "UPDATE profiles SET sync_id = ?, rev = ? WHERE id = ?",
            (uuid.uuid4().hex, _now(), pid),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    global _conn
    ensure_dirs()
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    with _lock, _conn:
        _conn.executescript(_SCHEMA)
        _migrate(_conn)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


def row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    from .security import decrypt_proxy

    d = dict(row)
    d["proxy"] = decrypt_proxy(json.loads(d.pop("proxy_json") or "null"))
    d["fingerprint"] = json.loads(d.pop("fingerprint_json"))
    d["launch"] = json.loads(d.pop("launch_json"))
    return d


def _profile_values(profile_id: str, name: str, group_name: str, notes: str,
                    kernel: str, target_os: str, proxy_json: str | None,
                    fingerprint_json: str, launch_json: str, now: str) -> tuple:
    from .security import encrypt_proxy

    proxy = json.loads(proxy_json) if proxy_json else None
    return (
        profile_id, name, group_name, notes, kernel, target_os,
        json.dumps(encrypt_proxy(proxy), ensure_ascii=False) if proxy else None,
        fingerprint_json, launch_json, now, now,
    )


def create_profile(
    *,
    name: str,
    group_name: str,
    notes: str,
    kernel: str,
    target_os: str,
    proxy: Optional[dict],
    fingerprint: dict,
    launch: dict,
    owner: str = "admin",
) -> dict[str, Any]:
    conn = _require_conn()
    profile_id = uuid.uuid4().hex
    now = _now()
    with _lock, conn:
        conn.execute(
            """INSERT INTO profiles
               (id, name, group_name, notes, kernel, target_os,
                proxy_json, fingerprint_json, launch_json, created_at, updated_at,
                sync_id, rev, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _profile_values(profile_id, name, group_name, notes, kernel,
                            target_os,
                            json.dumps(proxy, ensure_ascii=False) if proxy else None,
                            json.dumps(fingerprint, ensure_ascii=False),
                            json.dumps(launch, ensure_ascii=False), now)
            + (uuid.uuid4().hex, now, owner),
        )
    return get_profile(profile_id)  # type: ignore[return-value]


def get_profile(profile_id: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return row_to_profile(row) if row else None


def list_profiles(group: Optional[str] = None,
                  owner: Optional[str] = None) -> list[dict[str, Any]]:
    conn = _require_conn()
    query = "SELECT * FROM profiles"
    conds, params = [], []
    if group:
        conds.append("group_name = ?"); params.append(group)
    if owner is not None:  # 成员数据隔离：operator 只能看到自己的环境
        conds.append("owner = ?"); params.append(owner)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY created_at DESC"
    with _lock:
        rows = conn.execute(query, params).fetchall()
    return [row_to_profile(r) for r in rows]


def update_profile(
    profile_id: str,
    updates: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """updates 的键必须是 profiles 表的列名，值已序列化完成。"""
    from .security import encrypt_proxy

    if not updates:
        return get_profile(profile_id)
    if "proxy_json" in updates and updates["proxy_json"]:
        proxy = json.loads(updates["proxy_json"])
        updates["proxy_json"] = json.dumps(encrypt_proxy(proxy), ensure_ascii=False)
    conn = _require_conn()
    updates = {**updates, "updated_at": _now()}
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _lock, conn:
        cur = conn.execute(
            f"UPDATE profiles SET {set_clause} WHERE id = ?",
            (*updates.values(), profile_id),
        )
        if cur.rowcount == 0:
            return None
    return get_profile(profile_id)


def delete_profile(profile_id: str) -> bool:
    conn = _require_conn()
    with _lock, conn:
        row = conn.execute(
            "SELECT sync_id FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        if cur.rowcount and row and row["sync_id"]:
            # 记录删除墓碑，供同步时传播删除
            conn.execute(
                "INSERT OR REPLACE INTO sync_deletes (sync_id, deleted_at) VALUES (?, ?)",
                (row["sync_id"], _now()),
            )
    return cur.rowcount > 0


def bump_usage(profile_id: str) -> None:
    """启动计数与最近启动时间（环境使用统计）。"""
    conn = _require_conn()
    with _lock, conn:
        conn.execute(
            """UPDATE profiles SET start_count = start_count + 1,
               last_started_at = ? WHERE id = ?""",
            (_now(), profile_id),
        )


# ---------------------------------------------------------------- 同步（按 sync_id 上升降级 upsert）

def get_profile_by_sync_id(sync_id: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM profiles WHERE sync_id = ?", (sync_id,)
        ).fetchone()
    return row_to_profile(row) if row else None


def upsert_profile_by_sync(*, sync_id: str, rev: str, name: str, group_name: str,
                           notes: str, kernel: str, target_os: str,
                           proxy: Optional[dict], fingerprint: dict,
                           launch: dict, owner: str = "admin") -> str:
    """LWW upsert：远端 rev 更新时才覆盖本地。返回 'updated'/'created'/'skipped'。"""
    from .security import encrypt_proxy

    conn = _require_conn()
    local = get_profile_by_sync_id(sync_id)
    if local and (local.get("rev") or "") >= rev:
        return "skipped"
    with _lock, conn:
        if local:
            conn.execute(
                """UPDATE profiles SET name=?, group_name=?, notes=?, kernel=?, target_os=?,
                   proxy_json=?, fingerprint_json=?, launch_json=?, updated_at=?, rev=?,
                   owner=? WHERE sync_id=?""",
                (name, group_name, notes, kernel, target_os,
                 json.dumps(encrypt_proxy(proxy), ensure_ascii=False) if proxy else None,
                 json.dumps(fingerprint, ensure_ascii=False),
                 json.dumps(launch, ensure_ascii=False), rev, rev, owner, sync_id),
            )
            return "updated"
        conn.execute(
            """INSERT INTO profiles (id, name, group_name, notes, kernel, target_os,
               proxy_json, fingerprint_json, launch_json, created_at, updated_at,
               sync_id, rev, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _profile_values(uuid.uuid4().hex, name, group_name, notes, kernel,
                            target_os,
                            json.dumps(proxy, ensure_ascii=False) if proxy else None,
                            json.dumps(fingerprint, ensure_ascii=False),
                            json.dumps(launch, ensure_ascii=False), rev)
            + (sync_id, rev, owner),
        )
        conn.execute("DELETE FROM sync_deletes WHERE sync_id = ?", (sync_id,))
    return "created"


def list_sync_deletes(since: Optional[str] = None) -> list[dict[str, Any]]:
    conn = _require_conn()
    query, params = "SELECT * FROM sync_deletes", []
    if since:
        query += " WHERE deleted_at > ?"
        params.append(since)
    with _lock:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def clear_sync_delete(sync_id: str) -> None:
    conn = _require_conn()
    with _lock, conn:
        conn.execute("DELETE FROM sync_deletes WHERE sync_id = ?", (sync_id,))


# ---------------------------------------------------------------- RPA 任务

def create_task(*, name: str, notes: str, steps: list,
                webhook_url: str | None = None, owner: str = "admin") -> dict[str, Any]:
    conn = _require_conn()
    task_id = uuid.uuid4().hex
    now = _now()
    with _lock, conn:
        conn.execute(
            "INSERT INTO tasks (id, name, notes, steps_json, webhook_url, owner, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, name, notes, json.dumps(steps, ensure_ascii=False), webhook_url, owner, now, now),
        )
    return get_task(task_id)  # type: ignore[return-value]


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["steps"] = json.loads(d.pop("steps_json"))
    return d


def list_tasks(owner: Optional[str] = None) -> list[dict[str, Any]]:
    conn = _require_conn()
    query, params = "SELECT * FROM tasks", []
    if owner is not None:  # 成员数据隔离：operator 只能看到自己的任务
        query += " WHERE owner = ?"
        params.append(owner)
    query += " ORDER BY updated_at DESC"
    with _lock:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["steps"] = json.loads(d.pop("steps_json"))
        out.append(d)
    return out


def update_task(task_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    sets = {**updates, "updated_at": _now()}
    if "steps" in sets:
        sets["steps_json"] = json.dumps(sets.pop("steps"), ensure_ascii=False)
    set_clause = ", ".join(f"{k} = ?" for k in sets)
    with _lock, conn:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", (*sets.values(), task_id)
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def delete_task(task_id: str) -> bool:
    conn = _require_conn()
    with _lock, conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 任务运行记录

def create_run(*, task_id: str, task_name: str, profile_id: str, profile_name: str) -> dict[str, Any]:
    conn = _require_conn()
    run_id = uuid.uuid4().hex[:12]
    with _lock, conn:
        conn.execute(
            """INSERT INTO task_runs (id, task_id, task_name, profile_id, profile_name,
               status, results_json, started_at) VALUES (?, ?, ?, ?, ?, 'running', '[]', ?)""",
            (run_id, task_id, task_name, profile_id, profile_name, _now()),
        )
    return get_run(run_id)  # type: ignore[return-value]


def update_run(run_id: str, updates: dict[str, Any]) -> None:
    conn = _require_conn()
    updates = dict(updates)
    if "results" in updates:
        updates["results_json"] = json.dumps(updates.pop("results"), ensure_ascii=False)
    if updates.pop("finished", False):
        updates["finished_at"] = _now()
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _lock, conn:
        conn.execute(f"UPDATE task_runs SET {set_clause} WHERE id = ?", (*updates.values(), run_id))


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["results"] = json.loads(d.pop("results_json"))
    return d


def list_runs(task_id: str | None = None, profile_id: str | None = None,
              limit: int = 50) -> list[dict[str, Any]]:
    conn = _require_conn()
    query, params = "SELECT * FROM task_runs", []
    conds = []
    if task_id:
        conds.append("task_id = ?"); params.append(task_id)
    if profile_id:
        conds.append("profile_id = ?"); params.append(profile_id)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["results"] = json.loads(d.pop("results_json"))
        out.append(d)
    return out


# ---------------------------------------------------------------- 审计日志

def audit_log(action: str, target: str = "", detail: str = "", result: str = "ok") -> None:
    conn = _require_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO audit_logs (ts, action, target, detail, result) VALUES (?, ?, ?, ?, ?)",
            (_now(), action, target[:200], detail[:500], result),
        )
        conn.execute(
            "DELETE FROM audit_logs WHERE seq <= (SELECT MAX(seq) FROM audit_logs) - ?",
            (AUDIT_KEEP,),
        )


def list_audit_logs(action: str | None = None, limit: int = 100,
                    offset: int = 0) -> list[dict[str, Any]]:
    conn = _require_conn()
    query, params = "SELECT * FROM audit_logs", []
    if action:
        query += " WHERE action LIKE ?"
        params.append(f"%{action}%")
    query += " ORDER BY seq DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _lock:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 团队成员

def ensure_default_member() -> None:
    """首次运行时创建默认管理员（密钥沿用系统设置的 API Key）。"""
    from .security import load_settings

    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT 1 FROM members LIMIT 1").fetchone()
        if row:
            return
        key = load_settings().get("api_key", "")
        conn.execute(
            "INSERT INTO members (id, name, role, api_key, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            ("admin", "管理员", "admin", key, _now()),
        )


def create_member(*, name: str, role: str, api_key: str) -> dict[str, Any]:
    conn = _require_conn()
    member_id = f"m_{uuid.uuid4().hex[:10]}"
    with _lock, conn:
        conn.execute(
            "INSERT INTO members (id, name, role, api_key, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (member_id, name, role, api_key, _now()),
        )
    return get_member_by_key(api_key)  # type: ignore[return-value]


def get_member_by_key(api_key: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM members WHERE api_key = ? AND enabled = 1", (api_key,)
        ).fetchone()
    return dict(row) if row else None


def list_members() -> list[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        rows = conn.execute("SELECT * FROM members ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["api_key_masked"] = _mask_key(d.pop("api_key"))
        out.append(d)
    return out


def _mask_key(key: str) -> str:
    return key[:4] + "****" + key[-4:] if len(key) >= 8 else "****"


def set_member_enabled(member_id: str, enabled: bool) -> bool:
    conn = _require_conn()
    with _lock, conn:
        cur = conn.execute(
            "UPDATE members SET enabled = ? WHERE id = ?", (1 if enabled else 0, member_id)
        )
    return cur.rowcount > 0


def delete_member(member_id: str) -> bool:
    conn = _require_conn()
    with _lock:
        # 不允许删掉最后一个管理员
        admins = conn.execute(
            "SELECT COUNT(*) AS c FROM members WHERE role='admin' AND enabled=1"
        ).fetchone()["c"]
        row = conn.execute("SELECT role FROM members WHERE id=?", (member_id,)).fetchone()
        if row and row["role"] == "admin" and admins <= 1:
            return False
        cur = conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 定时调度

def create_schedule(*, name: str, task_id: str, kind: str, interval_minutes: Optional[int],
                    daily_time: Optional[str], profile_ids: list, headless: bool,
                    auto_close: bool, timezone: Optional[str] = None,
                    weekdays: Optional[list] = None) -> dict[str, Any]:
    conn = _require_conn()
    sid = uuid.uuid4().hex[:12]
    now = _now()
    with _lock, conn:
        conn.execute(
            """INSERT INTO schedules (id, name, task_id, kind, interval_minutes, daily_time,
               profile_ids_json, headless, auto_close, enabled, timezone, weekdays_json,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (sid, name, task_id, kind, interval_minutes, daily_time,
             json.dumps(profile_ids), int(headless), int(auto_close),
             timezone, json.dumps([w for w in (weekdays or []) if 0 <= int(w) <= 6]),
             now, now),
        )
    return get_schedule(sid)  # type: ignore[return-value]


def get_schedule(sid: str) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    return _schedule_row(row) if row else None


def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["profile_ids"] = json.loads(d.pop("profile_ids_json"))
    try:
        d["weekdays"] = json.loads(d.pop("weekdays_json") or "[]")
    except (ValueError, KeyError):
        d["weekdays"] = []
    d["headless"] = bool(d.pop("headless"))
    d["auto_close"] = bool(d.pop("auto_close"))
    d["enabled"] = bool(d.pop("enabled"))
    return d


def list_schedules() -> list[dict[str, Any]]:
    conn = _require_conn()
    with _lock:
        rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    return [_schedule_row(r) for r in rows]


def update_schedule(sid: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    conn = _require_conn()
    sets = {**updates, "updated_at": _now()}
    if "profile_ids" in sets:
        sets["profile_ids_json"] = json.dumps(sets.pop("profile_ids"))
    if "weekdays" in sets:
        sets["weekdays_json"] = json.dumps(
            [w for w in (sets.pop("weekdays") or []) if 0 <= int(w) <= 6])
    for bool_field in ("headless", "auto_close", "enabled"):
        if bool_field in sets:
            sets[bool_field] = int(sets[bool_field])
    set_clause = ", ".join(f"{k} = ?" for k in sets)
    with _lock, conn:
        cur = conn.execute(
            f"UPDATE schedules SET {set_clause} WHERE id = ?", (*sets.values(), sid)
        )
        if cur.rowcount == 0:
            return None
    return get_schedule(sid)


def delete_schedule(sid: str) -> bool:
    conn = _require_conn()
    with _lock, conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))
    return cur.rowcount > 0
