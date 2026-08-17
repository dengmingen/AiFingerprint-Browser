"""页面控制路由端到端测试：隔离数据目录 + 真实 camoufox 浏览器。

进程内 asyncio 直接调用（避免 TestClient 的 Windows ProactorEventLoop 不兼容问题）。

用法：.venv\\Scripts\\python scripts/page_control_test.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="fpwb-pc-test-"))
os.environ["FPWB_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402

PASS, FAIL = [], []

# 内嵌测试页面：覆盖 click/type/select/extract/hover 各交互
PAGE = (
    "data:text/html,<html><head><title>PC测试页</title></head><body>"
    "<h1 id=\"title\">Hello FPWorkbench</h1>"
    "<input id=\"q\" value=\"\"/>"
    "<select id=\"sel\"><option value=\"a\">甲</option><option value=\"b\">乙</option></select>"
    "<div id=\"out\" style=\"height:3000px;width:100px\">内容</div>"
    "<button id=\"btn\" onclick=\"document.getElementById('out').textContent='clicked'\">点我</button>"
    "</body></html>"
)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


async def main():
    db.init_db()
    manager = LaunchManager()

    profile = db.create_profile(
        name="页面控制测试", group_name="测试", notes="",
        kernel="camoufox", target_os="windows",
        proxy=None, fingerprint=create_fingerprint("windows", "generate"),
        launch={"preset": "standard", "geoip": False},
    )
    pid = profile["id"]
    print(f"环境已创建: {pid}")

    try:
        # ── 启动浏览器 ──
        t0 = time.time()
        inst = await manager.start(profile, headless=True)
        check("启动浏览器", inst is not None, f"耗时 {time.time()-t0:.0f}s")
        if not inst:
            return
        page = inst.page
        check("page 对象可用", page is not None)

        # ── navigate ──
        try:
            await page.goto(PAGE, wait_until="domcontentloaded", timeout=60_000)
            check("navigate", "PC" in page.url or "test" in page.url.lower(), page.url)
        except Exception as e:
            check("navigate", False, str(e))
            return

        # ── wait_for_element ──
        try:
            await page.locator("#btn").first.wait_for(timeout=10000)
            check("wait_for", True)
        except Exception as e:
            check("wait_for", False, str(e))

        # ── click ──
        try:
            await page.locator("#btn").first.click(timeout=10000)
            check("click", True)
        except Exception as e:
            check("click", False, str(e))

        # ── type ──
        try:
            loc = page.locator("#q").first
            await loc.fill("", timeout=10000)
            await loc.fill("测试输入", timeout=10000)
            check("type", True)
        except Exception as e:
            check("type", False, str(e))

        # ── press ──
        try:
            await page.keyboard.press("Tab")
            check("press", True)
        except Exception as e:
            check("press", False, str(e))

        # ── wait ──
        await asyncio.sleep(0.2)
        check("wait (200ms)", True)

        # ── scroll ──
        try:
            await page.mouse.wheel(0, 300)
            check("scroll", True)
        except Exception as e:
            check("scroll", False, str(e))

        # ── hover ──
        try:
            await page.locator("#btn").first.hover(timeout=10000)
            check("hover", True)
        except Exception as e:
            check("hover", False, str(e))

        # ── select ──
        try:
            await page.locator("#sel").first.select_option("b", timeout=10000)
            check("select", True)
        except Exception as e:
            check("select", False, str(e))

        # ── extract（文本）──
        try:
            values = await page.eval_on_selector_all(
                "#out", "els => els.map(e => (e.textContent || '').trim())"
            )
            check("extract 文本", values == ["clicked"], f"got {values}")
        except Exception as e:
            check("extract 文本", False, str(e))

        # ── extract（属性）──
        try:
            values = await page.eval_on_selector_all(
                "#btn", "els => els.map(e => e.getAttribute('id'))"
            )
            check("extract 属性", values == ["btn"], f"got {values}")
        except Exception as e:
            check("extract 属性", False, str(e))

        # ── page_info ──
        try:
            title = await page.title()
            url = page.url
            cookies = await page.context.cookies()
            check("page_info title", title == "PC测试页", title)
            check("page_info cookies", isinstance(cookies, list))
        except Exception as e:
            check("page_info", False, str(e))

        # ── get_html ──
        try:
            html = await page.content()
            check("get_html", "FPWorkbench" in html)
        except Exception as e:
            check("get_html", False, str(e))

        # ── evaluate（验证 click/type/select 副作用）──
        try:
            v = await page.evaluate(
                "({out: document.getElementById('out').textContent,"
                " q: document.getElementById('q').value,"
                " sel: document.getElementById('sel').value})"
            )
            check("evaluate click 副作用", v["out"] == "clicked", f"out={v['out']}")
            check("evaluate type 副作用", v["q"] == "测试输入", f"q={v['q']}")
            check("evaluate select 副作用", v["sel"] == "b", f"sel={v['sel']}")
        except Exception as e:
            check("evaluate", False, str(e))

        # ── screenshot ──
        try:
            shot = await page.screenshot(full_page=False)
            check("screenshot", len(shot) > 100, f"{len(shot)} bytes")
        except Exception as e:
            check("screenshot", False, str(e))

    finally:
        # ── 停止 & 清理 ──
        try:
            await manager.stop(pid)
        except Exception:
            pass
        try:
            db.delete_profile(pid)
        except Exception:
            pass
        shutil.rmtree(HOME, ignore_errors=True)

    # ── MCP 工具 schema 验证 ──
    from app.mcp_server import WorkbenchClient, build_tools
    tools = {t.name: t for t in build_tools(WorkbenchClient("http://127.0.0.1:1", None))}
    for tname in ["click_element", "type_text", "extract_elements", "get_page_info",
                  "get_html", "matrix_regenerate", "create_schedule", "cancel_task_run"]:
        check(f"MCP 工具 {tname}", tname in tools)

    # ── 汇总 ──
    print(f"\n{'='*50}")
    print(f"通过: {len(PASS)}  失败: {len(FAIL)}")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
