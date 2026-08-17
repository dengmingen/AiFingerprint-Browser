# Playwright 对接指南

本工作台的所有环境均可被 **Playwright（Python / Node.js）直接驱动**，且驱动时保留环境的全部
反检测特性（固定指纹、字体/噪声种子、代理、WebRTC 防护、webdriver 伪装）。

## 1. 对接方式总览

| 内核 | 连接协议 | 连接方法 | 适用场景 |
|---|---|---|---|
| `camoufox` | **Playwright Server**（`ws://`） | `firefox.connect(ws_endpoint)` | 高隐蔽自动化（推荐）：Firefox 定制内核，无 CDP 检测面 |
| `fp-chromium` | **CDP**（`ws://`） | `chromium.connect_over_cdp(ws_endpoint)` | Chrome 生态：需要 CDP/Selenium 体系组件、Chrome 扩展 |
| `chromium` | CDP | 同上 | 本地调试（无指纹伪装） |

> 两种连接拿到的都是**真实浏览器进程**：页面内 `navigator.webdriver=false`、
> TLS/HTTP2 指纹天然正确（非模拟）。指纹与该环境在界面中启动时完全一致
> （同一套固定种子驱动），已由自动化测试验证逐字一致。

## 2. 核心 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/browser/{id}/playwright-server` | 为 camoufox 环境启动 Playwright Server，返回 `ws_endpoint`（body 可选 `{"port": 9700}` 固定端口） |
| DELETE | `/api/v1/browser/{id}/playwright-server` | 停止该环境的 Playwright Server |
| GET | `/api/v1/browser/{id}/endpoint` | **统一端点查询**：fp-chromium/chromium 返回 CDP `ws_endpoint`（环境需在运行中）；camoufox 返回 Playwright Server 端点 |
| GET | `/api/v1/playwright-servers` | 列出全部运行中的 Playwright Server（admin） |
| POST | `/api/v1/browser/start` / `stop` | 环境生命周期（fp-chromium 需先 start 才有 CDP 端点） |

所有请求响应格式 `{"code":0,"msg":"success","data":...}`；开启 API Key 认证后带
`X-API-Key` 请求头。

## 3. Python 快速开始

前置：`pip install playwright httpx`（本仓库虚拟环境已内置 playwright，无需再装浏览器）。

### 3.1 camoufox（推荐：Playwright Server 模式）

```python
import asyncio
import httpx
from playwright.async_api import async_playwright

API = "http://127.0.0.1:18080/api/v1"
PROFILE_ID = "<环境ID>"          # 在「环境管理」界面创建/复制


async def main():
    # 1) 启动 Playwright Server（指纹/代理已按环境配置注入浏览器进程）
    async with httpx.AsyncClient(base_url=API, timeout=120) as api:
        r = (await api.post(f"/browser/{PROFILE_ID}/playwright-server", json={}))
        assert r.json()["code"] == 0, r.json()
        ws = r.json()["data"]["ws_endpoint"]
        print("Playwright 端点:", ws)

        # 2) 标准 Playwright 连接
        async with async_playwright() as pw:
            browser = await pw.firefox.connect(ws, timeout=60_000)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://example.com")
            print("UA:", await page.evaluate("navigator.userAgent"))

            # 3) 业务操作（选择器/等待/截图等同标准 Playwright 用法）
            print("标题:", await page.title())

            # 4) 登录态持久化（见第 5 节）
            state = await context.storage_state(path=f"states/{PROFILE_ID}.json")
            await context.close()
            await browser.close()      # 仅断开连接，Server 继续运行

        # 5) 用完关闭 Server（也可常驻复用）
        await api.delete(f"/browser/{PROFILE_ID}/playwright-server")

asyncio.run(main())
```

要点：
- `browser.close()` 只是断开连接；浏览器 Server 保持运行，可反复 `connect` 复用
- **每次 `new_context()` 都是全新上下文**（无历史 Cookie），登录态请配合
  `storage_state` 使用（见第 5 节）
- 一个环境可同时开"界面交互式启动"和"Playwright Server"，互不冲突
  （Server 模式不占用环境数据目录）

### 3.2 fp-chromium（CDP 模式）

```python
import httpx
from playwright.async_api import async_playwright

API = "http://127.0.0.1:18080/api/v1"
PROFILE_ID = "<环境ID>"

async def main():
    async with httpx.AsyncClient(base_url=API, timeout=120) as api:
        # 1) 启动环境（返回 CDP 端点；也可稍后用 /endpoint 查询）
        r = await api.post("/browser/start", json={"profile_id": PROFILE_ID,
                                                   "headless": True})
        ws = r.json()["data"]["ws_endpoint"]

        # 2) CDP 直连（Selenium 4 / Puppeteer 也可用同一端点）
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws, timeout=60_000)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://example.com")
            print("UA:", await page.evaluate("navigator.userAgent"))
            await context.close()
            await browser.close()      # 断开连接，环境仍由工作台管理

        # 3) 停止环境
        await api.post("/browser/stop", json={"profile_id": PROFILE_ID})
```

fp-chromium 的指纹由环境固定种子在内核层驱动（Chrome 系 UA/GPU/Canvas），
`--fingerprint=<种子>` 等参数由工作台自动注入，断开连接后环境仍受工作台
生命周期管理（审计/统计/批量停止均有效）。

## 4. Node.js 示例

```js
// npm i playwright@latest node-fetch  （playwright 仅作客户端，无需下载浏览器）
import { firefox, chromium } from "playwright";

const API = "http://127.0.0.1:18080/api/v1";

// camoufox：Playwright Server
const r = await fetch(`${API}/browser/${PROFILE_ID}/playwright-server`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
});
const { ws_endpoint } = (await r.json()).data;
const browser = await firefox.connect(ws_endpoint);
const page = await browser.newPage();
await page.goto("https://example.com");
console.log(await page.title());

// fp-chromium：CDP
const r2 = await fetch(`${API}/browser/start`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ profile_id: PROFILE_ID2, headless: true }),
});
const cdp = (await r2.json()).data.ws_endpoint;
const chrome = await chromium.connectOverCDP(cdp);
```

> Playwright 各语言实现（Java/.NET/Python/Node）均支持 `connect` / `connectOverCDP`，
> 对接方式完全一致。

## 5. 登录态（Cookie）管理最佳实践

Server/CDP 连接创建的 context 不与环境的交互式数据目录互通，推荐按环境持久化
storage_state：

```python
STATE = f"states/{PROFILE_ID}.json"

# 登录后保存
await context.storage_state(path=STATE)

# 下次连接时恢复（指纹不变 = 同一台"设备"，登录态延续）
context = await browser.new_context(storage_state=STATE
                                    if os.path.exists(STATE) else None)
```

- 需要人工登录态（交互式 Cookie）直接复用时，改用工作台界面启动同一环境
- 环境导出/导入（含数据目录归档）携带的是交互式数据；storage_state 文件请自行备份

## 6. 注意事项

1. **无头 vs 有头**：风控严格站点（Cloudflare 等）建议 `headless: false`（环境启动配置或
   `browser/start` 时指定），无头特征更易被挑战；camoufox Server 模式默认继承环境配置
2. **代理与属地**：环境配置了代理 + geoip 时，Playwright 连接中的页面自动走该代理，
   时区/语言/经纬度自动对齐出口 IP；无需在 Playwright 侧重设
3. **并发**：一个 ws 端点可建立多个连接、每个连接多个 context；批量任务建议
   每环境 ≤3 并发，配合工作台的 RPA 人机化节奏使用
4. **认证**：开启 API Key 后所有工作台请求需带 `X-API-Key`；ws 连接本身由工作台
   随机端口保护（可传 `port` 固定端口便于防火墙放行）
5. **资源回收**：Playwright Server 进程随工作台关闭而终止；长期不用记得 DELETE
   或在环境删除时自动清理

## 7. 与 RPA 任务引擎的关系

- **简单流程**（打开/点击/抽取/截图）→ 直接用工作台 RPA 任务（UI 编排/定时调度/Webhook），无需写代码
- **复杂业务逻辑**（登录链路、断言、数据管道）→ Playwright 对接（本文档）
- 两者可组合：RPA 做例行养号，Playwright 做业务自动化，共用同一套环境与指纹

## 8. 常见问题

**Q: `connect` 报 `ws endpoint connect timeout`？**
A: 端点绑定在 `127.0.0.1`/`[::1]`，请确认客户端与工作台同机；跨机使用需放行端口并
改用固定 `port` 启动 Server，且工作台以 `--host 0.0.0.0` 监听。

**Q: 连接后 UA 和界面里显示的不一样？**
A: camoufox 应完全一致（同一指纹）；fp-chromium 界面显示"内核种子驱动"，
实际呈现 Chrome 系 UA——以页面内 `navigator.userAgent` 为准。

**Q: Selenium / Puppetette 能用吗？**
A: fp-chromium 的 CDP 端点兼容 Selenium 4（`webdriver.ChromeOptions.debugger_address`）
与 Puppeteer（`puppeteer.connect({browserWSEndpoint})`）。
