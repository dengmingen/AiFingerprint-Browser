"""RPA 引擎升级（Phase 4）端到端验证：变量/分支/重试/新动作/多标签。

用 camoufox 真实浏览器执行（约 1~2 分钟）：
  python scripts/rpa_engine_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402
from app.task_engine import TaskEngine, validate_steps  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


async def main():
    db.init_db()
    manager = LaunchManager()
    engine = TaskEngine(manager)

    profile = db.create_profile(
        name="rpa-engine-test", group_name="测试", notes="",
        kernel="camoufox", target_os="windows",
        proxy=None, fingerprint=create_fingerprint("windows", "generate"),
        launch={"preset": "standard", "geoip": False},
    )
    print("环境已创建:", profile["id"])

    steps = [
        {"action": "navigate", "url": "https://example.com", "timeout": 60000},
        {"action": "extract", "selector": "h1", "var": "title"},
        {"action": "set_var", "name": "greeting", "value": "head={{title}}"},
        # 分支：title 包含 Example → 跳过打印
        {"action": "if", "var": "title", "op": "contains", "value": "Example",
         "then_goto": "after_print"},
        {"action": "evaluate", "expression": "'should-not-run'"},
        {"action": "label", "label": "after_print"},
        # 变量替换（引擎在执行前替换 {{var}}）
        {"action": "evaluate", "expression": "'var-subst: {{greeting}}'", "var": "echo"},
        # 重试：对一个必然失败的动作 retry 2 次后 on_error=continue
        {"action": "click", "selector": "#nonexistent", "retry": 1,
         "timeout": 2000, "on_error": "continue"},
        # 截图 + 新标签页
        {"action": "screenshot", "name": "e2e"},
        {"action": "tab_open", "url": "https://example.org", "timeout": 60000},
        {"action": "extract", "selector": "h1", "var": "title2"},
        {"action": "tab_close"},
    ]
    errors = validate_steps(steps)
    check("步骤校验通过", not errors, str(errors))

    task = {"id": "test-task", "name": "引擎升级测试", "steps": steps, "webhook_url": None}
    run_ids = await engine.run(task, [profile], headless=True, auto_close=True)
    run_id = run_ids[0]
    # 等待 run 完成
    for _ in range(120):
        await asyncio.sleep(2)
        r = db.get_run(run_id)
        if r["status"] != "running":
            break
    r = db.get_run(run_id)
    print("run 状态:", r["status"])
    results = {e["index"]: e for e in r["results"]}
    for e in r["results"]:
        print(f"    #{e['index']} {e['action']:10s} {e['status']:8s} {e['detail'][:60]}")

    check("整体成功（continue 策略吃掉预期失败）", r["status"] == "success")
    check("extract 存变量", "Example" in str(results[1].get("extracted")), str(results[1].get("extracted")))
    check("if 分支跳过失败路径", "should-not-run" not in str(results[3].get("detail")))
    echo = results[6].get("value")
    check("set_var+变量替换", echo == "var-subst: head=Example Domain", repr(echo))
    check("retry 后 on_error=continue", results[7]["status"] == "failed", results[7]["status"])
    check("失败后继续执行", results[8]["status"] == "ok" and results[9]["status"] == "ok")
    check("tab_open 新页抽取", "Example" in str(results[10].get("extracted")), str(results[10].get("extracted")))
    check("tab_close 关闭", results[11]["status"] == "ok")

    db.delete_profile(profile["id"])
    import shutil
    shutil.rmtree(Path("profiles") / profile["id"], ignore_errors=True)
    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败项：", "；".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
