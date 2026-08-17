"""风控环境优化专项测试：预设生效 / 就绪度体检 / 人机化节奏 / 预热模板。

前置: 主实例已运行（127.0.0.1:18080）。
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)


def ok(method, path, body=None):
    r = CLIENT.request(method, path, json=body)
    data = r.json()
    assert data["code"] == 0, f"{method} {path} 失败: {data}"
    return data["data"]


def step(n, msg):
    print(f"[{n}] {msg}")


async def cdp_eval(debug_port: int, js: str):
    async with httpx.AsyncClient() as c:
        targets = (await c.get(f"http://127.0.0.1:{debug_port}/json")).json()
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8 << 20) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "returnByValue": True}}))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if resp.get("id") == 1:
                return resp["result"]["result"]["value"]


ENV_INFO_JS = ("(() => ({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
               " lang: navigator.language }))()")


def wait_run(run_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = ok("GET", f"/task-runs/{run_id}")
        if r["status"] != "running":
            return r
        time.sleep(2)
    raise TimeoutError(run_id)


def main() -> int:
    created = []
    task_ids = []
    try:
        step(1, "国内风控预设（china）：zh-CN + Asia/Shanghai 生效验证（camoufox）...")
        p = ok("POST", "/profiles", {
            "name": "RC国内预设", "kernel": "camoufox", "target_os": "windows",
            "launch": {"preset": "china", "headless": True},
        })
        created.append(p["id"])
        ok("POST", "/browser/start", {"profile_id": p["id"]})
        info = ok("POST", f"/browser/{p['id']}/evaluate", {
            "expression": ENV_INFO_JS})["result"]
        ok("POST", "/browser/stop", {"profile_id": p["id"]})
        print(f"      tz={info['tz']} lang={info['lang']}")
        assert info["lang"].startswith("zh"), info
        assert info["tz"] == "Asia/Shanghai", info
        print("      camoufox 预设生效 ✔")

        step(2, "china 预设对 fp-chromium 内核生效（--timezone/--lang）...")
        fp = ok("POST", "/profiles", {
            "name": "RC国内预设FP", "kernel": "fp-chromium", "target_os": "windows",
            "launch": {"preset": "china", "headless": True},
        })
        created.append(fp["id"])
        r = CLIENT.post("/browser/start", json={"profile_id": fp["id"]}).json()
        assert r["code"] == 0, r
        port = r["data"]["debug_port"]
        info2 = asyncio.run(cdp_eval(port, ENV_INFO_JS))
        ok("POST", "/browser/stop", {"profile_id": fp["id"]})
        print(f"      tz={info2['tz']} lang={info2['lang']}")
        assert info2["tz"] == "Asia/Shanghai" and info2["lang"].startswith("zh"), info2
        print("      fp-chromium 预设生效 ✔")

        step(3, "Cloudflare 预设（disable_coop+轨迹增强）可正常启动 ...")
        cf = ok("POST", "/profiles", {
            "name": "RC_CF预设", "kernel": "camoufox", "target_os": "windows",
            "launch": {"preset": "cloudflare", "headless": True},
        })
        created.append(cf["id"])
        ok("POST", "/browser/start", {"profile_id": cf["id"]})
        ok("POST", "/browser/stop", {"profile_id": cf["id"]})
        print("      cloudflare 预设启动正常 ✔")

        step(4, "环境就绪度体检（实测网络/时区/语言/WebRTC/webdriver/Canvas）...")
        report = ok("POST", f"/profiles/{p['id']}/readiness")
        by_id = {c["id"]: c for c in report["checks"]}
        print(f"      得分 {report['score']}（{report['verdict_label']}）")
        for c in report["checks"]:
            print(f"      {c['status']:4s} | {c['id']:13s} | {c['detail'][:60]}")
        assert by_id["ip_reachable"]["status"] == "pass"
        assert by_id["webdriver"]["status"] == "pass"
        assert by_id["canvas_stable"]["status"] == "pass"
        assert by_id["webrtc_leak"]["status"] in ("pass", "warn")
        assert report["score"] >= 60, f"得分过低: {report['score']}"

        step(5, "RPA 人机化节奏（步间延迟 + 逐字输入）...")
        task = ok("POST", "/tasks", {
            "name": "RC节奏测试",
            "steps": [
                {"action": "navigate", "url": "data:text/html,<input id=q>"},
                {"action": "type", "selector": "#q", "text": "humanized-typing-test-0123456789"},
                {"action": "wait", "ms": 200}, {"action": "wait", "ms": 200},
                {"action": "wait", "ms": 200},
            ],
        })
        task_ids.append(task["id"])
        # 普通运行
        t0 = time.time()
        run = ok("POST", f"/tasks/{task['id']}/run", {"profile_ids": [p["id"]]})
        wait_run(run["run_ids"][0])
        plain = time.time() - t0
        # 人机化运行
        t0 = time.time()
        run = ok("POST", f"/tasks/{task['id']}/run",
                 {"profile_ids": [p["id"]], "humanize": True})
        finished = wait_run(run["run_ids"][0])
        humanized = time.time() - t0
        assert finished["status"] == "success", finished
        delta = humanized - plain
        print(f"      普通运行 {plain:.1f}s / 人机化 {humanized:.1f}s（差 {delta:.1f}s）")
        # 24 字符逐字输入 ≈ 2.4s + 4 次步间延迟 ≈ 2~7s → 差值应显著
        assert delta >= 3.0, f"人机化节奏未生效（差值 {delta:.1f}s）"
        print("      人机化节奏生效 ✔")

        step(6, "预热任务模板 ...")
        templates = ok("GET", "/task-templates")
        assert "warmup" in templates and templates["warmup"]["steps_count"] >= 5
        t = ok("POST", "/task-templates/warmup/create")
        task_ids.append(t["id"])
        assert len(t["steps"]) == templates["warmup"]["steps_count"]
        print(f"      模板创建 ✔（{templates['warmup']['name']}，{len(t['steps'])} 步）")

        print("\n风控环境优化专项 全部通过 ✔")
        return 0
    finally:
        for tid in task_ids:
            CLIENT.delete(f"/tasks/{tid}")
        for pid in created:
            CLIENT.delete(f"/profiles/{pid}")
        print(f"（已清理 {len(created)} 环境 / {len(task_ids)} 任务）")


if __name__ == "__main__":
    sys.exit(main())
