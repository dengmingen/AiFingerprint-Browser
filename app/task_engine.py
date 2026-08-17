"""RPA 任务引擎：在环境上按步骤序列执行自动化操作。

支持的动作:
  navigate  {url, timeout?}          打开页面
  click     {selector, timeout?}     点击元素
  type      {selector, text, press_enter?}  清空并输入文本
  press     {key}                    按键（Enter/Tab/Escape...）
  wait      {ms}                     固定等待
  wait_for  {selector, timeout?}     等待元素出现
  scroll    {amount}                 垂直滚动像素（可为负）
  screenshot {full_page?, name?}     截图留痕（保存到 data/runs/<run_id>/）
  extract   {selector, attr?, var?}  抽取元素文本/属性；var=存入变量供后续引用
  evaluate  {expression, var?}       执行 JS 并记录返回值；var=存入变量
  hover     {selector}               悬停
  select    {selector, value}        下拉选择（按 value 或可见文本）
  upload    {selector, path}         文件上传
  download  {url, timeout?}          下载文件到 run 目录
  tab_open  {url}                    新标签页打开
  tab_switch {value}                 切换标签页（序号或标题包含文本）
  tab_close {}                       关闭当前标签页
  set_var   {name, value}            设置变量（value 支持 {{var}} 引用）
  label     {name}                   标签（goto 跳转锚点）
  goto      {label}                  跳转到标签步骤
  if        {var, op, value, then_goto, else_goto?}  条件跳转

步骤通用字段:
  frame     {str}                    限定在某个 iframe 内执行（CSS 选择器）
  retry     {int}                    失败重试次数（默认 0）
  on_error  {abort|continue|goto:标签}  失败后的处置（默认 abort 终止任务）
变量: extract/evaluate 步骤指定 var 后，后续步骤的任意字符串字段可用
  {{var名}} 引用（列表变量取第一项）。变量作用域为单次 run。

运行模型：每个 (任务, 环境) 组合一个 run；可多环境并发；
环境未运行时自动以无头模式拉起，结束后按 auto_close 决定是否关闭。
内核支持：camoufox（Juggler）与 fp-chromium/chromium（CDP 直连）统一走
Playwright Page API。
"""
import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from . import db
from .config import DATA_DIR
from .launcher import LaunchManager

log = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {
    "navigate", "click", "type", "press", "wait", "wait_for",
    "scroll", "screenshot", "extract", "evaluate",
    "hover", "select", "upload", "download",
    "tab_open", "tab_switch", "tab_close",
    "set_var", "label", "goto", "if",
}

_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")

# 变量替换会应用到这些字符串字段上
_SUBST_FIELDS = ("url", "selector", "text", "key", "expression", "value",
                 "label", "attr", "frame", "path", "name")


def _subst(value: Any, variables: dict) -> Any:
    """把 {{var}} 替换为变量值（列表取首项）；非字符串原样返回。"""
    if not isinstance(value, str) or "{{" not in value:
        return value

    def repl(m: re.Match) -> str:
        v = variables.get(m.group(1))
        if v is None:
            return m.group(0)  # 未定义变量保留原样（便于发现拼写错误）
        return str(v[0]) if isinstance(v, list) and v else str(v)

    return _VAR_PATTERN.sub(repl, value)


class _RunContext:
    """单次 run 的执行上下文：页面列表、当前页、变量。"""

    def __init__(self, context, run_id: str):
        self.context = context
        self.run_id = run_id
        self.variables: dict[str, Any] = {}
        self.pages: list = []
        self.current: int = 0

    async def init_pages(self) -> None:
        self.pages = [p for p in self.context.pages]
        if not self.pages:
            self.pages = [await self.context.new_page()]
        self.current = 0

    @property
    def page(self):
        return self.pages[self.current]


class TaskEngine:
    def __init__(self, manager: LaunchManager) -> None:
        self.manager = manager
        self._jobs: dict[str, asyncio.Task] = {}
        self._start_sem = asyncio.Semaphore(3)  # 并发拉起浏览器上限

    # ------------------------------------------------------------ 对外接口

    async def run(self, task: dict, profiles: list[dict],
                  headless: bool = True, auto_close: bool = True,
                  humanize: bool = False) -> list[str]:
        """为每个环境创建一个 run 并异步执行，立即返回 run id 列表。"""
        run_ids = []
        for profile in profiles:
            run = db.create_run(
                task_id=task["id"], task_name=task["name"],
                profile_id=profile["id"], profile_name=profile["name"],
            )
            run_id = run["id"]
            job = asyncio.create_task(
                self._execute(run_id, task, profile, headless, auto_close, humanize)
            )
            self._jobs[run_id] = job
            run_ids.append(run_id)
        return run_ids

    def cancel(self, run_id: str) -> bool:
        job = self._jobs.get(run_id)
        if job and not job.done():
            job.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        for run_id, job in list(self._jobs.items()):
            if not job.done():
                job.cancel()
        await asyncio.gather(*[j for j in self._jobs.values() if not j.done()],
                             return_exceptions=True)

    # ------------------------------------------------------------ 执行器

    async def _execute(self, run_id: str, task: dict, profile: dict,
                       headless: bool, auto_close: bool, humanize: bool = False) -> None:
        started_by_us = False
        pw = None          # fp-chromium/chromium：本 run 专属 playwright 连接
        conn_browser = None
        try:
            inst = self.manager.get_instance(profile["id"])
            if inst is None:
                async with self._start_sem:
                    await self.manager.start(profile, headless=headless)
                started_by_us = True
                inst = self.manager.get_instance(profile["id"])
            if inst is None:
                raise RuntimeError("浏览器实例不可用")

            if inst.kernel == "camoufox":
                context = inst.context
                if context is None:
                    raise RuntimeError("camoufox 实例缺少页面上下文")
            else:
                ws = inst.info.get("ws_endpoint")
                if not ws:
                    raise RuntimeError("chromium 系实例缺少 CDP 端点")
                from playwright.async_api import async_playwright

                pw = await async_playwright().start()
                conn_browser = await pw.chromium.connect_over_cdp(ws)
                context = conn_browser.contexts[0] if conn_browser.contexts \
                    else await conn_browser.new_context()

            ctx = _RunContext(context, run_id)
            await ctx.init_pages()
            results, failed = await self._run_steps(ctx, task, humanize)

            db.update_run(run_id, {
                "status": "failed" if failed else "success",
                "results": results, "finished": True,
                **({"error": results[-1]["detail"]} if failed else {}),
            })
            await self._notify_webhook(task, profile, run_id,
                                       "failed" if failed else "success", results)
        except asyncio.CancelledError:
            db.update_run(run_id, {"status": "cancelled", "finished": True})
        except Exception as e:
            log.exception("任务运行 %s 失败", run_id)
            db.update_run(run_id, {"status": "failed", "error": str(e)[:500], "finished": True})
        finally:
            self._jobs.pop(run_id, None)
            # CDP 连接只断开不杀浏览器；auto_close 时由 manager.stop 统一关闭
            if conn_browser:
                try:
                    await conn_browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            if started_by_us and auto_close:
                await self.manager.stop(profile["id"])

    async def _run_steps(self, ctx: _RunContext, task: dict,
                         humanize: bool) -> tuple[list, bool]:
        """带控制流（goto/if）的步骤执行循环。"""
        steps = task["steps"]
        labels = {s["label"]: i for i, s in enumerate(steps)
                  if s.get("action") == "label" and s.get("label")}
        results: list[dict] = []
        failed = False
        i = 0
        jump: Optional[int] = None
        while i < len(steps):
            step = dict(steps[i])
            # 变量替换（跳转类字段在分支动作内部处理）
            for field in _SUBST_FIELDS:
                if field in step:
                    step[field] = _subst(step[field], ctx.variables)

            if humanize and results:
                await asyncio.sleep(random.uniform(0.4, 1.8))

            entry = {"index": i, "action": step.get("action", "?"),
                     "status": "ok", "detail": ""}
            try:
                jump = await self._do_step(ctx, step, entry, humanize, labels)
                entry["detail"] = entry.get("detail") or ""
            except asyncio.CancelledError:
                entry.update(status="cancelled", detail="任务被取消")
                results.append(entry)
                raise
            except Exception as e:
                entry.update(status="failed", detail=str(e)[:300])
                on_error = step.get("on_error") or "abort"
                if on_error.startswith("goto:") and on_error[5:] in labels:
                    entry["detail"] += "（按 on_error 跳转）"
                    results.append(entry)
                    i = labels[on_error[5:]]
                    jump = None
                    continue
                if on_error == "continue":
                    results.append(entry)
                    i += 1
                    continue
                results.append(entry)
                failed = True
                break
            results.append(entry)
            if isinstance(jump, int):
                i = jump
                jump = None
            else:
                i += 1
        return results, failed

    async def _notify_webhook(self, task: dict, profile: dict, run_id: str,
                              status: str, results: list) -> None:
        """任务配置了 webhook_url 时，运行结束回调（失败不阻塞主流程）。"""
        url = (task or {}).get("webhook_url")
        if not url:
            return
        payload = {
            "event": "task.run.finished",
            "run_id": run_id,
            "task_id": task["id"],
            "task_name": task["name"],
            "profile_id": profile["id"],
            "profile_name": profile["name"],
            "status": status,
            "steps_total": len(results),
            "steps_ok": sum(1 for r in results if r["status"] == "ok"),
            "results": results,
        }

        async def _fire() -> None:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(url, json=payload)
            except Exception as e:
                log.warning("webhook 回调失败 %s: %s", url, e)
        asyncio.create_task(_fire())

    # ------------------------------------------------------------ 步骤实现

    def _locator(self, ctx: _RunContext, step: dict):
        """按 frame 字段解析目标：主文档或 iframe 内的定位器。"""
        page = ctx.page
        if step.get("frame"):
            return page.frame_locator(step["frame"]).locator(step["selector"])
        return page.locator(step["selector"])

    async def _do_step(self, ctx: _RunContext, step: dict, entry: dict,
                       humanize: bool, labels: dict) -> Optional[int]:
        """执行单个步骤；返回 goto 目标索引（无跳转返回 None）。
        内部处理 retry 字段的重试。"""
        action = step.get("action")
        timeout = int(step.get("timeout") or 30_000)
        retries = int(step.get("retry") or 0)

        # 不需要浏览器的动作
        if action == "label":
            return None
        if action == "set_var":
            ctx.variables[step["name"]] = step.get("value", "")
            entry["detail"] = f"{step['name']} = {str(step.get('value', ''))[:60]}"
            return None

        attempts = retries + 1
        for attempt in range(attempts):
            try:
                jump = await self._do_step_once(ctx, step, entry, humanize,
                                                timeout, labels)
                if attempt:
                    entry["detail"] += f"（第 {attempt + 1} 次尝试成功）"
                return jump
            except Exception:
                if attempt < attempts - 1:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                raise
        return None

    async def _do_step_once(self, ctx: _RunContext, step: dict, entry: dict,
                            humanize: bool, timeout: int,
                            labels: dict) -> Optional[int]:
        action = step.get("action")
        page = ctx.page

        if action == "goto":
            target = step.get("label")
            if target not in labels:
                raise ValueError(f"goto 目标标签不存在: {target}")
            entry["detail"] = f"跳转到 {target}"
            return labels[target]

        if action == "if":
            var_name = step.get("var") or ""
            var = ctx.variables.get(var_name)
            if isinstance(var, list):
                var = var[0] if var else None
            op = step.get("op") or "equals"
            expected = step.get("value")
            if op == "exists":
                cond = var is not None
            elif op == "contains":
                cond = str(var or "").find(str(expected)) >= 0
            else:  # equals
                cond = str(var if var is not None else "") == str(expected)
            target = (step.get("then_goto") if cond else step.get("else_goto")) or ""
            entry["detail"] = f"{var_name}({op} {expected})={cond} → {target or '继续'}"
            if target:
                if target not in labels:
                    raise ValueError(f"if 目标标签不存在: {target}")
                return labels[target]
            return None

        if action == "navigate":
            await page.goto(step["url"], wait_until="domcontentloaded", timeout=timeout)
            entry["detail"] = page.url
            return None

        if action == "click":
            await self._locator(ctx, step).first.click(timeout=timeout)
            entry["detail"] = f"已点击 {step['selector']}"
            return None

        if action == "type":
            text = step.get("text", "")
            loc = self._locator(ctx, step).first
            await loc.fill("", timeout=timeout)
            if humanize and text:
                await loc.type(text, delay=random.randint(60, 140))
            else:
                await loc.fill(text, timeout=timeout)
            if step.get("press_enter"):
                await loc.press("Enter")
            entry["detail"] = f"已输入 {len(text)} 字符"
            return None

        if action == "press":
            await page.keyboard.press(step["key"])
            entry["detail"] = f"已按键 {step['key']}"
            return None

        if action == "wait":
            ms = int(step.get("ms", 1000))
            await asyncio.sleep(ms / 1000)
            entry["detail"] = f"等待 {ms}ms"
            return None

        if action == "wait_for":
            await self._locator(ctx, step).first.wait_for(timeout=timeout)
            entry["detail"] = f"元素已出现 {step['selector']}"
            return None

        if action == "scroll":
            await page.mouse.wheel(0, int(step.get("amount", 500)))
            entry["detail"] = f"滚动 {step.get('amount', 500)}px"
            return None

        if action == "screenshot":
            shot_dir = DATA_DIR / "runs" / ctx.run_id
            shot_dir.mkdir(parents=True, exist_ok=True)
            name = step.get("name") or f"step_{entry['index']}"
            path = shot_dir / f"{name}.png"
            await page.screenshot(path=str(path), full_page=bool(step.get("full_page")))
            entry["screenshot"] = f"/runs/{ctx.run_id}/{name}.png"
            entry["detail"] = f"截图已保存 {path.name}"
            return None

        if action == "extract":
            if step.get("attr"):
                values = await page.eval_on_selector_all(
                    step["selector"], f"els => els.map(e => e.getAttribute({step['attr']!r}))"
                )
            else:
                values = await page.eval_on_selector_all(
                    step["selector"],
                    "els => els.map(e => (e.textContent || '').trim())",
                )
            entry["extracted"] = values[:50]
            if step.get("var"):
                ctx.variables[step["var"]] = values
                entry["detail"] = f"抽取 {len(values)} 项 → 变量 {step['var']}"
            else:
                entry["detail"] = f"抽取到 {len(values)} 项"
            return None

        if action == "evaluate":
            value = await page.evaluate(step["expression"])
            entry["value"] = _jsonable(value)
            if step.get("var"):
                ctx.variables[step["var"]] = value
                entry["detail"] = f"结果已存入变量 {step['var']}"
            else:
                entry["detail"] = "执行成功"
            return None

        if action == "hover":
            await self._locator(ctx, step).first.hover(timeout=timeout)
            entry["detail"] = f"已悬停 {step['selector']}"
            return None

        if action == "select":
            value = step.get("value", "")
            loc = self._locator(ctx, step).first
            try:
                await loc.select_option(value, timeout=timeout)
            except Exception:
                # 按可见文本选择（value 不是 option value 的情况）
                await loc.select_option(label=value, timeout=timeout)
            entry["detail"] = f"已选择 {value}"
            return None

        if action == "upload":
            path = Path(step["path"])
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.is_file():
                raise FileNotFoundError(f"上传文件不存在: {path}")
            await self._locator(ctx, step).first.set_input_files(str(path), timeout=timeout)
            entry["detail"] = f"已上传 {path.name}"
            return None

        if action == "download":
            dl_dir = DATA_DIR / "runs" / ctx.run_id / "downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)
            async with page.expect_download(timeout=timeout) as dl_info:
                if step.get("url"):
                    await page.goto(step["url"], wait_until="commit", timeout=timeout)
                else:
                    await page.keyboard.press("Enter")  # 常见：表单触发的下载
            download = await dl_info.value
            dest = dl_dir / (download.suggested_filename or f"file_{int(time.time())}")
            await download.save_as(str(dest))
            entry["download"] = str(dest)
            entry["detail"] = f"已下载 {dest.name}"
            return None

        if action == "tab_open":
            new_page = await ctx.context.new_page()
            ctx.pages.append(new_page)
            ctx.current = len(ctx.pages) - 1
            if step.get("url"):
                await new_page.goto(step["url"], wait_until="domcontentloaded",
                                    timeout=timeout)
            entry["detail"] = f"新标签页 #{ctx.current} {new_page.url}"
            return None

        if action == "tab_switch":
            value = str(step.get("value") or "0")
            if value.isdigit():
                idx = int(value)
                if idx >= len(ctx.pages):
                    raise IndexError(f"标签页序号超界：{idx}（共 {len(ctx.pages)} 个）")
                ctx.current = idx
                entry["detail"] = f"切换到标签页 #{idx}"
            else:
                found = False
                for idx, p in enumerate(ctx.pages):
                    if p.is_closed():
                        continue
                    if value in (await p.title()):
                        ctx.current = idx
                        entry["detail"] = f"切换到标签页 #{idx}（{value}）"
                        found = True
                        break
                if not found:
                    raise ValueError(f"没有标题包含 {value!r} 的标签页")
            return None

        if action == "tab_close":
            closing = ctx.page
            ctx.pages.pop(ctx.current)
            ctx.current = max(0, ctx.current - 1)
            await closing.close()
            entry["detail"] = f"已关闭标签页（剩 {len(ctx.pages)} 个）"
            return None

        raise ValueError(f"不支持的步骤动作: {action}")


def _jsonable(value: Any) -> Any:
    try:
        import json

        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def validate_steps(steps: list[dict]) -> list[str]:
    """任务保存前的静态校验，返回错误列表。"""
    errors = []
    labels = {s.get("label") for s in steps if s.get("action") == "label"}
    required = {
        "navigate": ("url",), "click": ("selector",), "type": ("selector",),
        "press": ("key",), "wait_for": ("selector",), "extract": ("selector",),
        "evaluate": ("expression",), "select": ("selector", "value"),
        "upload": ("selector", "path"), "goto": ("label",),
        "set_var": ("name",),
        "if": ("var", "then_goto"),
        "tab_switch": ("value",),
    }
    for i, step in enumerate(steps):
        action = step.get("action")
        n = f"步骤{i + 1}"
        if action not in SUPPORTED_ACTIONS:
            errors.append(f"{n}: 不支持的动作 {action}")
            continue
        for field in required.get(action, ()):
            if not step.get(field):
                errors.append(f"{n} ({action}): 缺少 {field}")
        if action == "type" and "text" not in step:
            errors.append(f"{n} (type): 缺少 text")
        if action == "goto" and step.get("label") and step["label"] not in labels:
            errors.append(f"{n} (goto): 标签 {step['label']} 不存在")
        if action == "if":
            for fld in ("then_goto", "else_goto"):
                tgt = step.get(fld)
                if tgt and tgt not in labels:
                    errors.append(f"{n} (if): {fld} 标签 {tgt} 不存在")
            if step.get("op") and step["op"] not in ("equals", "contains", "exists"):
                errors.append(f"{n} (if): op 仅支持 equals/contains/exists")
        on_error = step.get("on_error")
        if on_error and on_error not in ("abort", "continue") and \
                not on_error.startswith("goto:"):
            errors.append(f"{n}: on_error 仅支持 abort/continue/goto:标签")
        if on_error and on_error.startswith("goto:") and on_error[5:] not in labels:
            errors.append(f"{n}: on_error 标签 {on_error[5:]} 不存在")
        retry = step.get("retry")
        if retry is not None and not (0 <= int(retry) <= 5):
            errors.append(f"{n}: retry 需在 0~5 之间")
    # 同名标签唯一性
    names = [s.get("label") for s in steps if s.get("action") == "label" and s.get("label")]
    dup = {x for x in names if names.count(x) > 1}
    if dup:
        errors.append(f"标签名重复: {', '.join(sorted(dup))}")
    return errors
