"""全局配置：路径、端口、常量。

支持环境变量 FPWB_HOME（或 run.py --home）重定位数据目录，
用于多实例部署（如同步服务器/测试实例）。
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

FPWB_HOME = Path(os.environ.get("FPWB_HOME") or BASE_DIR)
DATA_DIR = FPWB_HOME / "data"
PROFILE_ROOT = FPWB_HOME / "profiles"
DB_PATH = DATA_DIR / "fpworkbench.db"

APP_NAME = "指纹浏览器工作台"
API_VERSION = "v1"
VERSION = "0.4.0"
PHASE = 4

DEFAULT_HOST = "127.0.0.1"
# 仿 AdsPower Local API 风格；默认端口 50325 在部分 Windows 上被
# Hyper-V 端口保留范围占用（winerror 10013），故默认使用 18080
DEFAULT_PORT = 18080


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)


# 内置自检链接（管理界面一键打开）
DETECT_LINKS = {
    "CreepJS": "https://abrahamjuliot.github.io/creepjs/",
    "BrowserLeaks": "https://browserleaks.com/javascript",
    "WebRTC 检测": "https://browserleaks.com/webrtc",
    "Pixelscan": "https://pixelscan.net/",
    "IPHey": "https://iphey.com/",
    "AmIUnique": "https://amiunique.org/fp",
    "JA3/TLS 检测": "https://scrapfly.io/web-scraping-tools/ja3-fingerprint",
}

# 成员角色
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
