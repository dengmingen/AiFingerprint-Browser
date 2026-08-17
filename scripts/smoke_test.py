"""冒烟测试：对运行中的服务跑一遍完整链路。

用法（先启动服务 python run.py，另开终端执行）:
    python scripts/smoke_test.py [--skip-browser]

流程: 状态检查 → 创建环境 → 列表/详情/更新 → 无头启动 → 页面导航/截图/执行 JS
      → 指纹一致性验证（两次 evaluate 对比）→ 停止 → 删除。
"""
import argparse
import base64
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:18080"
CLIENT = httpx.Client(base_url=BASE, timeout=120)


def call(method: str, path: str, json_body: dict | None = None) -> dict:
    r = CLIENT.request(method, f"/api/v1{path}", json=json_body)
    data = r.json()
    assert data["code"] == 0, f"{method} {path} 失败: {data}"
    return data["data"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-browser", action="store_true", help="跳过浏览器启动测试")
    args = parser.parse_args()

    print("[1/8] 服务状态 ...")
    status = call("GET", "/status")
    print(f"      版本 {status['version']}, 内核:",
          {k: v["available"] for k, v in status["kernels"].items()})

    print("[2/8] 创建环境 ...")
    created = call("POST", "/profiles", {
        "name": "冒烟测试环境",
        "group_name": "测试",
        "kernel": "camoufox",
        "target_os": "windows",
        "launch": {"headless": True, "start_url": "about:blank"},
    })
    pid = created["id"]
    print(f"      id={pid}")
    print(f"      UA={created['fingerprint_summary']['user_agent']}")

    print("[3/8] 列表与更新 ...")
    assert any(p["id"] == pid for p in call("GET", "/profiles"))
    call("PUT", f"/profiles/{pid}", {"notes": "冒烟测试备注"})
    detail = call("GET", f"/profiles/{pid}")
    assert detail["notes"] == "冒烟测试备注"

    if not args.skip_browser:
        if not status["kernels"]["camoufox"]["available"]:
            print("!! Camoufox 浏览器未安装，跳过浏览器测试")
            args.skip_browser = True

    if not args.skip_browser:
        print("[4/8] 无头启动环境 ...")
        t0 = time.time()
        info = call("POST", "/browser/start", {"profile_id": pid})
        print(f"      {time.time() - t0:.1f}s 启动完成, kernel={info['kernel']}")

        print("[5/8] 导航与页面控制 ...")
        call("POST", f"/browser/{pid}/navigate", {"url": "https://example.com"})
        shot = call("POST", f"/browser/{pid}/screenshot")
        png = base64.b64decode(shot["base64"])
        assert png[:4] == b"\x89PNG", "截图不是 PNG"
        print(f"      截图 {len(png)} bytes; title={call('POST', f'/browser/{pid}/evaluate', {'expression': 'document.title'})['result']!r}")

        print("[6/8] 指纹稳定性（同次会话内两次采样）...")
        expr = """(() => ({
            ua: navigator.userAgent,
            hw: navigator.hardwareConcurrency,
            platform: navigator.platform,
        }))()"""
        a = call("POST", f"/browser/{pid}/evaluate", {"expression": expr})["result"]
        b = call("POST", f"/browser/{pid}/evaluate", {"expression": expr})["result"]
        assert a == b, f"同会话指纹不稳定: {a} vs {b}"
        assert a["ua"] == detail["fingerprint_summary"]["user_agent"], "UA 与存储指纹不一致"
        print(f"      一致 ✔  hw={a['hw']} platform={a['platform']}")

        print("[7/8] 停止环境 ...")
        call("POST", "/browser/stop", {"profile_id": pid})
        time.sleep(1)
        active = {x["profile_id"] for x in call("GET", "/browser/active")}
        assert pid not in active, "停止后仍在运行列表"
    else:
        print("[4-7] 跳过浏览器测试")

    print("[8/8] 删除环境 ...")
    call("DELETE", f"/profiles/{pid}")
    assert all(p["id"] != pid for p in call("GET", "/profiles"))

    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
