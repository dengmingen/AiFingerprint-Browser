"""API 数据模型（Pydantic v2）。"""
from typing import Literal, Optional
from pydantic import BaseModel, Field

ProxyScheme = Literal["http", "https", "socks4", "socks5"]
TargetOS = Literal["windows", "macos", "linux"]
KernelName = Literal["camoufox", "chromium", "fp-chromium"]
# 指纹模式：generate=BrowserForge 合成；preset=真实设备预设库
FingerprintMode = Literal["generate", "preset"]
# RPA 步骤动作
TaskAction = Literal[
    "navigate", "click", "type", "press", "wait", "wait_for",
    "scroll", "screenshot", "extract", "evaluate",
    "hover", "select", "upload", "download",
    "tab_open", "tab_switch", "tab_close",
    "set_var", "label", "goto", "if",
]


class ProxyConfig(BaseModel):
    scheme: ProxyScheme = "http"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=128)
    password: Optional[str] = Field(default=None, max_length=128)

    def to_playwright(self) -> dict:
        d = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        return d

    def to_url(self) -> str:
        if self.username and self.password:
            return f"{self.scheme}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"


# 风控预设：standard=通用；cloudflare=CF/Turnstile 强化；china=极验/易盾等国内风控
RiskPreset = Literal["standard", "cloudflare", "china"]


class LaunchConfig(BaseModel):
    headless: bool = False
    # 启动时根据代理出口 IP 自动匹配时区/经纬度/语言（Camoufox 内核）
    geoip: bool = True
    # 类人鼠标轨迹（Camoufox 内核）
    humanize: bool = True
    # 鼠标轨迹最大时长（秒）；风控预设 cloudflare 会自动调高
    humanize_max: Optional[float] = Field(default=None, ge=0.5, le=5.0)
    block_webrtc: bool = False
    locale: Optional[str] = Field(default=None, max_length=35)
    # 显式时区（如 Asia/Shanghai）；china 预设默认填充；留空由 geoip/系统决定
    timezone: Optional[str] = Field(default=None, max_length=64)
    # 禁用 Cross-Origin-Opener-Policy：Cloudflare Turnstile 等跨域 iframe 内的
    # 人机验证勾选框需要此选项才能正常交互（camoufox 内核）
    disable_coop: bool = False
    # 禁用广告拦截扩展（uBlock）：它会阻断 Cloudflare 验证域，
    # 导致 Turnstile 报 "Can't verify the user is human"；cloudflare 预设默认禁用
    disable_adblock: bool = False
    # fingerprint-chromium 148+：伪装 OS 具体版本（如 15.0.0）；
    # 留空 = 按指纹种子自动选择与 target_os 匹配的稳定版本
    fp_platform_version: Optional[str] = Field(default=None, max_length=32)
    # fingerprint-chromium 148+：按需关闭内核单项伪装，逗号分隔
    # （可选值：font,audio,canvas,clientrects,gpu）——调试对拍用，生产保持默认全开
    fp_disable_spoofing: Optional[str] = Field(default=None, max_length=64,
                                               pattern=r"^[a-z,]+$")
    # 风控环境预设：自动组合上述参数
    preset: RiskPreset = "standard"
    start_url: str = "about:blank"


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    group_name: str = Field(default="默认分组", max_length=64)
    notes: str = Field(default="", max_length=2000)
    kernel: KernelName = "camoufox"
    target_os: TargetOS = "windows"
    fingerprint_mode: FingerprintMode = "generate"
    proxy: Optional[ProxyConfig] = None
    launch: LaunchConfig = Field(default_factory=LaunchConfig)


class ProfileBatchCreate(BaseModel):
    """按模板批量创建环境：生成 count 个环境，名称自动加序号。"""
    count: int = Field(ge=1, le=200)
    template: ProfileCreate
    # 代理池：依次分配给各环境（循环使用；不足时最后的环境用池尾代理）
    proxy_pool: Optional[list[ProxyConfig]] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    group_name: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    kernel: Optional[KernelName] = None
    target_os: Optional[TargetOS] = None
    fingerprint_mode: Optional[FingerprintMode] = None
    proxy: Optional[ProxyConfig] = None
    launch: Optional[LaunchConfig] = None
    # 指纹重新生成开关：更新时传 regen_fingerprint=true 会为该环境换一套新指纹
    regen_fingerprint: bool = False
    # 清除代理绑定（proxy 传 null 无法与"不修改"区分，故用显式标志）
    clear_proxy: bool = False


class IdsRequest(BaseModel):
    profile_ids: list[str] = Field(min_length=1, max_length=200)


class BatchStartRequest(BaseModel):
    profile_ids: list[str] = Field(min_length=1, max_length=50)
    headless: Optional[bool] = None
    start_url: Optional[str] = None


class ProfileExportData(BaseModel):
    """环境导出/导入的可移植格式（代理密码明文包含在内，注意文件保密）。"""
    format_version: int = 1
    profile: dict  # 除 id/时间戳外的完整环境配置（含指纹）
    data_archive: Optional[str] = None  # 可选：用户数据目录 zip 的 base64


class TaskStep(BaseModel):
    action: TaskAction
    url: Optional[str] = None
    selector: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    ms: Optional[int] = None
    timeout: Optional[int] = Field(default=None, ge=1000, le=300000)
    amount: Optional[int] = None
    attr: Optional[str] = None
    expression: Optional[str] = None
    full_page: Optional[bool] = None
    name: Optional[str] = None
    press_enter: Optional[bool] = None
    # ---- 扩展字段（Phase 4）----
    var: Optional[str] = Field(default=None, max_length=64,
                               description="extract/evaluate 结果存入变量，供 {{var}} 引用")
    retry: Optional[int] = Field(default=None, ge=0, le=5,
                                 description="步骤失败重试次数")
    on_error: Optional[str] = Field(default=None, max_length=72,
                                    pattern=r"^(abort|continue|goto:[\w-]+)$",
                                    description="失败处置：abort 终止 / continue 继续 / goto:标签")
    frame: Optional[str] = Field(default=None, max_length=500,
                                 description="iframe 的 CSS 选择器（步骤在该 iframe 内执行）")
    value: Optional[str] = Field(default=None, max_length=2000,
                                 description="select 选项 / set_var 值 / tab_switch 序号或标题")
    path: Optional[str] = Field(default=None, max_length=1000,
                                description="upload 本地文件路径")
    label: Optional[str] = Field(default=None, max_length=64,
                                 description="label 定义标签名 / goto 跳转目标")
    op: Optional[Literal["equals", "contains", "exists"]] = Field(
        default=None, description="if 条件运算")
    then_goto: Optional[str] = Field(default=None, max_length=64)
    else_goto: Optional[str] = Field(default=None, max_length=64)


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    notes: str = Field(default="", max_length=2000)
    steps: list[TaskStep] = Field(min_length=1, max_length=100)
    webhook_url: Optional[str] = Field(default=None, max_length=500)


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    steps: Optional[list[TaskStep]] = Field(default=None, min_length=1, max_length=100)
    webhook_url: Optional[str] = Field(default=None, max_length=500)


class TaskRunRequest(BaseModel):
    profile_ids: list[str] = Field(min_length=1, max_length=50)
    headless: bool = True
    auto_close: bool = True
    # 人机化节奏：步骤间随机延迟、逐字符输入（降低行为特征检测）
    humanize: bool = False


class SettingsUpdate(BaseModel):
    api_key_enabled: Optional[bool] = None
    regenerate_key: bool = False
    sync_server_enabled: Optional[bool] = None
    sync_remote_url: Optional[str] = Field(default=None, max_length=300)
    sync_remote_token: Optional[str] = Field(default=None, max_length=128)
    regenerate_sync_token: bool = False


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    role: Literal["admin", "operator"] = "operator"


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    task_id: str
    kind: Literal["daily", "interval"]
    daily_time: Optional[str] = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=100000)
    profile_ids: list[str] = Field(min_length=1, max_length=50)
    headless: bool = True
    auto_close: bool = True
    # daily_time 的解释时区（IANA 名，如 Asia/Shanghai；缺省=系统本地时区，"UTC"=UTC）
    timezone: Optional[str] = Field(default=None, max_length=64)
    # 生效日：0=周一 … 6=周日；缺省/空=每天
    weekdays: Optional[list[int]] = Field(default=None, max_length=7)


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    kind: Optional[Literal["daily", "interval"]] = None
    daily_time: Optional[str] = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=100000)
    profile_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=50)
    headless: Optional[bool] = None
    auto_close: Optional[bool] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    weekdays: Optional[list[int]] = None


class BrowserStartRequest(BaseModel):
    profile_id: str
    # 临时覆盖项（不写回数据库）
    headless: Optional[bool] = None
    start_url: Optional[str] = None


class PlaywrightServerRequest(BaseModel):
    """为 camoufox 环境启动 Playwright Server（外部 Playwright 直连）。"""
    port: Optional[int] = Field(default=None, ge=1024, le=65535)


class ProxyTestRequest(BaseModel):
    # proxy 为 null 时测试直连（用于对照真实出口 IP）
    proxy: Optional[ProxyConfig] = None
    timeout: float = Field(default=15, ge=2, le=60)


class NavigateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ScreenshotRequest(BaseModel):
    full_page: bool = False


class EvaluateRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=10000)


# 页面交互请求（所有内核通用）
class ClickRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    timeout: int = Field(default=30000, ge=1000, le=120000)


class TypeRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    text: str = Field(default="", max_length=5000)
    press_enter: bool = False
    timeout: int = Field(default=30000, ge=1000, le=120000)


class PressRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)


class WaitRequest(BaseModel):
    ms: int = Field(default=1000, ge=100, le=300000)


class WaitForRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    timeout: int = Field(default=30000, ge=1000, le=120000)


class ScrollRequest(BaseModel):
    amount: int = Field(default=500)


class HoverRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    timeout: int = Field(default=30000, ge=1000, le=120000)


class SelectRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    value: str = Field(min_length=1, max_length=500)
    timeout: int = Field(default=30000, ge=1000, le=120000)


class ExtractRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    attr: Optional[str] = Field(default=None, max_length=128)


class GetHtmlRequest(BaseModel):
    max_length: int = Field(default=50000, ge=1000, le=500000)
