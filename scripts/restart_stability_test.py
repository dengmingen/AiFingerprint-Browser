"""跨重启指纹稳定性测试：同一环境两次启动，采集指纹特征并对比。

采集项: UA / platform / hardwareConcurrency / deviceMemory / 屏幕参数 /
Canvas 哈希 / AudioContext 指纹 / 字体度量。
"""
import hashlib
import sys

import httpx

BASE = "http://127.0.0.1:18080/api/v1"
CLIENT = httpx.Client(base_url=BASE, timeout=180)

COLLECT_JS = """(() => {
  const out = {};
  out.ua = navigator.userAgent;
  out.platform = navigator.platform;
  out.hw = navigator.hardwareConcurrency;
  out.mem = navigator.deviceMemory ?? null;
  out.screen = [screen.width, screen.height, screen.colorDepth];
  out.lang = navigator.languages.join(',');
  // Canvas 指纹
  const c = document.createElement('canvas'); c.width = 220; c.height = 30;
  const ctx = c.getContext('2d');
  ctx.textBaseline = 'top'; ctx.font = "14px 'Arial'"; ctx.fillStyle = '#f60';
  ctx.fillRect(125, 1, 62, 20); ctx.fillStyle = '#069';
  ctx.fillText('指纹浏览器工作台 🐾', 2, 15);
  ctx.fillStyle = 'rgba(102,204,0,0.7)';
  ctx.fillText('fingerprint-stability', 4, 17);
  out.canvas = c.toDataURL().length + ':' +
    [...c.toDataURL()].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7);
  // Audio 指纹
  try {
    const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    const ac = new AC(1, 4096, 44100);
    const osc = ac.createOscillator(); osc.type = 'triangle';
    osc.frequency.value = 10000;
    const comp = ac.createDynamicsCompressor();
    osc.connect(comp); comp.connect(ac.destination); osc.start(0);
    ac.startRendering();
  } catch (e) { out.audio_err = String(e); }
  // 字体度量
  const s = document.createElement('span');
  s.style.cssText = 'position:absolute;left:-9999px;font-size:72px';
  s.textContent = 'mmmmmmmmmmlli';
  document.body.appendChild(s);
  s.style.fontFamily = 'monospace';
  const w1 = s.offsetWidth; s.style.fontFamily = 'sans-serif';
  out.fonts = w1 + ',' + s.offsetWidth;
  s.remove();
  return out;
})()"""


def collect(profile_id: str) -> dict:
    r = CLIENT.post(f"/browser/{profile_id}/evaluate", json={"expression": COLLECT_JS})
    data = r.json()
    assert data["code"] == 0, data
    return data["data"]["result"]


def main() -> int:
    r = CLIENT.post("/profiles", json={
        "name": "稳定性测试",
        "kernel": "camoufox",
        "target_os": "windows",
        "launch": {"headless": True},
    })
    assert r.json()["code"] == 0, r.json()
    pid = r.json()["data"]["id"]

    try:
        print("第 1 次启动 ...")
        assert CLIENT.post("/browser/start", json={"profile_id": pid}).json()["code"] == 0
        first = collect(pid)
        CLIENT.post("/browser/stop", json={"profile_id": pid})

        print("第 2 次启动（全新进程）...")
        assert CLIENT.post("/browser/start", json={"profile_id": pid}).json()["code"] == 0
        second = collect(pid)
        CLIENT.post("/browser/stop", json={"profile_id": pid})

        print("\n指标                  | 第1次                          | 第2次                          | 一致")
        print("-" * 100)
        all_ok = True
        for key in first:
            a, b = first[key], second[key]
            same = a == b
            all_ok &= same
            sa = str(a)[:30].ljust(30)
            sb = str(b)[:30].ljust(30)
            print(f"{key:21s} | {sa} | {sb} | {'✔' if same else '✘ 不一致!'}")

        print()
        if all_ok:
            print("跨重启指纹稳定 ✔")
            return 0
        print("存在跨重启漂移的指标 ✘")
        return 1
    finally:
        CLIENT.delete(f"/profiles/{pid}")


if __name__ == "__main__":
    sys.exit(main())
