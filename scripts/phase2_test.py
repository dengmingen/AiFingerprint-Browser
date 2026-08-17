"""Phase 2 专项测试：批量/导入导出/任务引擎/加密/认证/备份/审计 全链路验证。

前置: 服务已启动（python run.py）。流程全部自动化，结束时清理测试数据。
"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)
KEY = None


def call(method: str, path: str, body=None, use_key=True):
    headers = {}
    if KEY and use_key:
        headers["X-API-Key"] = KEY
    r = CLIENT.request(method, path, json=body, headers=headers)
    return r


def ok(method: str, path: str, body=None):
    r = call(method, path, body)
    data = r.json()
    assert data["code"] == 0, f"{method} {path} 失败: {data}"
    return data["data"]


def step(n, msg):
    print(f"[{n}] {msg}")


def wait_run(run_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = ok("GET", f"/task-runs/{run_id}")
        if r["status"] != "running":
            return r
        time.sleep(3)
    raise TimeoutError(f"run {run_id} 超时")


def main() -> int:
    global KEY
    created = []
    task_ids = []

    try:
        # 防御：若历史测试中断导致认证残留开启，先关闭
        import json as _json
        from pathlib import Path as _Path
        _settings_path = _Path("data/settings.json")
        if _settings_path.exists():
            _s = _json.loads(_settings_path.read_text(encoding="utf-8"))
            if _s.get("api_key_enabled"):
                httpx.post(f"{BASE}/settings", json={"api_key_enabled": False},
                           headers={"X-API-Key": _s["api_key"]}, timeout=30)
                print("（检测到认证残留开启，已重置为关闭）")

        step(1, "批量创建 3 个环境（合成模式 + 代理密码加密）...")
        batch = ok("POST", "/profiles/batch", {
            "count": 3,
            "template": {
                "name": "P2批量", "group_name": "P2测试", "kernel": "camoufox",
                "target_os": "windows", "fingerprint_mode": "generate",
                "proxy": {"scheme": "http", "host": "127.0.0.1", "port": 8080,
                          "username": "u1", "password": "secret-pw-123"},
                "launch": {"headless": True},
            },
        })
        assert batch["count"] == 3
        created += [p["id"] for p in batch["profiles"]]
        print(f"      创建 {batch['count']} 个；健康分: {[p['fingerprint_summary']['health']['score'] for p in batch['profiles']]}")

        step(2, "代理密码已加密落库（数据库层检查 enc: 前缀）...")
        import sqlite3
        conn = sqlite3.connect("data/fpworkbench.db")
        raw = conn.execute("SELECT proxy_json FROM profiles WHERE id=?", (created[0],)).fetchone()[0]
        conn.close()
        assert '"enc:' in raw, f"密码未加密: {raw[:80]}"
        assert "secret-pw-123" not in raw, "明文密码泄露到数据库!"
        print("      密码以 enc: 加密存储 ✔")

        step(3, "真实预设模式创建环境...")
        preset = ok("POST", "/profiles", {
            "name": "P2预设环境", "kernel": "camoufox", "target_os": "windows",
            "fingerprint_mode": "preset", "launch": {"headless": True},
        })
        created.append(preset["id"])
        s = preset["fingerprint_summary"]
        assert s["mode"] == "真实预设", s
        assert s["health"]["score"] >= 70, s["health"]
        print(f"      模式={s['mode']} 健康分={s['health']['score']} UA={s['user_agent'][:40]}...")

        step(4, "导出（含数据目录）→ 删除 → 导入回滚验证...")
        ok("POST", "/browser/start", {"profile_id": preset["id"], "headless": True})
        ok("POST", "/browser/stop", {"profile_id": preset["id"]})
        exported = ok("GET", f"/profiles/{preset['id']}/export?include_data=true")
        assert exported["data_archive"], "导出未包含数据目录"
        ok("DELETE", f"/profiles/{preset['id']}")
        created.remove(preset["id"])
        imported = ok("POST", "/profiles/import", exported)
        created.append(imported["id"])
        a = json.dumps(exported["profile"]["fingerprint"], sort_keys=True)
        b = json.dumps(imported["fingerprint"], sort_keys=True)
        assert a == b, "导入后指纹与导出时不一致"
        print(f"      导入指纹一致 ✔ (UA={imported['fingerprint_summary']['user_agent'][:40]}...)")

        step(5, "RPA 任务：创建 → 运行 → 结果与截图验证...")
        # 任务跑在无代理的专用环境上（批量环境配的是假代理，仅用于加密验证）
        task_profile = ok("POST", "/profiles", {
            "name": "P2任务环境", "group_name": "P2测试",
            "launch": {"headless": True},
        })
        created.append(task_profile["id"])
        task = ok("POST", "/tasks", {
            "name": "P2测试任务",
            "steps": [
                {"action": "navigate", "url": "https://example.com"},
                {"action": "wait_for", "selector": "h1", "timeout": 30000},
                {"action": "extract", "selector": "h1"},
                {"action": "screenshot", "name": "final"},
                {"action": "evaluate", "expression": "document.title"},
            ],
        })
        task_ids.append(task["id"])
        run = ok("POST", f"/tasks/{task['id']}/run", {"profile_ids": [task_profile["id"]]})
        result = wait_run(run["run_ids"][0])
        assert result["status"] == "success", f"任务运行失败: {result}"
        steps = result["results"]
        assert len(steps) == 5 and all(s["status"] == "ok" for s in steps), steps
        assert steps[2]["extracted"] == ["Example Domain"], steps[2]
        assert steps[3]["screenshot"].startswith("/runs/"), steps[3]
        assert steps[4]["value"] == "Example Domain", steps[4]
        print(f"      5 步全部成功 ✔ 抽取={steps[2]['extracted']} 截图={steps[3]['screenshot']}")

        step(6, "批量启动 / 批量停止（无代理环境）...")
        batch_ids = [imported["id"], task_profile["id"]]
        r = ok("POST", "/browser/start-batch", {"profile_ids": batch_ids})
        assert r["started"] == 2, r
        r = ok("POST", "/browser/stop-batch", {"profile_ids": batch_ids})
        assert r["stopped"] == 2, r
        print("      2 启动 2 停止 ✔")

        step(7, "API Key 认证：开启 → 无 key 拒绝 → 带 key 放行 → 关闭 ...")
        ok("POST", "/settings", {"api_key_enabled": True})
        # 成员体系下：开启认证不再回传密钥，从设置文件取管理员密钥
        import json as _json
        from pathlib import Path as _Path
        KEY = _json.loads(_Path("data/settings.json").read_text(encoding="utf-8"))["api_key"]
        no_key = call("GET", "/profiles", use_key=False)
        assert no_key.status_code == 401 and no_key.json()["code"] == 40100, no_key.json()
        with_key = call("GET", "/profiles")
        assert with_key.json()["code"] == 0
        ok("POST", "/settings", {"api_key_enabled": False})
        KEY = None
        print("      401/放行/关闭 全部符合预期 ✔")

        step(8, "整机备份 → 变更 → 恢复 ...")
        backup = ok("GET", "/system/backup")
        base_count = backup["count"]
        assert base_count >= 4, backup["count"]
        extra = ok("POST", "/profiles", {"name": "P2临时", "launch": {"headless": True}})
        created.append(extra["id"])
        r = ok("POST", "/system/restore", backup)
        assert r["restored"] == base_count, r
        current = ok("GET", "/profiles")
        assert len(current) == base_count, f"恢复后环境数 {len(current)} != {base_count}"
        created = [p["id"] for p in current if p["group_name"] == "P2测试" or p["name"] == "P2临时"]
        print(f"      备份 {base_count} 个 → 恢复 {r['restored']} 个 ✔")

        step(9, "审计日志完整性...")
        logs = ok("GET", "/audit-logs?limit=200")
        actions = {l["action"] for l in logs}
        expected = {"profile.create", "profile.batch_create", "profile.export", "profile.import",
                    "task.create", "task.run", "browser.start", "browser.stop",
                    "browser.start_batch", "browser.stop_batch", "system.backup",
                    "system.restore", "settings.update", "auth.denied"}
        missing = expected - actions
        assert not missing, f"审计缺失: {missing}（现有: {sorted(actions)}）"
        denied = [l for l in logs if l["action"] == "auth.denied"]
        assert denied, "未记录认证拒绝事件"
        print(f"      14 类动作全部记录 ✔（含 auth.denied {len(denied)} 次）")

        print("\nPhase 2 全部通过 ✔")
        return 0

    finally:
        # 兜底：确保认证关闭
        try:
            import json as _json
            from pathlib import Path as _Path
            _s = _json.loads(_Path("data/settings.json").read_text(encoding="utf-8"))
            if _s.get("api_key_enabled"):
                httpx.post(f"{BASE}/settings", json={"api_key_enabled": False},
                           headers={"X-API-Key": _s["api_key"]}, timeout=30)
        except Exception:
            pass
        for tid in task_ids:
            call("DELETE", f"/tasks/{tid}")
        for pid in created:
            call("DELETE", f"/profiles/{pid}")
        print(f"（已清理 {len(created)} 个测试环境 / {len(task_ids)} 个任务）")


if __name__ == "__main__":
    sys.exit(main())
