"""FPWorkbench MCP Server：把本地 API 包装为 MCP 工具，供 Claude/Cursor 等 AI 工具直连。

用法（stdio 传输，newline-delimited JSON-RPC 2.0）：
    .venv\\Scripts\\python -m app.mcp_server [--url http://127.0.0.1:18080] [--api-key KEY]

密钥解析顺序：--api-key 参数 > FPWB_API_KEY 环境变量 > data/settings.json 的 api_key。
工作台地址解析顺序：--url 参数 > FPWB_URL 环境变量 > http://127.0.0.1:18080。

MCP 是对运行中的工作台 HTTP API 的薄包装——浏览器生命周期仍由 Web 服务统一管理，
本进程不碰数据库与浏览器。Claude Desktop / Cursor 配置示例见 docs/MCP对接指南.md。
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .config import DATA_DIR, VERSION

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "fpworkbench", "version": VERSION, "title": "FPWorkbench 指纹浏览器工作台"}
MAX_TEXT = 12000  # 单次工具返回的文本上限（防撑爆 AI 上下文）


# ---------------------------------------------------------------- HTTP 客户端

class WorkbenchClient:
    def __init__(self, base_url: str, api_key: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"X-API-Key": api_key} if api_key else {}
        self.http = httpx.Client(base_url=self.base_url, headers=headers, timeout=120)

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        # GET 的 body 作为查询参数传递（task-runs 等过滤场景）
        if method == "GET" and body:
            resp = self.http.request(method, path, params=body)
        else:
            resp = self.http.request(method, path, json=body)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            return {"raw": resp.text[:MAX_TEXT]}
        # 本地 API 约定 {"code":0,"msg":...,"data":...}；code!=0 抛给上层成 MCP 错误
        if isinstance(data, dict) and "code" in data and data["code"] != 0:
            raise RuntimeError(f"API {path} 失败（{data.get('code')}）：{data.get('msg')}")
        if resp.status_code >= 400:
            raise RuntimeError(f"API {path} HTTP {resp.status_code}：{json.dumps(data, ensure_ascii=False)[:400]}")
        return data.get("data", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------- 工具定义

def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


def _s(desc: str = "", **kw) -> dict:
    return {"type": "string", "description": desc, **kw}


def _b(desc: str = "", **kw) -> dict:
    return {"type": "boolean", "description": desc, **kw}


def _i(desc: str = "", **kw) -> dict:
    return {"type": "integer", "description": desc, **kw}


class Tool:
    def __init__(self, name: str, desc: str, schema: dict, fn: Callable[..., Any]) -> None:
        self.name, self.description, self.inputSchema, self.fn = name, desc, schema, fn


def build_tools(client: WorkbenchClient) -> list[Tool]:
    return [
        # ======== 系统 ========
        Tool("status", "工作台状态：版本、内核可用性（camoufox/fp-chromium/chromium）",
             _obj({}), lambda a: client.call("GET", "/api/v1/status")),

        # ======== 环境管理 ========
        Tool("list_profiles", "环境列表（名称/内核/系统/代理/健康分/运行状态）",
             _obj({"kernel": _s("按内核过滤：camoufox/fp-chromium/chromium")}),
             lambda a: client.call("GET", "/api/v1/profiles")),
        Tool("get_profile", "查看单个环境详情（含完整指纹 JSON）",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("GET", f"/api/v1/profiles/{a['profile_id']}")),
        Tool("create_profile", "创建环境。proxy_json 如 {\"scheme\":\"socks5\",\"host\":\"1.2.3.4\",\"port\":1080,\"username\":\"u\",\"password\":\"p\"}；"
             "launch_json 如 {\"geoip\":true,\"preset\":\"china\",\"start_url\":\"https://...\"}",
             _obj({"name": _s("环境名", minLength=1), "kernel": _s("camoufox（默认）/fp-chromium/chromium"),
                   "fingerprint_mode": _s("generate=合成（默认）/preset=真实设备预设"),
                   "target_os": _s("windows/macos/linux"), "group": _s("分组名"),
                   "proxy_json": _s("代理配置 JSON 字符串，空=直连"),
                   "launch_json": _s("启动选项 JSON 字符串")},
                  ["name"]),
             lambda a: client.call("POST", "/api/v1/profiles", {
                 "name": a["name"], "kernel": a.get("kernel"),
                 "fingerprint_mode": a.get("fingerprint_mode"), "target_os": a.get("target_os"),
                 "group": a.get("group"),
                 "proxy": json.loads(a["proxy_json"]) if a.get("proxy_json") else None,
                 "launch": json.loads(a["launch_json"]) if a.get("launch_json") else None,
             })),
        Tool("update_profile", "修改环境配置（名称/分组/代理/指纹重生成等）。"
             "updates_json 如 {\"name\":\"新名\",\"group_name\":\"分组\",\"proxy\":{\"scheme\":\"socks5\",...},"
             "\"regen_fingerprint\":true,\"clear_proxy\":false}，仅传需修改字段",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "updates_json": _s("修改字段 JSON 字符串，空={} 表示不修改")}, ["profile_id"]),
             lambda a: client.call("PUT", f"/api/v1/profiles/{a['profile_id']}",
                                   json.loads(a.get("updates_json", "{}")))),
        Tool("delete_profile", "删除环境（运行中会拒绝，先 stop_browser）",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("DELETE", f"/api/v1/profiles/{a['profile_id']}")),
        Tool("batch_create_profiles", "按模板批量创建环境（支持代理池依次分配）。"
             "template_json 如 {\"name\":\"店铺\",\"kernel\":\"camoufox\",\"target_os\":\"windows\","
             "\"fingerprint_mode\":\"preset\",\"proxy\":{\"scheme\":\"socks5\",...}}；"
             "proxy_pool_json 如 [{\"scheme\":\"socks5\",\"host\":\"1.2.3.4\",\"port\":1080},...]",
             _obj({"count": _i("创建数量（1-200）"), "template_json": _s("模板 JSON 字符串"),
                   "proxy_pool_json": _s("代理池 JSON 数组字符串（可选，依次分配）")}, ["count", "template_json"]),
             lambda a: client.call("POST", "/api/v1/profiles/batch", {
                 "count": a["count"],
                 "template": json.loads(a["template_json"]),
                 "proxy_pool": json.loads(a["proxy_pool_json"]) if a.get("proxy_pool_json") else None,
             })),
        Tool("batch_delete_profiles", "批量删除环境（运行中的会跳过）",
             _obj({"profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"}},
                  ["profile_ids"]),
             lambda a: client.call("POST", "/api/v1/profiles/batch-delete",
                                   {"profile_ids": a["profile_ids"]})),
        Tool("export_profile", "导出环境为可移植 JSON（可选含数据目录归档）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "include_data": _b("是否包含用户数据目录（Cookie 等，体积较大）")}, ["profile_id"]),
             lambda a: client.call("GET", f"/api/v1/profiles/{a['profile_id']}/export",
                                   body={"include_data": a.get("include_data", False)} if a.get("include_data") else None)),
        Tool("import_profile", "导入环境（数据为导出格式 JSON）",
             _obj({"profile_json": _s("导出格式的完整 JSON 字符串")}, ["profile_json"]),
             lambda a: client.call("POST", "/api/v1/profiles/import",
                                   json.loads(a["profile_json"]))),

        # ======== 浏览器启停 ========
        Tool("start_browser", "启动环境浏览器，返回调试端点（fp-chromium/chromium 为 CDP ws_endpoint，可接 Selenium/Puppeteer/Playwright）",
             _obj({"profile_id": _s("环境 ID", minLength=1), "headless": _b("无头模式（默认 false）"),
                   "start_url": _s("启动后打开的页面")}, ["profile_id"]),
             lambda a: client.call("POST", "/api/v1/browser/start", {
                 "profile_id": a["profile_id"], "headless": a.get("headless"),
                 "start_url": a.get("start_url")})),
        Tool("stop_browser", "停止环境浏览器",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("POST", "/api/v1/browser/stop", {"profile_id": a["profile_id"]})),
        Tool("batch_start_browsers", "批量启动浏览器（并行，最多 50 个）",
             _obj({"profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"},
                   "headless": _b("无头模式"), "start_url": _s("统一启动页（可选）")}, ["profile_ids"]),
             lambda a: client.call("POST", "/api/v1/browser/start-batch",
                                   {"profile_ids": a["profile_ids"], "headless": a.get("headless"),
                                    "start_url": a.get("start_url")})),
        Tool("batch_stop_browsers", "批量停止浏览器",
             _obj({"profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"}},
                  ["profile_ids"]),
             lambda a: client.call("POST", "/api/v1/browser/stop-batch",
                                   {"profile_ids": a["profile_ids"]})),
        Tool("list_active", "正在运行的浏览器实例列表",
             _obj({}), lambda a: client.call("GET", "/api/v1/browser/active")),
        Tool("get_endpoint", "获取自动化端点（fp-chromium/chromium 返回 CDP ws_endpoint；camoufox 返回 Playwright Server ws）",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("GET", f"/api/v1/browser/{a['profile_id']}/endpoint")),

        # ======== 页面控制（所有内核通用） ========
        Tool("navigate", "让运行中的环境打开指定 URL（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1), "url": _s("目标 URL", minLength=1)}, ["profile_id", "url"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/navigate", {"url": a["url"]})),
        Tool("screenshot", "对运行中的环境截图，返回 base64 PNG",
             _obj({"profile_id": _s("环境 ID", minLength=1), "full_page": _b("全页截图")}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/screenshot",
                                   {"full_page": a.get("full_page", False)})),
        Tool("evaluate_js", "在环境当前页面执行 JS 并返回结果（谨慎：等同页面内权限）",
             _obj({"profile_id": _s("环境 ID", minLength=1), "expression": _s("JS 表达式", minLength=1)}, ["profile_id", "expression"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/evaluate",
                                   {"expression": a["expression"]})),
        Tool("click_element", "点击页面元素（CSS 选择器，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1),
                   "timeout": _i("超时毫秒（默认 30000）")}, ["profile_id", "selector"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/click",
                                   {"selector": a["selector"], "timeout": a.get("timeout", 30000)})),
        Tool("type_text", "在指定元素中清空并输入文本（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1),
                   "text": _s("输入文本"),
                   "press_enter": _b("输入后按回车")}, ["profile_id", "selector"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/type",
                                   {"selector": a["selector"], "text": a.get("text", ""),
                                    "press_enter": a.get("press_enter", False)})),
        Tool("press_key", "按下键盘按键（Enter/Tab/Escape 等，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "key": _s("按键名（如 Enter、Tab、Escape、Control+a）", minLength=1)}, ["profile_id", "key"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/press",
                                   {"key": a["key"]})),
        Tool("wait_ms", "等待指定毫秒数（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "ms": _i("等待毫秒（默认 1000）")}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/wait",
                                   {"ms": a.get("ms", 1000)})),
        Tool("wait_for_element", "等待页面元素出现（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1),
                   "timeout": _i("超时毫秒（默认 30000）")}, ["profile_id", "selector"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/wait_for",
                                   {"selector": a["selector"], "timeout": a.get("timeout", 30000)})),
        Tool("scroll_page", "垂直滚动页面（正数向下，负数向上，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "amount": _i("滚动像素（默认 500，负数向上）")}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/scroll",
                                   {"amount": a.get("amount", 500)})),
        Tool("hover_element", "悬停在元素上（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1)}, ["profile_id", "selector"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/hover",
                                   {"selector": a["selector"]})),
        Tool("select_option", "下拉选择（按 value 或可见文本，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1),
                   "value": _s("选项 value 或可见文本", minLength=1)}, ["profile_id", "selector", "value"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/select",
                                   {"selector": a["selector"], "value": a["value"]})),
        Tool("extract_elements", "抽取元素文本或属性值（返回数组，最多 50 项，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "selector": _s("CSS 选择器", minLength=1),
                   "attr": _s("属性名（可选，不传取 textContent）")}, ["profile_id", "selector"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/extract",
                                   {"selector": a["selector"], "attr": a.get("attr")})),
        Tool("get_page_info", "获取页面信息：标题、URL、Cookie（任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/page_info")),
        Tool("get_html", "获取页面 HTML 内容（可裁剪长度，任意内核）",
             _obj({"profile_id": _s("环境 ID", minLength=1),
                   "max_length": _i("最大返回长度（默认 50000）")}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/browser/{a['profile_id']}/get_html",
                                   {"max_length": a.get("max_length", 50000)})),
        Tool("readiness_check", "环境就绪度体检（约 20-60 秒）：出口 IP/时区/语言/WebRTC/自动化痕迹/Canvas 稳定等 17 项",
             _obj({"profile_id": _s("环境 ID", minLength=1)}, ["profile_id"]),
             lambda a: client.call("POST", f"/api/v1/profiles/{a['profile_id']}/readiness", {})),

        # ======== 代理 / 矩阵 ========
        Tool("proxy_test", "测试代理连通性与归属地；proxy_json 为空测直连",
             _obj({"proxy_json": _s('代理 JSON，如 {"scheme":"socks5","host":"1.2.3.4","port":1080}')}),
             lambda a: client.call("POST", "/api/v1/proxy/test",
                                   {"proxy": json.loads(a["proxy_json"]) if a.get("proxy_json") else None})),
        Tool("matrix_report", "指纹矩阵风控报告：特征分布/重复指纹组/拥挤项",
             _obj({}), lambda a: client.call("GET", "/api/v1/matrix/report")),
        Tool("matrix_regenerate", "矩阵风控重生成：为指定环境重新生成指纹，自动避开现有矩阵组合",
             _obj({"profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"}},
                  ["profile_ids"]),
             lambda a: client.call("POST", "/api/v1/matrix/regenerate",
                                   {"profile_ids": a["profile_ids"]})),

        # ======== RPA 任务 ========
        Tool("list_tasks", "RPA 任务列表",
             _obj({}), lambda a: client.call("GET", "/api/v1/tasks")),
        Tool("get_task", "查看任务详情（含完整步骤定义）",
             _obj({"task_id": _s("任务 ID", minLength=1)}, ["task_id"]),
             lambda a: client.call("GET", f"/api/v1/tasks/{a['task_id']}")),
        Tool("create_task", "创建 RPA 任务。steps_json 为步骤数组，每步 {action, ...参数}；"
             "动作：navigate/click/type/press/wait/wait_for/scroll/screenshot/extract/evaluate/hover/select/upload/download/"
             "tab_open/tab_switch/tab_close/set_var/label/goto/if；通用字段 frame/retry/on_error；"
             "extract/evaluate 可用 var 存变量，后续步骤 {{变量名}} 引用",
             _obj({"name": _s("任务名", minLength=1),
                   "steps_json": _s('步骤 JSON 数组字符串，如 [{"action":"navigate","url":"https://example.com"},{"action":"extract","selector":"h1","var":"title"}]'),
                   "webhook_url": _s("运行结束回调地址（可选）")}, ["name", "steps_json"]),
             lambda a: client.call("POST", "/api/v1/tasks", {
                 "name": a["name"],
                 "steps": json.loads(a["steps_json"]),
                 "webhook_url": a.get("webhook_url")})),
        Tool("update_task", "修改任务（名称/步骤/webhook）。steps_json 不传则不修改步骤",
             _obj({"task_id": _s("任务 ID", minLength=1),
                   "name": _s("新名称（可选）"),
                   "steps_json": _s("新步骤 JSON 数组（可选，传则覆盖）"),
                   "webhook_url": _s("webhook 回调地址（可选）")}, ["task_id"]),
             lambda a: client.call("PUT", f"/api/v1/tasks/{a['task_id']}",
                                   {k: v for k, v in {
                                       "name": a.get("name"),
                                       "steps": json.loads(a["steps_json"]) if a.get("steps_json") else None,
                                       "webhook_url": a.get("webhook_url"),
                                   }.items() if v is not None})),
        Tool("delete_task", "删除任务",
             _obj({"task_id": _s("任务 ID", minLength=1)}, ["task_id"]),
             lambda a: client.call("DELETE", f"/api/v1/tasks/{a['task_id']}")),
        Tool("run_task", "在指定环境上运行 RPA 任务，返回 run_id（用 task_runs 查结果）",
             _obj({"task_id": _s("任务 ID", minLength=1), "profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"},
                   "headless": _b("无头模式"), "auto_close": _b("运行后自动关闭浏览器"),
                   "humanize": _b("人机化节奏（步骤间随机延迟、逐字符输入）")},
                  ["task_id", "profile_ids"]),
             lambda a: client.call("POST", f"/api/v1/tasks/{a['task_id']}/run", {
                 "profile_ids": a["profile_ids"], "headless": a.get("headless", True),
                 "auto_close": a.get("auto_close", True), "humanize": a.get("humanize", False)})),
        Tool("task_runs", "任务运行记录（可按 task_id/profile_id 过滤）",
             _obj({"task_id": _s(), "profile_id": _s()}),
             lambda a: client.call("GET", "/api/v1/task-runs",
                                   {k: v for k, v in a.items() if v} or None)),
        Tool("get_task_run", "查看单条运行记录详情（含逐步结果）",
             _obj({"run_id": _s("运行记录 ID", minLength=1)}, ["run_id"]),
             lambda a: client.call("GET", f"/api/v1/task-runs/{a['run_id']}")),
        Tool("cancel_task_run", "取消正在运行的任务",
             _obj({"run_id": _s("运行记录 ID", minLength=1)}, ["run_id"]),
             lambda a: client.call("POST", f"/api/v1/task-runs/{a['run_id']}/cancel")),

        # ======== 定时调度 ========
        Tool("list_schedules", "定时调度计划列表",
             _obj({}), lambda a: client.call("GET", "/api/v1/schedules")),
        Tool("create_schedule", "创建定时调度。daily 需传 daily_time；interval 需传 interval_minutes；"
             "weekdays 为 [0-6]（周一到周日），空=每天",
             _obj({"name": _s("调度名", minLength=1),
                   "task_id": _s("任务 ID", minLength=1),
                   "kind": _s("daily=每日固定时刻 / interval=固定间隔"),
                   "daily_time": _s("每日时刻 HH:MM（daily 模式必填）"),
                   "interval_minutes": _i("间隔分钟（interval 模式必填）"),
                   "profile_ids": {"type": "array", "items": {"type": "string"}, "description": "环境 ID 列表"},
                   "timezone": _s("IANA 时区（如 Asia/Shanghai）"),
                   "weekdays": {"type": "array", "items": {"type": "integer"}, "description": "生效日 [0=周一..6=周日]"}},
                  ["name", "task_id", "kind", "profile_ids"]),
             lambda a: client.call("POST", "/api/v1/schedules", {
                 "name": a["name"], "task_id": a["task_id"], "kind": a["kind"],
                 "daily_time": a.get("daily_time"), "interval_minutes": a.get("interval_minutes"),
                 "profile_ids": a["profile_ids"], "timezone": a.get("timezone"),
                 "weekdays": a.get("weekdays"),
             })),
        Tool("update_schedule", "修改调度（仅传需修改字段）",
             _obj({"schedule_id": _s("调度 ID", minLength=1),
                   "name": _s("新名称"), "kind": _s("daily/interval"),
                   "daily_time": _s("HH:MM"), "interval_minutes": _i("间隔分钟"),
                   "profile_ids": {"type": "array", "items": {"type": "string"}},
                   "enabled": _b("启用/暂停"), "timezone": _s("IANA 时区"),
                   "weekdays": {"type": "array", "items": {"type": "integer"}}}, ["schedule_id"]),
             lambda a: client.call("PUT", f"/api/v1/schedules/{a['schedule_id']}",
                                   {k: v for k, v in {
                                       "name": a.get("name"), "kind": a.get("kind"),
                                       "daily_time": a.get("daily_time"),
                                       "interval_minutes": a.get("interval_minutes"),
                                       "profile_ids": a.get("profile_ids"),
                                       "enabled": a.get("enabled"),
                                       "timezone": a.get("timezone"),
                                       "weekdays": a.get("weekdays"),
                                   }.items() if v is not None})),
        Tool("delete_schedule", "删除调度",
             _obj({"schedule_id": _s("调度 ID", minLength=1)}, ["schedule_id"]),
             lambda a: client.call("DELETE", f"/api/v1/schedules/{a['schedule_id']}")),
        Tool("run_schedule_now", "立即触发调度（手动补跑）",
             _obj({"schedule_id": _s("调度 ID", minLength=1)}, ["schedule_id"]),
             lambda a: client.call("POST", f"/api/v1/schedules/{a['schedule_id']}/run-now")),
    ]


# ---------------------------------------------------------------- MCP 协议（stdio）

def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"\n…（结果过长，已截断，共 {len(text)} 字符）"


def make_handler(client: WorkbenchClient) -> Callable[[dict], Optional[dict]]:
    tools = {t.name: t for t in build_tools(client)}

    def handle(msg: dict) -> Optional[dict]:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        is_notification = msg_id is None

        if method == "initialize":
            # 协商协议版本：支持客户端请求的版本则原样回显，否则回落到本实现版本
            want = (msg.get("params") or {}).get("protocolVersion")
            version = want if isinstance(want, str) and want else PROTOCOL_VERSION
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"protocolVersion": version,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": SERVER_INFO}}
        if method.startswith("notifications/"):
            return None  # 通知不回包
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"tools": [{"name": t.name, "description": t.description,
                                          "inputSchema": t.inputSchema} for t in tools.values()]}}
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            tool = tools.get(name)
            if tool is None:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32602, "message": f"未知工具: {name}"}}
            try:
                result = tool.fn(params.get("arguments") or {})
                text = _clip(json.dumps(result, ensure_ascii=False, indent=1, default=str))
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": text}], "isError": False}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": f"工具执行失败：{e}"}],
                                   "isError": True}}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    return handle


def _read_settings_api_key() -> Optional[str]:
    try:
        data = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        return data.get("api_key")
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(prog="python -m app.mcp_server",
                                     description="FPWorkbench MCP Server（stdio）")
    parser.add_argument("--url", default=os.environ.get("FPWB_URL", "http://127.0.0.1:18080"),
                        help="工作台地址（默认 http://127.0.0.1:18080）")
    parser.add_argument("--api-key", default=os.environ.get("FPWB_API_KEY"),
                        help="API Key（缺省自动读取工作台 data/settings.json）")
    args = parser.parse_args(argv)

    api_key = args.api_key or _read_settings_api_key()
    client = WorkbenchClient(args.url, api_key)
    handler = make_handler(client)

    # MCP stdio 传输：每行一个 JSON-RPC 消息；stdout 只能输出协议消息
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                response = handler(msg)
            except Exception as e:
                response = {"jsonrpc": "2.0", "id": msg.get("id"),
                            "error": {"code": -32603, "message": f"Internal error: {e}"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
