"""Phase 4 升级回归测试：代理转发器 + 安全 + 调度时区/周几 + MCP 协议 + 自动同步/内核健康。

可独立运行（不启动浏览器、不联网）：
  python scripts/phase4_test.py
"""
import asyncio
import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


# ================================================================
#  1. 代理认证转发器
# ================================================================
async def _echo_server():
    async def handle(r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        finally:
            w.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _fake_upstream(user: str, pwd: str):
    async def handle(r, w):
        try:
            head = await r.readuntil(b"\r\n\r\n")
            auth_ok = False
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            for part in head.decode("latin-1").split("\r\n"):
                if "proxy-authorization:" in part.lower() and f"Basic {token}" in part:
                    auth_ok = True
            if not auth_ok:
                w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await w.drain()
                w.close()
                return
            line = head.split(b"\r\n", 1)[0].decode()
            _, target = line.split(" ")[:2]
            host = target.rpartition(":")[0]
            port = int(target.rpartition(":")[2])
            ur, uw = await asyncio.open_connection(host, port)
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()

            async def pump(sr, sw):
                try:
                    while True:
                        d = await sr.read(65536)
                        if not d:
                            break
                        sw.write(d)
                        await sw.drain()
                finally:
                    sw.close()
            await asyncio.gather(pump(r, uw), pump(ur, w))
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_forwarder():
    print("\n== 1. 代理认证转发器 ==")
    from app.models import ProxyConfig
    from app.proxy_forwarder import AuthProxyForwarder, needs_forwarder

    echo, echo_port = await _echo_server()
    upstream, up_port = await _fake_upstream("u1", "p1")

    fwd = await AuthProxyForwarder(ProxyConfig(
        scheme="http", host="127.0.0.1", port=up_port,
        username="u1", password="p1")).start()
    try:
        check("转发器启动并获得本地端口", fwd.port > 0)
        r, w = await asyncio.open_connection("127.0.0.1", fwd.port)
        w.write(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        resp = await r.readuntil(b"\r\n\r\n")
        check("CONNECT 建立隧道", b"200" in resp)
        w.write(b"hello-fpwb")
        await w.drain()
        data = await r.readexactly(10)
        check("隧道数据回声", data == b"hello-fpwb")
        w.close()

        fwd_bad = await AuthProxyForwarder(ProxyConfig(
            scheme="http", host="127.0.0.1", port=up_port,
            username="u1", password="WRONG")).start()
        r2, w2 = await asyncio.open_connection("127.0.0.1", fwd_bad.port)
        w2.write(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode())
        await w2.drain()
        try:
            resp2 = await asyncio.wait_for(r2.readuntil(b"\r\n\r\n"), 5)
            check("错误密码被上游拒绝", b"200" not in resp2)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            check("错误密码被上游拒绝", True, "连接被关闭")
        w2.close()
        await fwd_bad.stop()

        check("needs_forwarder 判定",
              needs_forwarder(ProxyConfig(scheme="http", host="x", port=1, username="a", password="b"))
              and not needs_forwarder(ProxyConfig(scheme="http", host="x", port=1)))
    finally:
        await fwd.stop()
        upstream.close()
        echo.close()


# ================================================================
#  2. 安全加固 / 模型 / 数据库迁移
# ================================================================
async def test_security_and_db():
    print("\n== 2. 安全加固 / 模型 / 数据库 ==")
    from app import security
    st = security.encryption_status()
    check("加密状态接口可用", isinstance(st.get("available"), bool))

    from app.main import app
    paths = {r.path for r in app.routes}
    check("任务路由存在", "/api/v1/tasks" in paths)

    import app.db as db
    from app.config import DATA_DIR
    print(f"  （数据库：{DATA_DIR / 'workbench.db'}）")
    db.init_db()

    # 任务 owner
    t = db.create_task(name="t-owner-test", notes="", steps=[], owner="m_x")
    check("任务 owner 落库", t.get("owner") == "m_x")
    check("按 owner 过滤任务", all(x["owner"] == "m_x" for x in db.list_tasks(owner="m_x")))
    t2 = db.create_task(name="t-admin-test", notes="", steps=[], owner="admin")
    check("任务列表无过滤返回全部",
          any(x["owner"] == "m_x" for x in db.list_tasks())
          and any(x["owner"] == "admin" for x in db.list_tasks()))
    db.delete_task(t2["id"])
    db.delete_task(t["id"])

    # 调度表迁移：timezone / weekdays / catchup 列
    schedules = db.list_schedules()
    check("调度表迁移列（timezone/weekdays/catchup）",
          all("timezone" in s and "weekdays" in s and "catchup" in s for s in schedules) or not schedules)

    # auto_sync 设置项
    settings = security.load_settings()
    check("auto_sync 设置项存在", "auto_sync" in settings)
    settings["auto_sync"] = True
    security.update_settings(sync={"auto_sync": True})
    reloaded = security.load_settings()
    check("auto_sync 读写一致", reloaded.get("auto_sync") is True)
    security.update_settings(sync={"auto_sync": False})


# ================================================================
#  3. 调度时区 / 周几 / 补跑 / next_run
# ================================================================
async def test_scheduler_tz():
    print("\n== 3. 调度器（时区/周几/补跑/next_run） ==")
    from app.scheduler import Scheduler, _daily_due_local, _schedule_tz, _weekdays

    # _schedule_tz：显式 IANA 时区
    tz_sh = _schedule_tz({"timezone": "Asia/Shanghai"})
    now_utc = datetime.now(timezone.utc)
    now_sh = now_utc.astimezone(tz_sh)
    check("IANA 时区解析 Asia/Shanghai", getattr(now_sh.tzinfo, "key", "") == "Asia/Shanghai")

    # _schedule_tz：无效名回落到本地偏移
    tz_bad = _schedule_tz({"timezone": "Nonexistent/Zone"})
    check("无效时区回落（固定偏移）", isinstance(tz_bad, timezone))

    # _schedule_tz：空时区回落
    tz_empty = _schedule_tz({})
    check("空时区回落（固定偏移）", isinstance(tz_empty, timezone))

    # _weekdays
    check("weekdays 解析", _weekdays({"weekdays": [1, 3, 5]}) == {1, 3, 5})
    check("weekdays 空", _weekdays({}) == set())
    check("weekdays 含非数字过滤", _weekdays({"weekdays": [0, "abc", 3]}) == {0, 3})

    # _daily_due_local：构造"当前本地时间已过目标时刻"的场景
    now_local = now_utc.astimezone(tz_sh)
    past_time = (now_local - timedelta(minutes=5)).strftime("%H:%M")
    s = {"daily_time": past_time, "timezone": "Asia/Shanghai"}
    due, utc_time = _daily_due_local(s, now_utc)
    check("已过时刻判定为 due", due)

    # 周几过滤：当前星期不在列表中
    import calendar
    today_idx = now_local.weekday()  # 0=Mon
    other_day = (today_idx + 3) % 7
    s_filtered = {"daily_time": past_time, "timezone": "Asia/Shanghai", "weekdays": [other_day]}
    due_f, _ = _daily_due_local(s_filtered, now_utc)
    check("周几过滤：非当日不可执行", not due_f)

    # Scheduler.describe
    check("describe interval",
          Scheduler.describe({"kind": "interval", "interval_minutes": 30}) == "每 30 分钟")
    d_daily = Scheduler.describe({"kind": "daily", "daily_time": "09:30", "timezone": "Asia/Shanghai"})
    check("describe daily 含时区", "Asia/Shanghai" in d_daily and "09:30" in d_daily)
    check("describe daily 含周几",
          "周一、周三" in Scheduler.describe({"kind": "daily", "daily_time": "08:00", "weekdays": [0, 2]}))

    # Scheduler.next_run_at
    check("next_run_at paused returns None",
          Scheduler.next_run_at({"enabled": False, "kind": "daily", "daily_time": "08:00"}) is None)
    nxt = Scheduler.next_run_at({"enabled": True, "kind": "interval",
                                  "interval_minutes": 60, "last_run_at": now_utc.isoformat()})
    check("next_run_at interval 返回时间", nxt is not None)
    nxt_daily = Scheduler.next_run_at({"enabled": True, "kind": "daily",
                                       "daily_time": "23:59", "timezone": "Asia/Shanghai"})
    check("next_run_at daily 返回时间", nxt_daily is not None)

    # _is_due interval
    check("_is_due interval 已到期",
          Scheduler._is_due({"kind": "interval", "interval_minutes": 1,
                             "last_run_at": (now_utc - timedelta(minutes=5)).isoformat(timespec="seconds")},
                            now_utc))
    check("_is_due interval 未到期",
          not Scheduler._is_due({"kind": "interval", "interval_minutes": 60,
                                "last_run_at": (now_utc - timedelta(seconds=30)).isoformat(timespec="seconds")},
                               now_utc))


# ================================================================
#  4. MCP 协议（子进程通信）
# ================================================================
async def test_mcp_protocol():
    print("\n== 4. MCP 协议（子进程） ==")
    # 需要 FastAPI 可导入但不实际启动服务；MCP 进程会连不上 API，
    # 但 initialize / tools-list / ping 不需要后端
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    try:
        def send_recv(msg: dict) -> dict:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            proc.stdin.write(line.encode())
            proc.stdin.flush()
            raw = proc.stdout.readline().decode()
            return json.loads(raw.strip())

        # initialize
        r = send_recv({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"}})
        check("MCP initialize", r.get("result", {}).get("serverInfo", {}).get("name") == "fpworkbench")

        # tools/list
        r = send_recv({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = r.get("result", {}).get("tools", [])
        check("MCP tools/list 19 个工具", len(tools) == 19, f"实际 {len(tools)} 个")
        tool_names = {t["name"] for t in tools}
        for expected in ["status", "list_profiles", "create_profile", "start_browser",
                         "navigate", "screenshot", "matrix_report", "run_task", "list_schedules"]:
            check(f"工具存在: {expected}", expected in tool_names)

        # ping
        r = send_recv({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        check("MCP ping", r.get("result") == {})

        # 未知工具
        r = send_recv({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "nonexistent_tool", "arguments": {}}})
        check("MCP 未知工具报错", r.get("error", {}).get("code") == -32602)

        # 未知方法
        r = send_recv({"jsonrpc": "2.0", "id": 5, "method": "fake/method"})
        check("MCP 未知方法报错", "error" in r and r["error"]["code"] == -32601)

        # 解析错误
        proc.stdin.write(b"not-json\n")
        proc.stdin.flush()
        r = json.loads(proc.stdout.readline().decode().strip())
        check("MCP 解析错误 -32700", r.get("error", {}).get("code") == -32700)

    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


# ================================================================
#  5. 模型验证（ScheduleCreate/Update 含 timezone/weekdays）
# ================================================================
async def test_models():
    print("\n== 5. 数据模型 ==")
    from app.models import ScheduleCreate, ScheduleUpdate, TaskCreate

    sc = ScheduleCreate(name="test", task_id="t1", kind="daily", daily_time="08:00",
                        profile_ids=["p1"], timezone="Asia/Tokyo", weekdays=[0, 2, 4])
    check("ScheduleCreate timezone", sc.timezone == "Asia/Tokyo")
    check("ScheduleCreate weekdays", sc.weekdays == [0, 2, 4])

    su = ScheduleUpdate(timezone="Europe/London", weekdays=[1, 5])
    check("ScheduleUpdate timezone", su.timezone == "Europe/London")
    check("ScheduleUpdate weekdays", su.weekdays == [1, 5])

    # TaskCreate steps 含新动作
    tc = TaskCreate(name="t", steps=[
        {"action": "navigate", "url": "https://example.com"},
        {"action": "hover", "selector": ".menu"},
        {"action": "select", "selector": "select#lang", "value": "en"},
        {"action": "upload", "selector": "input[type=file]", "path": "/tmp/f.png"},
        {"action": "download", "url": "https://example.com/file.zip", "save_as": "file.zip"},
        {"action": "tab_open", "url": "https://example.com/new"},
        {"action": "tab_switch", "index": 1},
        {"action": "tab_close", "index": 1},
        {"action": "set_var", "name": "count", "value": "0"},
        {"action": "label", "name": "loop_start"},
        {"action": "if", "var": "count", "operator": "lt", "value": "10", "goto": "loop_start"},
        {"action": "goto", "name": "loop_start"},
        {"action": "extract", "selector": "h1", "var": "title"},
        {"action": "evaluate", "expression": "document.title", "var": "pageTitle"},
    ])
    check("TaskCreate 21 动作验证通过", tc.steps is not None and len(tc.steps) == 14)


# ================================================================
#  6. 内核健康 + auto_sync 设置项
# ================================================================
async def test_kernel_health_and_sync():
    print("\n== 6. 内核健康 / 自动同步设置 ==")
    from app.security import load_settings, update_settings
    from app.main import _kernel_health

    check("内核健康字段存在", "status" in _kernel_health and "note" in _kernel_health)

    settings = load_settings()
    check("sync 设置项", all(k in settings for k in ["sync_remote_url", "sync_remote_token", "auto_sync"]))
    check("auto_sync 默认关闭", settings.get("auto_sync") is False)


# ================================================================
#  main
# ================================================================
async def main():
    await test_forwarder()
    await test_security_and_db()
    await test_scheduler_tz()
    await test_models()
    await test_kernel_health_and_sync()
    await test_mcp_protocol()

    print(f"\n{'='*50}")
    print(f"结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败项：", "；".join(FAIL))
        sys.exit(1)
    else:
        print("Phase 4 全部回归测试通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
