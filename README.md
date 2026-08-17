# 指纹浏览器工作台（FPWorkbench）

本地化反检测浏览器（指纹浏览器）：基于 **Camoufox**（Firefox 定制内核，C++ 层指纹改写）
与 **fingerprint-chromium**（Chromium 定制内核），提供环境（Profile）管理、每环境固定指纹、
代理绑定、RPA 任务与定时调度、指纹矩阵风控、团队协作与自托管同步。
技术调研见《[指纹浏览器开发调研报告.md](指纹浏览器开发调研报告.md)》，
当前已完成 **Phase 1（MVP）、Phase 2（专业级）、Phase 3（团队与规模化）、Phase 4（智能化与稳定性升级）**。

## 功能特性

### 环境与指纹
- **环境管理**：每个环境独立的 Cookie/存储（持久化 user-data-dir）、独立指纹身份、独立代理，支持分组与备注、使用统计（启动次数/最近启动）
- **指纹固定**：创建环境时生成一次指纹，跨重启保持一致——UA/平台/硬件参数/屏幕/WebGL/字体子集/语音列表/Canvas·Audio·字体间距噪声种子全部固定
- **双指纹模式**：合成生成（BrowserForge 真实统计分布，无限独特组合）或 真实预设（Camoufox 内置真实设备指纹库，泛化 WebGL 自动替换真实 GPU 参数）
- **指纹健康分**：创建/更新时自动做跨信号一致性体检（UA↔platform↔GPU↔屏幕↔硬件参数），矛盾项警示
- **三内核**：
  - `camoufox`（推荐）：Firefox 定制，C++ 层指纹伪装
  - `fp-chromium`：[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) 定制 Chromium，内核层伪装 + **标准 CDP 端点**（Selenium/Puppeteer/Playwright 可直连），种子驱动 Canvas/WebGL/GPU 伪造，自带 navigator.webdriver=false 与 CDP 检测规避。安装：下载 Release 解压后设置环境变量 `FPWB_FPCHROMIUM` 或放入 `tools/fp-chromium/`
  - `chromium`：系统浏览器直启（无伪装，调试用）

### 矩阵风控（Phase 3）
- **分布统计**：OS / GPU / 屏幕 / CPU 核心的特征分布可视化
- **查重扫描**：检测多个环境呈现相同指纹（UA+屏幕+GPU 完全一致）→ 高危
- **拥挤检测**：同一 GPU+屏幕组合被 ≥4 个环境共用 → 中危
- **矩阵感知重生成**：一键重生成风险环境指纹，生成时避开现有矩阵指纹组合（有界重试 + 拥挤度偏好），防止"重生成又撞车"

### 团队协作（Phase 3）
- **多成员**：admin / operator 双角色，成员级独立 API Key（仅创建时显示一次）
- **数据隔离**：operator 只能看到和操作自己创建的环境，越权访问返回 403
- **审计归属**：所有变更操作记录操作成员，认证拒绝事件入审计日志

### 自托管同步（Phase 3）
- 任意实例可开启**同步服务器**（独立 X-Sync-Token 保护）
- 其它节点配置远端后即可 **push / pull** 环境配置与指纹（不含 Cookie，多机矩阵/云端备份）
- LWW（最新修改优先）合并 + **删除墓碑自动传播**；`--home` 参数支持多实例独立数据目录
- **自动同步推送**：开启后每 30 分钟自动向远端推送变更（系统设置中 `auto_sync` 开关）

### 批量与流转
- **批量操作**：按模板批量创建（支持代理池依次分配）、批量启动/停止/删除/导出
- **导入/导出**：单环境或批量导出为可移植 JSON（可选含数据目录归档，Cookie 一并迁移）；一键导入
- **整机备份/恢复**：全部环境配置与指纹的备份下载与恢复

### 自动化
- **本地 API**：AdsPower 风格响应（`{"code":0,"msg":"success","data":...}`），65 条路由（`/docs` 查看 Swagger）
- **Playwright 正式对接**（详见 [docs/Playwright对接指南.md](docs/Playwright对接指南.md)）：
  - camoufox：`POST /browser/{id}/playwright-server` 暴露标准 Playwright Server 端点，
    `firefox.connect(ws://...)` 直连，指纹与界面启动完全一致
  - fp-chromium：启动环境即得 CDP 端点，`chromium.connect_over_cdp(ws://...)` / Selenium 4 / Puppeteer 直连
- **RPA 任务引擎**：21 类步骤可视化编排（打开页面/点击/输入/按键/等待/等待元素/滚动/截图/抽取/执行JS/
  悬停/下拉选择/上传/下载/多标签管理/变量设置/标签跳转/条件分支），支持 `{{变量名}}` 插值、
  失败重试（abort/continue/goto）、iframe 内操作，多环境并发执行，逐步留痕、截图存档、结果可查
- **定时调度**：每日固定时刻或固定间隔自动执行任务；**IANA 时区支持**（Asia/Shanghai 等），
  **周几过滤**（可选仅周一/三/五执行），**停机错过补跑**（服务重启后补触发当日未运行计划），可暂停/立即运行
- **Webhook 通知**：任务运行结束回调运行结果 JSON（对接企微/钉钉/飞书机器人或自有系统）
- **代理**：HTTP/HTTPS/SOCKS4/SOCKS5，按环境绑定，可选按出口 IP 自动对齐时区/经纬度/语言（geoip），WebRTC IP 自动伪造，一键测试连通性与归属地

### 安全与审计
- **API Key 认证**：成员级密钥（X-API-Key），本地单用户模式可关闭
- **代理密码加密存储**：Fernet 加密落库（`enc:` 前缀），数据库文件不泄露明文
- **代理认证转发器**：上游代理需认证时，自动创建本地无认证中间端口转发（无需将密码暴露给 Playwright/Selenium 脚本）
- **审计日志**：全部变更操作、认证与同步拒绝事件记录在案（保留最近 5000 条），界面可查

### AI 集成（Phase 4）
- **MCP Server**：标准 Model Context Protocol（stdio JSON-RPC 2.0），提供 48 个工具覆盖环境管理、
  浏览器页面交互（点击/输入/滚动等，所有内核通用）、RPA 任务、定时调度、矩阵风控等全流程。
  Claude Desktop / Cursor 直接调用，配置示例见 [docs/MCP对接指南.md](docs/MCP对接指南.md)

### 管理界面
中文 Web UI（无外部依赖）：环境管理（多选批量栏、健康分徽章、指纹详情、使用统计）、
RPA 任务（步骤编辑器、运行记录、截图预览、Webhook）、调度计划、矩阵风控（分布图/重复组/一键重生成）、
审计日志（过滤/分页）、系统设置（成员管理/认证/同步/备份）六页签，内置 CreepJS/BrowserLeaks/Pixelscan 等自检链接。
- **内核健康公告**：服务启动时自动检查 Camoufox GitHub 状态，界面与 API 实时展示维护预警或分支推荐

### 浏览器插件（Chrome/Edge）
[extension/](extension/README.md)：一键连接（配对码自动换取密钥）、环境启停面板、
**右键任意链接 → 在指纹环境中打开**、当前页发送到环境。安装：`chrome://extensions`
开启开发者模式 → 加载已解压的扩展程序 → 选择 `extension` 目录。

## 快速开始

### 方式一：一键启动器（推荐）

双击项目根目录的 **`一键启动工作台.bat`** 即可——首次运行自动完成全部配置
（Python 虚拟环境 → 依赖安装（失败自动切换清华镜像）→ Camoufox 内核下载），
随后启动服务并自动打开管理界面；以后每次双击直接启动。

| 文件 | 作用 |
|---|---|
| `一键启动工作台.bat` | 一键启动（缺什么自动补什么）并打开界面 |
| `停止工作台.bat` | 停止服务（含浏览器与 Playwright 子进程） |
| `创建桌面快捷方式.bat` | 在桌面创建带图标的快捷方式 |
| `launcher/设置开机自启.bat` / `取消开机自启.bat` | 开机自动启动开关 |

也可用引擎直接调用：`powershell -File launcher/workbench.ps1 -Action check|setup|start|stop`

### 方式二：命令行

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 下载 Camoufox 浏览器内核（约 500MB，一次性）
.venv\Scripts\python -m camoufox fetch

# 3. 启动服务
.venv\Scripts\python run.py            # 默认 http://127.0.0.1:18080
```

浏览器打开 `http://127.0.0.1:18080` 进入管理界面；`/docs` 查看 Swagger 文档。

> 若 `camoufox fetch` 下载 uBlock 扩展失败（AMO 451 地区封锁），服务会自动降级为不加载扩展，
> 也可手动修复：从 [uBlock GitHub Releases](https://github.com/gorhill/uBlock/releases) 下载
> `.firefox.signed.xpi` 解压到 `%LOCALAPPDATA%\camoufox\camoufox\Cache\addons\UBO\`。

## API 一览（节选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/status` | 服务与内核状态 |
| GET/POST | `/api/v1/profiles` | 环境列表 / 创建（fingerprint_mode: generate/preset；kernel: camoufox/fp-chromium/chromium） |
| POST | `/api/v1/profiles/batch` `batch-delete` | 批量创建（模板+代理池）/ 批量删除 |
| GET | `/api/v1/profiles/{id}/export` / POST `/profiles/import` | 导出（?include_data=true 含数据目录）/ 导入 |
| GET/POST | `/api/v1/system/backup` `/system/restore` | 整机备份 / 恢复 |
| POST | `/api/v1/browser/start` `start-batch` `stop` `stop-batch` | 启停（含批量；fp-chromium 返回 ws_endpoint） |
| POST | `/api/v1/browser/{id}/navigate` `screenshot` `evaluate` | 页面控制（camoufox 直接） |
| POST | `/api/v1/browser/{id}/click` `type` `press` `wait` `wait_for` | 页面交互（所有内核通用，CDP 桥接） |
| POST | `/api/v1/browser/{id}/scroll` `hover` `select` `extract` | 页面交互（所有内核通用） |
| POST | `/api/v1/browser/{id}/page_info` `get_html` | 页面信息（所有内核通用） |
| GET/POST/PUT/DELETE | `/api/v1/tasks` | RPA 任务 CRUD（21 类步骤 + webhook_url） |
| POST | `/api/v1/tasks/{id}/run` / GET `/task-runs` / POST `/{id}/cancel` | 运行 / 记录 / 取消 |
| GET/POST/PUT/DELETE | `/api/v1/schedules` / POST `/{id}/run-now` | 定时调度 CRUD 与立即运行 |
| GET | `/api/v1/matrix/report` / POST `/matrix/regenerate` | 矩阵风控扫描 / 风险指纹重生成 |
| GET/POST/DELETE | `/api/v1/members` / POST `/{id}/toggle` | 团队成员管理（admin） |
| GET/POST | `/api/v1/settings` | 认证 / 同步服务器 / 远端配置 |
| POST | `/api/v1/sync/push` `pull` | 推送 / 拉取远端同步服务器 |
| GET | `/api/v1/audit-logs` | 审计日志 |
| POST | `/api/v1/proxy/test` | 测试代理（body 的 proxy 为 null 时测直连） |

创建并启动一个带代理的环境（curl）：

```bash
curl -X POST http://127.0.0.1:18080/api/v1/profiles -H "Content-Type: application/json" -d '{
  "name": "店铺A", "fingerprint_mode": "preset",
  "proxy": {"scheme": "socks5", "host": "127.0.0.1", "port": 7890},
  "launch": {"geoip": true, "start_url": "https://www.baidu.com"}
}'

curl -X POST http://127.0.0.1:18080/api/v1/browser/start -H "Content-Type: application/json" \
  -d '{"profile_id": "<上一步返回的 id>"}'
```

开启 API Key 认证后，所有请求加 `-H "X-API-Key: <密钥>"`。

## 目录结构

```
app/
├── config.py             # 路径（支持 FPWB_HOME 多实例）、端口、角色常量
├── models.py             # API 数据模型
├── db.py                 # SQLite：环境/任务/运行/审计/成员/调度/同步墓碑
├── security.py           # API Key + Fernet 字段加密 + 同步设置
├── fingerprints.py       # BrowserForge 指纹序列化基建
├── fingerprint_engine.py # 双模式指纹生成 + WebGL 修复 + 一致性体检
├── launcher.py           # 浏览器实例生命周期管理（三内核）
├── task_engine.py        # RPA 任务引擎（21 步骤/变量/重试/分支/iframe/多标签/CDP）
├── scheduler.py          # 定时调度循环（时区/周几/补跑/自动同步）
├── mcp_server.py         # MCP Server（stdio JSON-RPC 2.0，19 工具）
├── matrix.py             # 指纹矩阵风控（分布/查重/矩阵感知重生成）
├── sync.py               # 自托管同步（push/pull/LWW/删除传播）
├── transfer.py           # 导入导出 + 整机备份恢复
├── proxy_test.py         # 代理连通性测试
├── proxy_forwarder.py    # 代理认证转发器（本地无认证端口→上游认证代理）
├── readiness.py          # 环境就绪度体检（17 项检测）
├── kernels/
│   ├── camoufox_kernel.py   # Camoufox 内核（Firefox 定制）
│   ├── fp_chromium_kernel.py# fingerprint-chromium 内核（Chromium 定制+CDP）
│   └── chromium_kernel.py   # 系统 Chrome/Edge（调试降级）
├── main.py               # FastAPI 应用（57 路由 + 成员权限中间件）
└── static/               # 管理界面（六页签）
profiles/<环境id>/        # 各环境独立的用户数据目录
data/                     # 数据库 / 密钥 / 设置 / 任务截图
scripts/                  # 全部测试脚本
```

## 指纹固定机制（本项目的关键设计）

Camoufox 默认**每次启动**随机生成指纹与噪声种子（面向一次性爬虫场景）。而指纹浏览器要求
「同一环境 = 同一台稳定设备」，因此本工作台在创建环境时一次性生成并落库：

- 指纹主体（BrowserForge 合成 或 真实设备预设；UA 版本对齐到已安装内核；
  预设的泛化 WebGL "Mozilla" 自动替换为该 OS 的真实 GPU 参数，否则启动失败）
- Canvas / Audio / 字体间距噪声种子（camoufox 的 `set_into` 只在键缺失时生效，用户预置值优先）
- 字体子集与语音列表（默认每次启动随机，会导致字体度量与 Canvas 跨重启漂移）

已用 `scripts/restart_stability_test.py` 验证：同一环境多次启动，UA/平台/屏幕/字体度量/
Canvas 哈希完全一致。

## 多实例与同步部署

```bash
# 本机作为同步服务器节点
.venv\Scripts\python run.py --port 18080 --sync-server

# 另一台机器（或本机另一目录）作为工作节点
.venv\Scripts\python run.py --port 18090 --home D:\fp-node2
# 节点设置中填入服务器地址与令牌后即可 push/pull
```

## 测试

```bash
.venv\Scripts\python scripts/smoke_test.py              # Phase 1 全链路
.venv\Scripts\python scripts\restart_stability_test.py  # 跨重启指纹稳定性
.venv\Scripts\python scripts\phase2_test.py             # Phase 2 全量（批量/导出导入/任务/加密/认证/备份/审计）
.venv\Scripts\python scripts\phase3_test.py             # Phase 3 全量（矩阵风控/使用统计/成员权限/Webhook/调度/双实例同步）
.venv\Scripts\python scripts\phase4_test.py             # Phase 4 回归（转发器/调度时区/MCP协议/自动同步/内核健康）
.venv\Scripts\python scripts\riskctrl_test.py           # 风控优化（预设/就绪度体检/人机化节奏/预热模板）
.venv\Scripts\python scripts\playwright_bridge_test.py  # Playwright 对接（双内核真实连接+指纹一致性）
.venv\Scripts\python scripts\fpchromium_verify.py       # fp-chromium 内核验证
.venv\Scripts\python scripts\mcp_protocol_test.py        # MCP 协议独立测试（11 项检查）
```

## 风控环境优化（Cloudflare / Google 人机验证 / 极验 / 网易易盾）

产品定位是**环境质量**：让风控系统信任环境中的真人操作（少弹验证、不误伤），
不提供也不应使用验证码自动破解。三层能力：

### 1. 风控预设（环境创建时选择）

| 预设 | 自动生效参数 | 适用 |
|---|---|---|
| `standard` | 默认参数 | 大多数站点 |
| `cloudflare` | 禁用 COOP（Turnstile 勾选框可交互）+ 类人鼠标轨迹增强（上限 2s） | Cloudflare 防护站点 |
| `china` | locale=zh-CN + 时区=Asia/Shanghai + geoip 对齐 | 极验 / 网易易盾等国内风控 |

### 2. 环境就绪度体检（环境列表 →「体检」按钮）

在真实浏览器内实测 8 项一致性并给出整改建议（每项标注关联的风控体系）：
网络出口可达性、时区 vs IP 属地、语言 vs IP 国家、**WebRTC 公网 IP 泄露**、
navigator.webdriver 自动化标记、Canvas 读取稳定性、WebGL 渲染器合理性、字体渲染。
输出 0~100 评分与结论（就绪/建议优化/存在穿帮风险）。

### 3. 行为质量

- **人机化节奏**（RPA 运行时可勾选）：步骤间随机停顿 0.4~1.8s、逐字符随机间隔输入
- **环境预热模板**（RPA 任务页一键创建）：访问中性高流量站点积累浏览历史与信誉
- camoufox 内核自带类人鼠标轨迹（贝塞尔曲线移动）

### 各风控体系要点（经验总结）

- **Cloudflare**：真浏览器内核天然通过 TLS/JA3 与 HTTP/2 指纹；重点在
  ①住宅代理（机房 IP 几乎必被挑战）②有头模式优于无头 ③Turnstile 勾选需 `cloudflare` 预设的 disable_coop
  ④时区/语言与 IP 属地一致（体检确认）
- **Google reCAPTCHA**：重历史与信誉——同环境长期使用（本产品 Cookie/指纹跨重启稳定）、
  避免频繁清空数据、登录态一致、新环境先用预热模板养几天再干正事
- **极验 / 网易易盾**：设备指纹连续性 + IP 质量为主——国内站点配国内住宅 IP + `china` 预设、
  Canvas/字体/Audio 指纹稳定（本产品按环境固定种子）、行为侧开启人机化节奏

### Turnstile「Can't verify the user is human」排查（如 Cursor 登录）

实测结论（`scripts/turnstile_diagnose.py` + 就绪度体检）：

1. **语言与 IP 属地错配是最常见元凶**——camoufox 默认 locale 是 en-US，
   若出口 IP 在国内，Turnstile 直接低分拒绝。**解法：国内直连用 `china` 预设
   （zh-CN + Asia/Shanghai）；海外代理用 `cloudflare` 预设并开 geoip 自动对齐**
2. 广告拦截扩展（uBlock）可能阻断验证域——`cloudflare` 预设已默认排除 uBlock，
   也可在启动选项手动勾选「禁用广告拦截」
3. **必须有头模式**（无头特征是重扣分项）；IP 质量差（机房/滥用段）时换住宅代理
4. camoufox 仍被拒时可换 **fp-chromium 内核**（继承系统语言、作者实测通过 Turnstile）
5. 环境列表「体检」按钮：locale_match 与 turnstile_reachable 两项可直接定位此类问题

## 已知限制

- Camoufox 走 Playwright/Juggler 协议无 CDP 端口；需要 Selenium/Puppeteer 直连时选
  `fp-chromium` 内核（返回 `ws_endpoint`）——需自行下载安装 fingerprint-chromium
- RPA 任务与页面控制仅支持 camoufox 内核（fp-chromium 走 CDP 直连自动化）
- Cookie 等数据目录不参与同步（体积与安全考量）
- 首次启动环境约 30~60 秒（字体库与指纹初始化）；后续启动明显加快
- 默认端口 18080（50325 在部分 Windows 上被 Hyper-V 保留）

## 合规提示

本项目仅用于合法用途：自有账号矩阵运营、广告验证、数据采集（遵守目标网站条款与当地法规）、
自动化测试。禁止用于欺诈、规避司法封禁等违法行为。

