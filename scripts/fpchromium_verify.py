"""fingerprint-chromium 内核实测：启动 → CDP 端点 → 指纹真实生效验证。

通过 CDP WebSocket 读取页面内的 navigator 实际值，验证：
- UA 为 Chrome（无 Headless 特征）
- platform / hardwareConcurrency 与种子驱动的指纹一致
- 两次启动（同环境）指纹一致
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)

NAV_JS = """(() => ({
    ua: navigator.userAgent,
    platform: navigator.platform,
    hw: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory ?? null,
    webdriver: navigator.webdriver,
    langs: navigator.languages.join(','),
}))()"""


async def cdp_evaluate(debug_port: int) -> dict:
    """连 CDP 页面目标执行 JS 并取回结果。"""
    async with httpx.AsyncClient() as c:
        targets = (await c.get(f"http://127.0.0.1:{debug_port}/json")).json()
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
        msg_id = 1
        await ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate",
                                  "params": {"expression": NAV_JS, "returnByValue": True}}))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if resp.get("id") == msg_id:
                return resp["result"]["result"]["value"]


def main() -> int:
    created = []
    try:
        print("[1] 创建 fp-chromium 环境 ...")
        r = CLIENT.post("/profiles", json={
            "name": "fpchromium验证", "kernel": "fp-chromium", "target_os": "windows",
            "launch": {"headless": True, "start_url": "about:blank"},
        })
        assert r.json()["code"] == 0, r.json()
        p = r.json()["data"]
        created.append(p["id"])

        print("[2] 启动（无头）...")
        t0 = time.time()
        r = CLIENT.post("/browser/start", json={"profile_id": p["id"]}).json()
        assert r["code"] == 0, f"启动失败: {r}"
        info = r["data"]
        port = info["debug_port"]
        print(f"      {time.time()-t0:.1f}s 启动完成")
        print(f"      CDP ws: {info['ws_endpoint'][:60]}...")
        print(f"      指纹种子: {info['fingerprint_seed']}")

        print("[3] CDP /json/version ...")
        ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=10).json()
        print(f"      Browser: {ver['Browser']}")
        print(f"      UA: {ver['User-Agent'][:80]}")
        assert "Chrome/148" in ver["Browser"], ver["Browser"]
        assert "HeadlessChrome" not in ver["User-Agent"], "UA 暴露 Headless 特征!"

        print("[4] 页面内指纹采样（第 1 次）...")
        first = asyncio.run(cdp_evaluate(port))
        print(f"      ua={first['ua'][:70]}")
        print(f"      platform={first['platform']} hw={first['hw']} mem={first['mem']} "
              f"webdriver={first['webdriver']} langs={first['langs']}")

        print("[5] 停止 → 重启 → 采样（跨启动一致性）...")
        CLIENT.post("/browser/stop", json={"profile_id": p["id"]})
        time.sleep(2)
        r = CLIENT.post("/browser/start", json={"profile_id": p["id"]}).json()
        assert r["code"] == 0, r
        port2 = r["data"]["debug_port"]
        second = asyncio.run(cdp_evaluate(port2))
        CLIENT.post("/browser/stop", json={"profile_id": p["id"]})
        print(f"      第2次: platform={second['platform']} hw={second['hw']} mem={second['mem']}")
        same = all(first[k] == second[k] for k in ("ua", "platform", "hw", "mem", "langs"))
        print(f"      跨启动一致: {'✔' if same else '✘ 不一致!'}")
        assert same, f"{first} vs {second}"

        # 基本合理性
        assert first["webdriver"] is False or first["webdriver"] == False, "navigator.webdriver 未伪装!"
        assert first["platform"] == "Win32", first["platform"]
        assert "Chrome/" in first["ua"] and "Headless" not in first["ua"], first["ua"]

        print("\nfingerprint-chromium 内核验证全部通过 ✔")
        return 0
    finally:
        for pid in created:
            CLIENT.post("/browser/stop", json={"profile_id": pid})
            CLIENT.delete(f"/profiles/{pid}")


if __name__ == "__main__":
    sys.exit(main())
