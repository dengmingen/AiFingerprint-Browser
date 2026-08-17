# MCP 对接指南（Model Context Protocol）

FPWorkbench 内置 MCP Server，将工作台全部 API 包装为 **48 个标准 MCP 工具**，
Claude Desktop / Cursor / 其他 MCP 客户端可通过 stdio 直接调用，实现 AI 驱动的
环境管理、浏览器控制（点击/输入/滚动等页面交互）、RPA 任务、矩阵风控等全流程操作。

## 1. 前置条件

- 工作台服务已在运行（默认 `http://127.0.0.1:18080`）
- MCP 客户端（Claude Desktop ≥ 0.7、Cursor ≥ 0.45 等）

## 2. 配置方法

### 2.1 Claude Desktop

编辑 Claude Desktop 配置文件：

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "fpworkbench": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "B:\\指纹浏览器",
      "env": {
        "FPWB_URL": "http://127.0.0.1:18080",
        "FPWB_API_KEY": ""
      }
    }
  }
}
```

> `args` 中的路径需要指向项目根目录（包含 `app` 包）。
> 如果使用虚拟环境，`command` 改为 `.venv\\Scripts\\python.exe`。
> `FPWB_API_KEY` 留空则自动从工作台 `data/settings.json` 读取（关闭认证模式）。

### 2.2 Cursor

Cursor 的 MCP 配置在项目级 `.cursor/mcp.json` 或全局设置中：

**项目级**（`项目根/.cursor/mcp.json`）：
```json
{
  "mcpServers": {
    "fpworkbench": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": ".",
      "env": {
        "FPWB_URL": "http://127.0.0.1:18080"
      }
    }
  }
}
```

**全局级**（Cursor Settings → MCP → Add）：
- Command: `python`
- Args: `-m app.mcp_server`
- Working Directory: `B:\指纹浏览器`（项目根目录）

### 2.3 命令行调试

```bash
# 直接运行 MCP Server，手动发送 JSON-RPC 测试
.venv\Scripts\python -m app.mcp_server --url http://127.0.0.1:18080

# 也可指定 API Key
.venv\Scripts\python -m app.mcp_server --url http://127.0.0.1:18080 --api-key YOUR_KEY
```

输入一行 JSON-RPC 消息后按回车，服务器返回一行 JSON-RPC 响应：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}
→ {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"fpworkbench","version":"0.3.0","title":"FPWorkbench 指纹浏览器工作台"}}}
```

## 3. 可用工具列表（48 个）

### 3.1 系统（1 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `status` | 工作台状态（版本、内核可用性、内核健康） | 无 |

### 3.2 环境管理（9 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `list_profiles` | 环境列表 | `kernel`（可选，按内核过滤） |
| `get_profile` | 查看单个环境详情（含完整指纹） | `profile_id` |
| `create_profile` | 创建环境 | `name`, `kernel`, `fingerprint_mode`, `target_os`, `group`, `proxy_json`, `launch_json` |
| `update_profile` | 修改环境配置（名称/分组/代理/指纹重生成等） | `profile_id`, `updates_json` |
| `delete_profile` | 删除环境（需先停止） | `profile_id` |
| `batch_create_profiles` | 按模板批量创建（支持代理池） | `count`, `template_json`, `proxy_pool_json` |
| `batch_delete_profiles` | 批量删除环境 | `profile_ids` |
| `export_profile` | 导出环境为 JSON（可选含数据目录） | `profile_id`, `include_data` |
| `import_profile` | 导入环境 | `profile_json` |

### 3.3 浏览器启停（6 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `start_browser` | 启动环境浏览器（返回调试端点） | `profile_id`, `headless`, `start_url` |
| `stop_browser` | 停止环境浏览器 | `profile_id` |
| `batch_start_browsers` | 批量启动浏览器（并行，最多 50 个） | `profile_ids`, `headless`, `start_url` |
| `batch_stop_browsers` | 批量停止浏览器 | `profile_ids` |
| `list_active` | 正在运行的浏览器实例列表 | 无 |
| `get_endpoint` | 获取自动化端点（Playwright ws / CDP ws） | `profile_id` |

### 3.4 页面控制（15 个，所有内核通用）

> 所有内核（camoufox / fp-chromium / chromium）均可使用。camoufox 通过内置页面引用操作，
> fp-chromium/chromium 通过 CDP 临时连接操作。

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `navigate` | 打开 URL | `profile_id`, `url` |
| `screenshot` | 截图（返回 base64 PNG） | `profile_id`, `full_page` |
| `evaluate_js` | 执行 JavaScript | `profile_id`, `expression` |
| `click_element` | 点击元素 | `profile_id`, `selector`, `timeout` |
| `type_text` | 在元素中输入文本 | `profile_id`, `selector`, `text`, `press_enter` |
| `press_key` | 按键（Enter/Tab/Escape 等） | `profile_id`, `key` |
| `wait_ms` | 等待指定毫秒 | `profile_id`, `ms` |
| `wait_for_element` | 等待元素出现 | `profile_id`, `selector`, `timeout` |
| `scroll_page` | 垂直滚动（正=向下，负=向上） | `profile_id`, `amount` |
| `hover_element` | 悬停在元素上 | `profile_id`, `selector` |
| `select_option` | 下拉选择 | `profile_id`, `selector`, `value` |
| `extract_elements` | 抽取元素文本/属性（返回数组） | `profile_id`, `selector`, `attr` |
| `get_page_info` | 获取页面 title/url/cookies | `profile_id` |
| `get_html` | 获取页面 HTML | `profile_id`, `max_length` |
| `readiness_check` | 环境就绪度体检（17 项，约 20-60 秒） | `profile_id` |

### 3.5 代理 / 矩阵风控（3 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `proxy_test` | 测试代理连通性与归属地 | `proxy_json`（空测直连） |
| `matrix_report` | 指纹矩阵风控报告 | 无 |
| `matrix_regenerate` | 矩阵风控重生成风险指纹 | `profile_ids` |

### 3.6 RPA 任务（9 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `list_tasks` | 任务列表 | 无 |
| `get_task` | 查看任务详情（含步骤） | `task_id` |
| `create_task` | 创建任务 | `name`, `steps_json`, `webhook_url` |
| `update_task` | 修改任务（名称/步骤/webhook） | `task_id`, `name`, `steps_json`, `webhook_url` |
| `delete_task` | 删除任务 | `task_id` |
| `run_task` | 运行任务 | `task_id`, `profile_ids`, `headless`, `auto_close`, `humanize` |
| `task_runs` | 运行记录列表 | `task_id`, `profile_id`（可选过滤） |
| `get_task_run` | 查看运行详情（含逐步结果） | `run_id` |
| `cancel_task_run` | 取消运行中的任务 | `run_id` |

### 3.7 定时调度（5 个）

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `list_schedules` | 调度计划列表 | 无 |
| `create_schedule` | 创建调度 | `name`, `task_id`, `kind`, `profile_ids`, `daily_time`, `interval_minutes`, `timezone`, `weekdays` |
| `update_schedule` | 修改调度 | `schedule_id`, `name`, `kind`, `enabled` 等 |
| `delete_schedule` | 删除调度 | `schedule_id` |
| `run_schedule_now` | 立即触发调度（手动补跑） | `schedule_id` |

## 4. 使用场景示例

### 4.1 AI 直接操作网页（页面交互）

这是 MCP 扩展后的核心能力——AI 可以像真人一样在指纹浏览器中操作网页：

```
用户：启动"店铺A"环境，打开淘宝首页，搜索"蓝牙耳机"，截个图
Claude：[调用 start_browser(profile_id="p_abc123", start_url="https://taobao.com")]
       [调用 wait_for_element(profile_id="p_abc123", selector="#q", timeout=10000)]
       [调用 type_text(profile_id="p_abc123", selector="#q", text="蓝牙耳机", press_enter=true)]
       [调用 wait_ms(profile_id="p_abc123", ms=3000)]
       [调用 screenshot(profile_id="p_abc123")]
       → 截图已返回（base64 PNG）

用户：帮我看看搜索结果前 5 条的标题
Claude：[调用 extract_elements(profile_id="p_abc123", selector=".items .title")]
       → 返回 5 条标题文本

用户：获取页面 Cookie，看看有没有登录态
Claude：[调用 get_page_info(profile_id="p_abc123")]
       → 返回页面标题、URL 和所有 Cookie 信息
```

### 4.2 环境批量管理

```
用户：批量创建 10 个"店铺"环境，用这批代理轮换
Claude：[调用 batch_create_profiles(count=10, template_json={...}, proxy_pool_json=[...])]

用户：全部启动并打开不同页面
Claude：[调用 batch_start_browsers(profile_ids=[...], start_url="https://...")]

用户：查看哪些还在运行
Claude：[调用 list_active]

用户：下班了，全部关掉
Claude：[调用 batch_stop_browsers(profile_ids=[...])]
```

### 4.3 RPA 任务全生命周期

```
用户：创建一个任务：打开百度，搜索"指纹浏览器"，截取第一条结果标题
Claude：[调用 create_task(name="百度搜索", steps_json=[...])]

用户：先在"测试环境"上跑一下看看效果
Claude：[调用 run_task(task_id="t_xxx", profile_ids=["p_test"], headless=false)]
       [调用 task_runs(task_id="t_xxx")]
       → 查看运行结果

用户：效果不错，改成每天早上 9 点在 3 个环境上自动跑
Claude：[调用 create_schedule(name="每日搜索", task_id="t_xxx", kind="daily",
         daily_time="09:00", profile_ids=["p1","p2","p3"],
         timezone="Asia/Shanghai")]
```

### 4.4 矩阵风控检查与修复

```
用户：检查指纹矩阵有没有问题
Claude：[调用 matrix_report]
       → 发现环境 A 和环境 B 的 UA+屏幕+GPU 完全一致（高危）

用户：帮它们重新生成指纹
Claude：[调用 matrix_regenerate(profile_ids=["p_A", "p_B"])]
       → 已为 2 个环境重生成指纹，自动避开现有矩阵组合
```

### 4.5 fp-chromium 内核页面操作

```
用户：在 fp-chromium 环境中点击登录按钮
Claude：[调用 click_element(profile_id="p_fp", selector="#login-btn")]
       → 通过 CDP 临时连接执行，无需 Playwright Server
```

## 5. 参数格式说明

### create_profile 的 proxy_json

```json
{"scheme": "socks5", "host": "1.2.3.4", "port": 1080, "username": "user", "password": "pass"}
```

支持 `http`/`https`/`socks4`/`socks5`，留空或不传为直连。

### create_profile 的 launch_json

```json
{
  "geoip": true,
  "preset": "cloudflare",
  "start_url": "https://example.com",
  "headless": false,
  "disable_coop": true
}
```

### update_profile 的 updates_json

```json
{
  "name": "新名称",
  "group_name": "新分组",
  "proxy": {"scheme": "socks5", "host": "1.2.3.4", "port": 1080},
  "regen_fingerprint": true,
  "clear_proxy": false
}
```

仅传需修改字段，空对象 `{}` 不做任何修改。

### batch_create_profiles 的 template_json

```json
{
  "name": "店铺",
  "kernel": "camoufox",
  "target_os": "windows",
  "fingerprint_mode": "preset",
  "proxy": {"scheme": "socks5", "host": "1.2.3.4", "port": 1080}
}
```

`proxy_pool_json`（可选）为代理数组，依次分配给各环境。

### create_task 的 steps_json

每步格式：`{"action": "动作名", ...参数}`。21 种动作：

| 动作 | 参数 |
|---|---|
| `navigate` | `url` |
| `click` | `selector` |
| `type` | `selector`, `text` |
| `press` | `key` |
| `wait` | `ms` |
| `wait_for` | `selector`, `timeout` |
| `scroll` | `amount` |
| `screenshot` | `full_page?` |
| `extract` | `selector`, `attribute`（可选）, `var`（可选） |
| `evaluate` | `expression`, `var`（可选） |
| `hover` | `selector` |
| `select` | `selector`, `value` |
| `upload` | `selector`, `path` |
| `download` | `url` |
| `tab_open` | `url` |
| `tab_switch` | `value`（序号或标题） |
| `tab_close` | 无 |
| `set_var` | `name`, `value` |
| `label` | `name`（goto 跳转锚点） |
| `goto` | `label`（跳转到指定 label） |
| `if` | `var`, `op`（equals/contains/exists）, `value`, `then_goto`, `else_goto?` |

通用可选字段（所有步骤均可附加）：
- `frame`：iframe 选择器，在指定 iframe 内执行
- `retry`：失败重试次数（默认 0）
- `on_error`：失败时行为（`abort`/`continue`/`goto:标签名`）

### create_schedule

| 参数 | 说明 |
|---|---|
| `kind` | `daily`=每日固定时刻 / `interval`=固定间隔 |
| `daily_time` | `HH:MM`（daily 模式必填） |
| `interval_minutes` | 间隔分钟数（interval 模式必填） |
| `timezone` | IANA 时区名（如 `Asia/Shanghai`） |
| `weekdays` | `[0-6]`（0=周一…6=周日），空=每天 |

## 6. 注意事项

1. **MCP Server 不直接操作浏览器和数据库**：它是对工作台 HTTP API 的薄包装，
   浏览器生命周期仍由 Web 服务统一管理
2. **认证**：工作台开启 API Key 认证后，MCP Server 通过 `FPWB_API_KEY` 环境变量
   或 `--api-key` 参数传递密钥
3. **工作台需先运行**：MCP Server 通过 HTTP 请求工作台，工作台未启动时所有工具调用会报连接错误
4. **结果截断**：单次工具返回的文本上限 12000 字符，超出会被截断
5. **协议兼容**：支持 MCP 协议版本协商，自动回显客户端请求的版本号
6. **页面交互所有内核通用**：click/type/scroll 等 13 个页面控制工具支持 camoufox、fp-chromium、chromium 三种内核。
   fp-chromium/chromium 通过 CDP 临时连接执行，每次请求自动连接/断开
7. **CDP 连接开销**：非 camoufox 内核的页面交互每次请求会建立临时 CDP 连接，有约 1-2 秒的额外开销。
   频繁操作场景建议使用 RPA 任务或通过 `get_endpoint` 获取 CDP 端点后用 Playwright 直连

## 7. 协议细节

- 传输层：stdio（标准输入/输出），每行一个 JSON-RPC 2.0 消息
- 支持的方法：`initialize`、`ping`、`tools/list`、`tools/call`、`notifications/*`
- 错误码：`-32700`（解析错误）、`-32601`（方法不存在）、`-32602`（无效参数）、`-32603`（内部错误）
