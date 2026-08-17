"""Phase 3 专项测试：矩阵风控 / 使用统计 / 团队成员权限 / 双实例同步 / 调度 / Webhook。

前置: 主实例已启动（python run.py，127.0.0.1:18080）。
测试会临时开启 API Key 认证并启动第二个实例（18081，独立数据目录，同步服务器模式）。
"""
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)
ADMIN_KEY = None


def call(method, path, body=None, key="unset"):
    headers = {}
    if key == "unset" and ADMIN_KEY:
        headers["X-API-Key"] = ADMIN_KEY
    elif key:
        headers["X-API-Key"] = key
    return CLIENT.request(method, path, json=body, headers=headers)


def ok(method, path, body=None, key="unset"):
    r = call(method, path, body, key)
    data = r.json()
    assert data["code"] == 0, f"{method} {path} 失败: {data}"
    return data["data"]


def step(n, msg):
    print(f"[{n}] {msg}")


def main() -> int:
    global ADMIN_KEY
    created = []
    task_ids = []
    schedule_ids = []
    node_b = None

    try:
        # 防御：若上次测试中断导致认证残留开启，先读管理员密钥并关闭
        settings_raw = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
        if settings_raw.get("api_key_enabled"):
            ADMIN_KEY = settings_raw["api_key"]
            ok("POST", "/settings", {"api_key_enabled": False})
            ADMIN_KEY = None
            print("（检测到认证残留开启，已重置为关闭）")

        # ======================================================== 矩阵风控
        step(1, "矩阵风控：制造指纹重复 → 扫描 → 重生成 ...")
        p1 = ok("POST", "/profiles", {"name": "P3矩阵A", "group_name": "P3测试",
                                      "launch": {"headless": True}})
        created.append(p1["id"])
        exported = ok("GET", f"/profiles/{p1['id']}/export?include_data=false")
        dup = ok("POST", "/profiles/import", exported)  # 与 p1 指纹完全一致
        created.append(dup["id"])
        report = ok("GET", "/matrix/report")
        high = [r for r in report["risks"] if r["risk"] == "high"]
        assert high, "未检测到高危指纹重复"
        assert {r["profile_id"] for r in high} == {p1["id"], dup["id"]}
        assert report["duplicates"], "重复组为空"
        print(f"      检测到 {len(high)} 个高危重复 ✔ 分布: OS={len(report['distribution']['os'])} GPU={len(report['distribution']['gpu'])}")
        r = ok("POST", "/matrix/regenerate", {"profile_ids": [dup["id"]]})
        assert r[0]["ok"]
        report2 = ok("GET", "/matrix/report")
        assert not [x for x in report2["risks"] if x["profile_id"] == dup["id"]], "重生成后仍有风险"
        print("      重生成后风险清除 ✔")

        # ======================================================== 使用统计
        step(2, "环境使用统计 ...")
        ok("POST", "/browser/start", {"profile_id": p1["id"], "headless": True})
        ok("POST", "/browser/stop", {"profile_id": p1["id"]})
        detail = ok("GET", f"/profiles/{p1['id']}")
        assert detail["start_count"] >= 1 and detail["last_started_at"], detail
        print(f"      启动 {detail['start_count']} 次，最近启动 {detail['last_started_at']} ✔")

        # ======================================================== 团队成员
        step(3, "团队成员：operator 创建/隔离/越权拦截 ...")
        ADMIN_KEY = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))["api_key"]
        ok("POST", "/settings", {"api_key_enabled": True})
        op_name = f"操作员{int(time.time()) % 100000}"
        op = ok("POST", "/members", {"name": op_name, "role": "operator"})
        op_key = op["api_key"]
        op_id = op["id"]
        mine = ok("POST", "/profiles", {"name": "操作员的环境", "launch": {"headless": True}}, key=op_key)
        created.append(mine["id"])
        visible = ok("GET", "/profiles", key=op_key)
        assert [p["id"] for p in visible] == [mine["id"]], "operator 看到了别人的环境!"
        r403 = call("GET", f"/profiles/{p1['id']}", key=op_key)
        assert r403.status_code == 403 and r403.json()["code"] == 403, r403.json()
        r403b = call("GET", "/members", key=op_key)
        assert r403b.status_code == 403
        all_view = ok("GET", "/profiles")  # admin 可见全部
        assert len(all_view) >= 3
        logs = ok("GET", "/audit-logs?limit=100")
        assert any(op_name in l["detail"] for l in logs), "审计未归属到操作员"
        no_key = call("GET", "/profiles", key=None)
        assert no_key.status_code == 401
        print(f"      operator 仅见 1 个自有环境 / 越权 403 / 审计归属 ✔（admin 可见 {len(all_view)} 个）")
        ok("POST", "/settings", {"api_key_enabled": False})
        ok("DELETE", f"/members/{op_id}")
        ADMIN_KEY = None

        # ======================================================== Webhook
        step(4, "Webhook 回调 ...")
        received = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200); self.end_headers()
            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 18999), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        task = ok("POST", "/tasks", {
            "name": "P3钩子任务", "webhook_url": "http://127.0.0.1:18999/hook",
            "steps": [{"action": "navigate", "url": "https://example.com"},
                      {"action": "extract", "selector": "h1"}],
        })
        task_ids.append(task["id"])
        run = ok("POST", f"/tasks/{task['id']}/run", {"profile_ids": [p1["id"]]})
        t0 = time.time()
        while time.time() - t0 < 180 and not received:
            time.sleep(3)
        server.shutdown()
        assert received, "未收到 webhook 回调"
        hook = received[0]
        assert hook["event"] == "task.run.finished" and hook["status"] == "success", hook
        assert hook["steps_ok"] == 2
        print(f"      回调收到 ✔ status={hook['status']} steps_ok={hook['steps_ok']}")

        # ======================================================== 调度
        step(5, "定时调度（每日时刻 = 1 分钟后，等待自动触发）...")
        due = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%H:%M")
        sched = ok("POST", "/schedules", {
            "name": "P3调度", "task_id": task["id"], "kind": "daily", "daily_time": due,
            "profile_ids": [p1["id"]], "headless": True, "auto_close": True,
        })
        schedule_ids.append(sched["id"])
        print(f"      调度时刻 {due}（UTC），等待调度器触发（最长 150 秒）...")
        t0 = time.time()
        run_id = None
        while time.time() - t0 < 150:
            runs = ok("GET", f"/task-runs?task_id={task['id']}&limit=10")
            cand = [r for r in runs if r["started_at"] > sched["created_at"]]
            if cand:
                run_id = cand[0]["id"]
                print(f"      调度自动触发 ✔ run={run_id} 初始状态={cand[0]['status']}")
                break
            time.sleep(5)
        assert run_id, "调度未在预期时间内触发"
        t0 = time.time()
        while time.time() - t0 < 240:
            r = ok("GET", f"/task-runs/{run_id}")
            if r["status"] != "running":
                assert r["status"] == "success", f"调度运行失败: {r}"
                print("      调度运行完成 ✔")
                break
            time.sleep(5)
        else:
            raise TimeoutError("调度运行超时未完成")
        r = ok("POST", f"/schedules/{sched['id']}/run-now")
        assert r["submitted"] >= 1
        print("      手动立即运行 ✔")

        # ======================================================== 双实例同步
        step(6, "双实例同步：节点B（18081，独立数据目录，同步服务器）...")
        home_b = Path("data-node-b").resolve()
        subprocess.run(["powershell", "-Command",
                        f"Remove-Item -Recurse -Force '{home_b}' -ErrorAction SilentlyContinue"])
        env = {**__import__("os").environ, "FPWB_HOME": str(home_b)}
        node_b = subprocess.Popen(
            [r".venv\Scripts\python.exe", "run.py", "--port", "18081", "--sync-server"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        b_ready = False
        for _ in range(30):
            time.sleep(1)
            try:
                if httpx.get("http://127.0.0.1:18081/api/v1/status", timeout=2).status_code == 200:
                    b_ready = True
                    break
            except Exception:
                continue
        assert b_ready, "节点 B 未启动"
        token_b = json.loads((home_b / "data" / "settings.json").read_text(encoding="utf-8"))["sync_token"]

        # A → B 推送
        ok("POST", "/settings", {"sync_remote_url": "http://127.0.0.1:18081",
                                 "sync_remote_token": token_b})
        before = ok("GET", "/profiles")
        push = ok("POST", "/sync/push")
        b_profiles = httpx.get("http://127.0.0.1:18081/api/v1/profiles", timeout=30).json()["data"]
        assert push["created"] == len(before) and len(b_profiles) == len(before), \
            f"推送 {push} / B 有 {len(b_profiles)} / A 有 {len(before)}"
        print(f"      A→B 推送 {push['created']} 个环境 ✔")

        # B 侧指纹一致性抽查
        a_detail = ok("GET", f"/profiles/{p1['id']}")
        b_match = next((x for x in b_profiles if x["name"] == p1["name"]), None)
        assert b_match, "B 侧未找到对应环境"
        assert b_match["fingerprint_summary"]["user_agent"] == a_detail["fingerprint_summary"]["user_agent"], "同步后指纹不一致"
        print("      指纹同步一致 ✔")

        # 删除传播：A 删一个 → 推送 → B 侧同步消失
        ok("DELETE", f"/profiles/{dup['id']}")
        created.remove(dup["id"])
        ok("POST", "/sync/push")
        b_after = httpx.get("http://127.0.0.1:18081/api/v1/profiles", timeout=30).json()["data"]
        assert len(b_after) == len(b_profiles) - 1, f"删除未传播: {len(b_after)}"
        print("      删除传播 ✔")

        # B → A 拉取方向（在 B 建环境，A 拉取）
        httpx.post("http://127.0.0.1:18081/api/v1/profiles", timeout=60,
                   json={"name": "B节点新建", "launch": {"headless": True}}).raise_for_status()
        pull = ok("POST", "/sync/pull")
        a_names = {p["name"] for p in ok("GET", "/profiles")}
        assert "B节点新建" in a_names and pull["created"] >= 1, f"拉取失败: {pull}"
        created += [p["id"] for p in ok("GET", "/profiles") if p["name"] == "B节点新建"]
        print(f"      B→A 拉取 {pull['created']} 个 ✔ 双向同步闭环")

        # ======================================================== fp-chromium 状态
        step(7, "fp-chromium 内核状态报告...")
        st = ok("GET", "/status")
        fpc = st["kernels"]["fp-chromium"]
        assert isinstance(fpc.get("available"), bool), f"状态字段异常: {fpc}"
        if fpc["available"]:
            assert fpc.get("path"), "已安装但未返回路径"
            print("      已安装 ✔（available=True，含内核路径）")
        else:
            assert "releases" in (fpc.get("error") or ""), f"未安装时应给安装指引: {fpc}"
            print("      未安装时给出安装指引 ✔（优雅降级）")

        print("\nPhase 3 全部通过 ✔")
        return 0

    finally:
        # 兜底：确保认证关闭，避免影响后续测试/使用
        try:
            s = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
            if s.get("api_key_enabled"):
                httpx.post(f"{BASE}/settings".replace("/api/v1", "/api/v1"),
                           json={"api_key_enabled": False},
                           headers={"X-API-Key": s["api_key"]}, timeout=30)
        except Exception:
            pass
        for sid in schedule_ids:
            call("DELETE", f"/schedules/{sid}")
        for tid in task_ids:
            call("DELETE", f"/tasks/{tid}")
        for pid in created:
            call("DELETE", f"/profiles/{pid}")
        if node_b:
            node_b.terminate()
            try:
                node_b.wait(timeout=15)
            except Exception:
                node_b.kill()
        subprocess.run(["powershell", "-Command",
                        "Remove-Item -Recurse -Force 'data-node-b' -ErrorAction SilentlyContinue"],
                       capture_output=True)
        print(f"（已清理 {len(created)} 环境 / {len(task_ids)} 任务 / {len(schedule_ids)} 调度 / 节点B）")


if __name__ == "__main__":
    sys.exit(main())
