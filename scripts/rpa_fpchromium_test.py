"""RPA 引擎（Phase 4）：fp-chromium 内核 CDP 直连执行验证。

  python scripts/rpa_fpchromium_test.py
"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.kernels import fp_chromium_kernel  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402
from app.task_engine import TaskEngine  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


async def main():
    ok, err = fp_chromium_kernel.is_available()
    if not ok:
        print("fp-chromium 未安装，跳过:", err)
        return
    db.init_db()
    manager = LaunchManager()
    engine = TaskEngine(manager)
    profile = db.create_profile(
        name="rpa-fpc-test", group_name="测试", notes="",
        kernel="fp-chromium", target_os="windows",
        proxy=None, fingerprint=create_fingerprint("windows", "generate"),
        launch={"preset": "standard"},
    )
    steps = [
        {"action": "navigate", "url": "https://example.com", "timeout": 60000},
        {"action": "extract", "selector": "h1", "var": "t"},
        {"action": "evaluate", "expression": "navigator.userAgent", "var": "ua"},
    ]
    task = {"id": "t", "name": "fpc", "steps": steps, "webhook_url": None}
    run_ids = await engine.run(task, [profile], headless=True, auto_close=True)
    for _ in range(60):
        await asyncio.sleep(2)
        r = db.get_run(run_ids[0])
        if r["status"] != "running":
            break
    r = db.get_run(run_ids[0])
    for e in r["results"]:
        print(f"    #{e['index']} {e['action']:10s} {e['status']:8s} {e['detail'][:60]}")
    check("fp-chromium 任务执行成功", r["status"] == "success", r.get("error"))
    check("页面抽取正常", "Example" in str(r["results"][1].get("extracted")))
    check("UA 为 Chrome 系", "Chrome" in str(r["results"][2].get("value")))

    db.delete_profile(profile["id"])
    shutil.rmtree(Path("profiles") / profile["id"], ignore_errors=True)
    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
