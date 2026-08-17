"""Turnstile 阻断对比实测：uBlock 开（standard 预设）vs 关（cloudflare 预设）。

只做诊断（验证域可达性 / 组件渲染 / 截图），不自动点击或提交人机验证。
"""
import sys
import time

import httpx

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=300)

CURSOR_URL = ("https://authenticator.cursor.sh/?client_id=client_01GS6W3C96KW4WRS6Z93JCE2RJ"
              "&redirect_uri=https%3A%2F%2Fcursor.com%2Fapi%2Fauth%2Fcallback")

# 探针：验证域可达（no-cors）+ 页面内 Turnstile 组件渲染状态
PROBE = """(async () => {
  const out = {};
  out.url = location.href.slice(0, 80);
  // 1) Cloudflare 验证域：网络层被扩展阻断才 reject
  out.cfDomain = await fetch('https://challenges.cloudflare.com/turnstile/v0/api.js',
    { mode: 'no-cors' }).then(() => '可达').catch(() => '被阻断');
  // 2) 页面上的 Turnstile 组件
  const ts = document.querySelectorAll(
    "iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [data-sitekey]");
  out.turnstileWidget = ts.length;
  const first = document.querySelector("iframe[src*='challenges.cloudflare.com']");
  out.widgetVisible = first ? !!(first.offsetWidth || first.offsetHeight) : null;
  // 3) 页面可见文案（判断是否已给出错误/需人工）
  out.bodyText = (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 160);
  return out;
})()"""


def probe_case(name: str, preset: str, disable_adblock=None) -> dict:
    body = {"name": f"诊断-{name}", "kernel": "camoufox", "target_os": "windows",
            "launch": {"preset": preset, "headless": True, "start_url": CURSOR_URL}}
    if disable_adblock is not None:
        body["launch"]["disable_adblock"] = disable_adblock
    p = CLIENT.post("/profiles", json=body).json()["data"]
    r = CLIENT.post("/browser/start", json={"profile_id": p["id"]}).json()
    assert r["code"] == 0, r
    time.sleep(4)  # 等页面与组件加载
    ev = CLIENT.post(f"/browser/{p['id']}/evaluate",
                     json={"expression": PROBE}).json()
    shot = CLIENT.post(f"/browser/{p['id']}/screenshot").json()
    result = ev["data"]["result"] if ev["code"] == 0 else {"error": ev["msg"]}
    result["_screenshot_bytes"] = len(shot["data"]["base64"]) * 3 // 4 if shot["code"] == 0 else 0
    CLIENT.post("/browser/stop", json={"profile_id": p["id"]})
    CLIENT.delete(f"/profiles/{p['id']}")
    return result


def main() -> int:
    print("[1] uBlock 开启（standard 预设，旧行为）...")
    a = probe_case("UBO开", "standard")
    print("    cf 验证域:", a.get("cfDomain"), "| Turnstile 组件:", a.get("turnstileWidget"),
          "| 可见:", a.get("widgetVisible"))
    print("    页面:", a.get("bodyText", "")[:80])

    print("[2] uBlock 排除（cloudflare 预设，修复后）...")
    b = probe_case("UBO关", "cloudflare")
    print("    cf 验证域:", b.get("cfDomain"), "| Turnstile 组件:", b.get("turnstileWidget"),
          "| 可见:", b.get("widgetVisible"))
    print("    页面:", b.get("bodyText", "")[:80])

    print("\n结论：")
    if a.get("cfDomain") == "被阻断" and b.get("cfDomain") == "可达":
        print("  ✔ 确认元凶：uBlock 阻断 challenges.cloudflare.com 导致 Turnstile 拒绝")
        print("  ✔ 修复生效：cloudflare 预设已默认排除 uBlock")
        return 0
    print(f"  uBlock 开={a.get('cfDomain')} / 关={b.get('cfDomain')}")
    if b.get("cfDomain") == "可达":
        print("  验证域可达性正常；若仍被拒，检查 IP 质量（机房 IP）与有头模式")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
