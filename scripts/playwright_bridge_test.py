"""Playwright 对接专项测试：双内核真实连接 + 指纹一致性验证。

- camoufox:  Playwright Server 桥 → firefox.connect(ws) → 指纹与存储指纹逐字一致
- fp-chromium: CDP → chromium.connect_over_cdp(ws) → Chrome UA 无 Headless 特征
"""
import asyncio
import sys

import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)


def ok(method, path, body=None):
    r = CLIENT.request(method, path, json=body)
    data = r.json()
    assert data["code"] == 0, f"{method} {path} 失败: {data}"
    return data["data"]


def step(n, msg):
    print(f"[{n}] {msg}")


async def test_camoufox_server() -> str:
    step(1, "camoufox：启动 Playwright Server 桥 ...")
    p = ok("POST", "/profiles", {
        "name": "PW桥接测试", "kernel": "camoufox", "target_os": "windows",
        "launch": {"headless": True},
    })
    expected_ua = p["fingerprint_summary"]["user_agent"]
    info = ok("POST", f"/browser/{p['id']}/playwright-server", {})
    assert info["ws_endpoint"].startswith("ws://"), info
    print(f"      端点: {info['ws_endpoint']}")

    step(2, "用 playwright.firefox.connect 直连并验证指纹 ...")
    async with async_playwright() as pw:
        browser = await pw.firefox.connect(info["ws_endpoint"], timeout=60_000)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded",
                        timeout=60_000)
        nav = await page.evaluate(
            "({ua: navigator.userAgent, platform: navigator.platform,"
            " hw: navigator.hardwareConcurrency, webdriver: navigator.webdriver})")
        title = await page.title()
        state = await context.storage_state()  # 登录态管理 API 可用性
        await context.close()
        await browser.close()
    print(f"      UA={nav['ua']}")
    print(f"      platform={nav['platform']} hw={nav['hw']} webdriver={nav['webdriver']}")
    assert nav["ua"] == expected_ua, f"UA 与环境指纹不一致:\n{nav['ua']}\n{expected_ua}"
    assert nav["platform"] == "Win32" and nav["webdriver"] is False
    assert title == "Example Domain"
    assert "origins" in state

    step(3, "服务器生命周期：查询/停止 ...")
    got = ok("GET", f"/browser/{p['id']}/endpoint")
    assert got["ws_endpoint"] == info["ws_endpoint"] and got["protocol"] == "playwright"
    ok("DELETE", f"/browser/{p['id']}/playwright-server")
    r = CLIENT.get(f"/browser/{p['id']}/endpoint")
    assert r.status_code == 404
    print("      启动→连接→指纹一致→停止 全链路 ✔")
    return p["id"]


async def test_fp_chromium_cdp() -> str:
    step(4, "fp-chromium：启动 → CDP 端点 → connect_over_cdp ...")
    p = ok("POST", "/profiles", {
        "name": "PW_CDP测试", "kernel": "fp-chromium", "target_os": "windows",
        "launch": {"headless": True},
    })
    started = ok("POST", "/browser/start", {"profile_id": p["id"], "headless": True})
    endpoint = ok("GET", f"/browser/{p['id']}/endpoint")
    assert endpoint["protocol"] == "cdp"
    assert endpoint["ws_endpoint"].startswith("ws://")
    print(f"      端点: {endpoint['ws_endpoint']}")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(endpoint["ws_endpoint"],
                                                     timeout=60_000)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded",
                        timeout=60_000)
        nav = await page.evaluate(
            "({ua: navigator.userAgent, webdriver: navigator.webdriver})")
        title = await page.title()
        await context.close()
        await browser.close()  # 断开连接不关闭浏览器进程
    print(f"      UA={nav['ua'][:60]}... webdriver={nav['webdriver']} title={title}")
    assert "Chrome/" in nav["ua"] and "Headless" not in nav["ua"], nav["ua"]
    assert nav["webdriver"] is False
    assert title == "Example Domain"
    ok("POST", "/browser/stop", {"profile_id": p["id"]})
    print("      CDP 直连 + 断开后环境仍受服务管理 ✔")
    return p["id"]


def main() -> int:
    created = []
    try:
        created.append(asyncio.run(test_camoufox_server()))
        created.append(asyncio.run(test_fp_chromium_cdp()))
        print("\nPlaywright 对接 全部通过 ✔")
        return 0
    finally:
        for pid in created:
            CLIENT.delete(f"/profiles/{pid}")
        print(f"（已清理 {len(created)} 个测试环境）")


if __name__ == "__main__":
    sys.exit(main())
