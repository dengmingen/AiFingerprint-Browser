"""环境就绪度检测：在真实浏览器内实测各风控体系关注的环境一致性维度。

设计思路：风控系统（Cloudflare / Google reCAPTCHA / 极验 / 网易易盾）判定"可疑环境"
的核心信号不是单个指纹值，而是**矛盾**——IP 属地与时区/语言不符、WebRTC 暴露真实 IP、
UA 声称 A 设备但 Canvas/GPU 是 B、自动化标记残留等。本引擎启动（或复用）环境浏览器，
在页面内实地采样并逐项判定，输出可直接执行的整改建议。

检测项（均为运行时实测，非静态检查）:
  ip_reachable   代理/网络可达性（浏览器网络栈内访问 ip-api）
  tz_match       浏览器时区 vs 出口 IP 属地时区
  locale_match   浏览器语言 vs 出口 IP 国家（CN→zh、欧美→en）
  webrtc_leak    WebRTC 候选公网 IP 是否与出口 IP 一致（或已禁用）
  webdriver      navigator.webdriver 自动化标记
  canvas_stable  同一会话内 Canvas 指纹读取稳定性
  webgl_sane     WebGL 渲染器字符串合理（非泛化/软件渲染）
  fonts_render   字体渲染度量有效
  audio_stable   AudioContext 指纹读取稳定性
  ua_chips       Client Hints（userAgentData）平台与目标 OS 一致（Chromium 系）
  screen_sane    屏幕尺寸/色深/像素比合理
  media_devices  媒体设备枚举非空（0 设备是虚拟机特征）
  webgpu         WebGPU 适配器可用性（新兴指纹信号）
  adblock_active 广告拦截探测（扩展可被站点探测且阻断验证域）
  plugins_sane   navigator.plugins 数量合理（真 Chrome 内置 5 个 PDF 插件）
  hw_sane        deviceMemory/hardwareConcurrency 数值合理（Chromium 系）
  turnstile_reachable Cloudflare 验证域可达
"""
import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx
import websockets

from .launcher import LaunchManager

log = logging.getLogger(__name__)


class _CamoufoxPage:
    """camoufox 内核：playwright 上下文里开新页做检查（不打扰用户页面）。"""

    def __init__(self, context):
        self.context = context
        self.page = None

    async def open(self) -> None:
        self.page = await self.context.new_page()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    async def evaluate(self, js: str) -> Any:
        return await self.page.evaluate(js)

    async def close(self) -> None:
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass


class _CdpPage:
    """chromium 系内核（fp-chromium / chromium）：新建 CDP 目标页做检查。"""

    def __init__(self, port: int):
        self.port = port
        self.ws = None
        self.target_id = None
        self._id = 0

    async def open(self) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.put(f"http://127.0.0.1:{self.port}/json/new?about:blank", timeout=10)
            info = r.json()
        self.target_id = info["id"]
        self.ws = await websockets.connect(info["webSocketDebuggerUrl"], max_size=8 << 20)

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if resp.get("id") == self._id:
                return resp

    async def goto(self, url: str) -> None:
        await self._send("Page.navigate", {"url": url})
        for _ in range(30):  # 等待加载完成
            await asyncio.sleep(0.5)
            r = await self._send("Runtime.evaluate", {"expression": "document.readyState"})
            if r.get("result", {}).get("result", {}).get("value") in ("complete", "interactive"):
                return

    async def evaluate(self, js: str) -> Any:
        r = await self._send("Runtime.evaluate",
                             {"expression": js, "awaitPromise": True, "returnByValue": True})
        if r.get("result", {}).get("exceptionDetails"):
            raise RuntimeError("页面脚本执行失败")
        return r.get("result", {}).get("result", {}).get("value")

    async def close(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.target_id:
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(f"http://127.0.0.1:{self.port}/json/close/{self.target_id}",
                                timeout=5)
            except Exception:
                pass

# 出口 IP 探测：多端点回退（http 优先，https 备用；页面在 https 下时混合内容需走 https 端点）
_IP_PROVIDERS = [
    ("http://ip-api.com/json/?fields=status,message,query,country,countryCode,city,timezone,isp",
     lambda d: {"query": d.get("query"), "country": d.get("country"), "countryCode": d.get("countryCode"),
                "city": d.get("city"), "timezone": d.get("timezone"), "isp": d.get("isp")}
     if d.get("status") == "success" else None),
    ("https://ipwho.is/?fields=ip,success,country,country_code,city,timezone,connection",
     lambda d: {"query": d.get("ip"), "country": d.get("country"), "countryCode": d.get("country_code"),
                "city": d.get("city"), "timezone": d.get("timezone"),
                "isp": (d.get("connection") or {}).get("isp")}
     if d.get("success") else None),
    ("https://ipinfo.io/json",
     lambda d: {"query": d.get("ip"), "country": d.get("country"), "countryCode": d.get("country"),
                "city": d.get("city"), "timezone": d.get("timezone"), "isp": d.get("org")}
     if d.get("ip") else None),
]

# 各检测项与风控体系的关联（用于报告展示）
VENDOR_MAP = {
    "ip_reachable": ["cloudflare", "google", "geetest", "yidun"],
    "tz_match": ["cloudflare", "google", "geetest", "yidun"],
    "locale_match": ["cloudflare", "geetest", "yidun"],
    "webrtc_leak": ["cloudflare", "google", "geetest", "yidun"],
    "webdriver": ["cloudflare", "google", "geetest", "yidun"],
    "canvas_stable": ["google", "geetest", "yidun"],
    "webgl_sane": ["cloudflare", "google", "geetest", "yidun"],
    "fonts_render": ["geetest", "yidun", "google"],
    "audio_stable": ["google", "geetest", "yidun"],
    "ua_chips": ["cloudflare", "google"],
    "screen_sane": ["cloudflare", "google", "geetest", "yidun"],
    "media_devices": ["google", "yidun"],
    "webgpu": ["cloudflare", "google"],
    "adblock_active": ["cloudflare"],
    "plugins_sane": ["cloudflare", "google"],
    "hw_sane": ["google", "yidun"],
    "turnstile_reachable": ["cloudflare"],
}

_EXPECT_LANG_PREFIX = {"CN": "zh", "TW": "zh", "HK": "zh", "US": "en", "GB": "en",
                       "JP": "ja", "KR": "ko", "DE": "de", "FR": "fr", "RU": "ru"}

_CHECK_JS = """(async () => {
  const out = {};
  out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  out.lang = navigator.language || '';
  out.langs = (navigator.languages || []).join(',');
  out.webdriver = navigator.webdriver;
  out.plugins = (navigator.plugins || []).length;
  out.ua = navigator.userAgent || '';
  // Client Hints（Chromium 独有；Firefox 为 undefined 属正常）
  out.chPlatform = navigator.userAgentData ? navigator.userAgentData.platform : null;
  // deviceMemory / 硬件并发（Chromium 独有 deviceMemory）
  out.deviceMemory = navigator.deviceMemory || null;
  out.hw = navigator.hardwareConcurrency || null;
  // 屏幕与像素比
  out.screenWH = screen.width + 'x' + screen.height;
  out.colorDepth = screen.colorDepth;
  out.dpr = window.devicePixelRatio;
  // 媒体设备枚举（无授权时标签为空但数量可见；0 设备是虚拟机特征）
  try { out.mediaDevices = (await navigator.mediaDevices.enumerateDevices()).length; }
  catch (e) { out.mediaDevices = -1; }
  // 电池 API（Chromium 独有）
  out.hasBattery = typeof navigator.getBattery === 'function';
  // Canvas：同一会话两次读取
  const cv = document.createElement('canvas'); cv.width = 240; cv.height = 40;
  const x = cv.getContext('2d');
  x.textBaseline = 'top'; x.font = "14px 'Arial'";
  x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
  x.fillStyle = '#069'; x.fillText('readiness-check', 2, 15);
  const read = () => {
    const d = cv.toDataURL(); let h = 0;
    for (const ch of d) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return d.length + ':' + h;
  };
  out.canvas1 = read(); out.canvas2 = read();
  // Audio 指纹：离线渲染压缩器输出（两次，验稳定性）
  try {
    const render = async () => {
      const ac = new OfflineAudioContext(1, 44100, 44100);
      const osc = ac.createOscillator(); osc.type = 'triangle'; osc.frequency.value = 10000;
      const comp = ac.createDynamicsCompressor();
      osc.connect(comp); comp.connect(ac.destination); osc.start(0);
      const buf = await ac.startRendering();
      let s = 0;
      for (let i = 4500; i < 5000; i++) s += Math.abs(buf.getChannelData(0)[i]);
      return s;
    };
    out.audio1 = await render(); out.audio2 = await render();
  } catch (e) { out.audio1 = out.audio2 = null; }
  // WebGL 渲染器
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    out.webglRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '';
  } catch (e) { out.webglRenderer = ''; }
  // WebGPU 适配器（3 秒超时，防挂起）
  out.webgpu = await Promise.race([
    (async () => {
      if (!navigator.gpu) return 'unsupported';
      try {
        const a = await navigator.gpu.requestAdapter();
        if (!a) return 'no-adapter';
        return 'ok' + (a.info && a.info.architecture ? ':' + a.info.architecture : '');
      } catch (e) { return 'error'; }
    })(),
    new Promise((r) => setTimeout(() => r('timeout'), 3000)),
  ]);
  // 字体渲染
  const s = document.createElement('span');
  s.style.cssText = 'position:absolute;left:-9999px;font-size:72px;white-space:nowrap';
  s.textContent = 'mmmmmmmmmmlli';
  document.body.appendChild(s);
  s.style.fontFamily = 'Arial';
  out.fontWidth = s.offsetWidth;
  s.remove();
  // WebRTC：收集 ICE 候选（5 秒超时）
  out.srflx = await new Promise((resolve) => {
    let done = false; const ips = [];
    try {
      const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
      pc.createDataChannel('c');
      pc.onicecandidate = (e) => {
        if (!e.candidate) { if (!done) { done = true; resolve(ips); } return; }
        const m = e.candidate.candidate.match(/candidate:\\S+ \\d+ \\S+ \\d+ (\\S+)/);
        const ip = m && m[1];
        if (ip && /^[0-9a-fA-F.:]+$/.test(ip)) {
          // 仅记录公网地址（不含 192.168./10./172.16-31. 与本地 v6）
          const parts = ip.split('.');
          const isPrivate = parts.length === 4 &&
            (parts[0] === '10' || parts[0] === '192' && parts[1] === '168' ||
             parts[0] === '172' && +parts[1] >= 16 && +parts[1] <= 31 ||
             parts[0] === '127' || parts[0] === '169' && parts[1] === '254');
          const isLinkLocalV6 = ip.startsWith('fe80') || ip.startsWith('::1');
          if (!isPrivate && !isLinkLocalV6) ips.push(ip);
        }
      };
      pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => resolve([]));
      setTimeout(() => { if (!done) { done = true; resolve(ips); pc.close(); } }, 5000);
    } catch (e) { resolve([]); }
  });
  // 广告拦截探测：广告域请求被拦截 → 存在广告拦截扩展（可被站点探测）
  out.adblock = await fetch('https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js',
    { mode: 'no-cors' }).then(() => 'no').catch(() => 'yes');
  // Cloudflare Turnstile 验证域可达性（no-cors：网络层被阻断才 reject）
  out.turnstile = await fetch('https://challenges.cloudflare.com/turnstile/v0/api.js',
    { mode: 'no-cors' }).then(() => 'ok').catch(() => 'blocked');
  return out;
})()"""


def _normalize_tz(tz: Any) -> str:
    """ipwho.is 等端点的 timezone 字段是 {'id': 'Asia/Shanghai', ...} 对象，统一取字符串。"""
    if isinstance(tz, dict):
        return str(tz.get("id") or tz.get("name") or "")
    return str(tz or "")


def _result(check_id: str, status: str, detail: str, advice: str = "") -> dict:
    return {
        "id": check_id, "status": status, "detail": detail, "advice": advice,
        "vendors": VENDOR_MAP.get(check_id, []),
    }


async def run_readiness(manager: LaunchManager, profile: dict) -> dict:
    """执行就绪度检测（约 10~40 秒）。返回报告 dict。"""
    started_by_us = False
    inst = manager.get_instance(profile["id"])
    if inst is None:
        await manager.start(profile, headless=True)
        started_by_us = True
        inst = manager.get_instance(profile["id"])
    if inst is None:
        raise RuntimeError("浏览器实例不可用（启动失败）")

    # 按内核选择检查通道：camoufox 用 playwright 上下文；chromium 系用 CDP 新目标页
    if profile["kernel"] == "camoufox":
        if inst.context is None:
            raise RuntimeError("camoufox 实例缺少页面上下文")
        page = _CamoufoxPage(inst.context)
    else:
        port = inst.info.get("debug_port")
        if not port:
            raise RuntimeError("chromium 系实例缺少 CDP 调试端口")
        page = _CdpPage(port)

    checks: list[dict] = []
    try:
        await page.open()

        # ---- 网络路径与出口 IP（浏览器网络栈内实测，天然走代理；多端点回退）
        ipinfo: dict = {}
        ip_errors: list[str] = []
        for url, extract in _IP_PROVIDERS:
            try:
                await page.goto(url)
                raw = json.loads(await page.evaluate("document.body.innerText"))
                info = extract(raw)
                if info and info.get("query"):
                    ipinfo = info
                    break
                ip_errors.append(f"{url.split('/')[2]}: 数据无效")
            except Exception as e:
                ip_errors.append(f"{url.split('/')[2]}: {str(e)[:40]}")
        if not ipinfo:
            checks.append(_result("ip_reachable", "fail",
                                  f"出口探测失败（{'; '.join(ip_errors[:2])}）",
                                  "若浏览器可正常上网，可能是个别探测端点不可用；"
                                  "仍建议确认代理连通性或更换住宅 IP"))
            return _report(profile, checks)
        exit_ip = ipinfo.get("query", "")
        checks.append(_result("ip_reachable", "pass",
                              f"出口 {exit_ip}（{ipinfo.get('country')} {ipinfo.get('city') or ''}，ISP: {str(ipinfo.get('isp') or '')[:30]}）"))

        # ---- 页面内全量采样
        data = await asyncio.wait_for(page.evaluate(_CHECK_JS), timeout=45)

        # 时区一致性
        ip_tz = _normalize_tz(ipinfo.get("timezone"))
        if data["tz"] and ip_tz and data["tz"] != ip_tz:
            checks.append(_result("tz_match", "fail",
                                  f"浏览器时区 {data['tz']} ≠ IP 属地时区 {ip_tz}",
                                  "时区矛盾是最常见的环境穿帮：开启 geoip 或设置显式时区（国内预设=Asia/Shanghai）"))
        else:
            checks.append(_result("tz_match", "pass", f"时区一致（{data['tz']}）"))

        # 语言一致性
        cc = ipinfo.get("countryCode", "")
        expected = _EXPECT_LANG_PREFIX.get(cc)
        lang_prefix = (data["lang"] or "").split("-")[0].lower()
        if expected and lang_prefix and lang_prefix != expected:
            checks.append(_result("locale_match", "warn",
                                  f"IP 属地 {cc} 期望 {expected}-*，浏览器语言 {data['lang']}",
                                  "语言与属地不符会显著提高人机验证触发率；在启动选项设置 locale（国内预设=zh-CN）"))
        else:
            checks.append(_result("locale_match", "pass",
                                  f"语言 {data['lang'] or '(空)'} 与属地 {cc} 相符"))

        # WebRTC 泄露
        srflx = data.get("srflx") or []
        exit_ip = ipinfo.get("query", "")
        leaked = [ip for ip in srflx if ip != exit_ip]
        if leaked:
            checks.append(_result("webrtc_leak", "fail",
                                  f"WebRTC 暴露非出口公网 IP: {', '.join(leaked[:3])}",
                                  "真实 IP 泄露！开启 geoip（WebRTC IP 伪造）或勾选禁用 WebRTC"))
        elif srflx:
            checks.append(_result("webrtc_leak", "pass", f"WebRTC 公网 IP 与出口一致（{srflx[0]}）"))
        else:
            checks.append(_result("webrtc_leak", "pass", "未产生公网候选（已禁用/已防护）"))

        # webdriver
        if data["webdriver"] in (False, None, 0):
            checks.append(_result("webdriver", "pass", "navigator.webdriver 无自动化标记"))
        else:
            checks.append(_result("webdriver", "fail", "navigator.webdriver=true",
                                  "自动化标记未清除；检查内核选择（调试内核无伪装）"))

        # Canvas 稳定性
        if data["canvas1"] == data["canvas2"]:
            checks.append(_result("canvas_stable", "pass", "同会话 Canvas 读取稳定"))
        else:
            checks.append(_result("canvas_stable", "fail", "同会话两次读取不一致（噪声随机化）",
                                  "Canvas 噪声必须固定（本产品已按环境固定种子，请勿手动改动指纹数据）"))

        # WebGL 合理性
        renderer = data.get("webglRenderer") or ""
        if not renderer:
            checks.append(_result("webgl_sane", "warn", "WebGL 渲染器为空", "部分站点视无 WebGL 为可疑"))
        elif any(k in renderer for k in ("SwiftShader", "llvmpipe", "Software", "Basic Render")):
            checks.append(_result("webgl_sane", "warn", f"软件渲染: {renderer[:50]}",
                                  "无 GPU/软件渲染是虚拟机信号；有头模式+实体机可缓解"))
        elif renderer.strip() == "Mozilla":
            checks.append(_result("webgl_sane", "warn", "泛化渲染器 Mozilla", "建议重生成指纹（真实 GPU 参数）"))
        else:
            checks.append(_result("webgl_sane", "pass", renderer[:60]))

        # 字体渲染
        if int(data.get("fontWidth") or 0) > 0:
            checks.append(_result("fonts_render", "pass", f"字体度量正常（{data['fontWidth']}px）"))
        else:
            checks.append(_result("fonts_render", "fail", "字体度量无效", "字体子集异常，重生成指纹"))

        # ---- 引擎类型（Chromium 独有信号的"缺失"是否正常以此为界）
        ua = data.get("ua") or ""
        is_chromium = "Firefox" not in ua

        # Audio 稳定性
        if data.get("audio1") is None:
            checks.append(_result("audio_stable", "warn",
                                  "AudioContext 指纹不可读",
                                  "音频指纹缺失在部分风控中视为可疑；检查内核版本"))
        elif data["audio1"] == data["audio2"]:
            checks.append(_result("audio_stable", "pass", "同会话 Audio 指纹稳定"))
        else:
            checks.append(_result("audio_stable", "fail", "两次 Audio 指纹不一致（噪声未固定）",
                                  "Audio 噪声必须固定（本产品按环境固定种子，请勿手改指纹数据）"))

        # Client Hints（仅 Chromium 系需要检查；Firefox 无此 API 属正常）
        if is_chromium:
            expect_ch = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
                profile.get("target_os"), "")
            got = data.get("chPlatform")
            if got and expect_ch and got != expect_ch:
                checks.append(_result("ua_chips", "fail",
                                      f"Client Hints 平台 {got} ≠ 目标 OS {expect_ch}",
                                      "userAgentData 与伪装 OS 矛盾（穿帮级）；"
                                      "fp-chromium 内核自带一致性，出现此问题请换内核或重生成指纹"))
            elif got:
                checks.append(_result("ua_chips", "pass", f"Client Hints 平台一致（{got}）"))
            else:
                checks.append(_result("ua_chips", "warn",
                                      "userAgentData 缺失（Chromium 应有）",
                                      "真 Chrome 均带 Client Hints；缺失是自动化/精简版信号"))
        else:
            checks.append(_result("ua_chips", "pass", "Firefox 内核无 Client Hints（正常）"))

        # 屏幕合理性
        try:
            w, h = (int(v) for v in str(data.get("screenWH", "0x0")).split("x"))
        except Exception:
            w = h = 0
        depth = data.get("colorDepth")
        dpr = data.get("dpr")
        screen_ok = w >= 1024 and h >= 720
        depth_ok = depth in (24, 30)
        dpr_ok = dpr in (1, 1.25, 1.5, 1.75, 2, 2.5, 3)
        if screen_ok and depth_ok and dpr_ok:
            checks.append(_result("screen_sane", "pass",
                                  f"{w}x{h} @{depth}bit DPR={dpr}"))
        else:
            issues = []
            if not screen_ok:
                issues.append(f"尺寸 {w}x{h} 异常")
            if not depth_ok:
                issues.append(f"色深 {depth} 异常（真机常见 24/30）")
            if not dpr_ok:
                issues.append(f"像素比 {dpr} 异常")
            checks.append(_result("screen_sane", "warn", "；".join(issues),
                                  "屏幕参数不合理是虚拟机/自动化特征"))

        # 媒体设备
        md = data.get("mediaDevices")
        if md is None or md == -1:
            checks.append(_result("media_devices", "warn",
                                  "enumerateDevices 不可用",
                                  "真机均有摄像头/麦克风/扬声器条目；0 设备是虚拟机信号"))
        elif md == 0:
            checks.append(_result("media_devices", "warn",
                                  "媒体设备数为 0（虚拟机特征）",
                                  "如非故意禁用，建议在真机运行或接受该项风险"))
        else:
            checks.append(_result("media_devices", "pass", f"枚举到 {md} 个媒体设备"))

        # WebGPU
        wg = data.get("webgpu")
        if str(wg).startswith("ok"):
            checks.append(_result("webgpu", "pass",
                                  f"WebGPU 适配器可用（{str(wg)[2:][:40] or '无架构信息'}）"))
        elif wg in ("unsupported", "no-adapter"):
            checks.append(_result("webgpu", "warn",
                                  f"WebGPU 不可用（{wg}）",
                                  "新一代检测站开始采集 WebGPU；主流真机 Chrome 已支持，"
                                  "不可用会被视为旧设备/虚拟机"))
        else:
            checks.append(_result("webgpu", "warn", f"WebGPU 探测异常（{wg}）"))

        # 广告拦截探测
        if data.get("adblock") == "yes":
            checks.append(_result("adblock_active", "warn",
                                  "检测到广告拦截（广告域请求被拦截）",
                                  "广告拦截扩展可被站点探测并阻断验证域；"
                                  "cloudflare 预设或启动选项勾选「禁用广告拦截」"))
        else:
            checks.append(_result("adblock_active", "pass", "未检测到广告拦截"))

        # 插件数量（真 Chrome 有 5 个内置 PDF 插件；0 是精简/自动化信号）
        plugins = int(data.get("plugins") or 0)
        if is_chromium and plugins == 0:
            checks.append(_result("plugins_sane", "warn",
                                  "navigator.plugins 为空（真 Chrome 应有 5 个 PDF 插件）",
                                  "插件列表为空是 headless/精简版特征"))
        else:
            checks.append(_result("plugins_sane", "pass", f"插件数 {plugins}"))

        # 硬件参数合理性（deviceMemory/Battery 仅 Chromium；Firefox 无属正常）
        hw = data.get("hw")
        mem = data.get("deviceMemory")
        hw_ok = hw is None or 1 <= int(hw) <= 64
        mem_ok = mem is None or (float(mem) >= 0.25 and float(mem) <= 128)
        battery_note = ""
        if is_chromium and not data.get("hasBattery"):
            battery_note = "；Battery API 缺失（真 Chrome 应有）"
        if hw_ok and mem_ok and not battery_note:
            detail = f"核心数 {hw or '未知'}"
            detail += f"，内存 {mem}GB" if mem else ""
            checks.append(_result("hw_sane", "pass", detail))
        else:
            detail = f"硬件参数异常（核心 {hw}，内存 {mem}）{battery_note}"
            checks.append(_result("hw_sane", "warn", detail,
                                  "hardwareConcurrency/deviceMemory/Battery 超出真机常见特征"))

        # Turnstile 验证域可达（uBlock 等广告拦截会阻断它）
        if data.get("turnstile") == "ok":
            checks.append(_result("turnstile_reachable", "pass",
                                  "challenges.cloudflare.com 可达（Turnstile 正常加载）"))
        elif data.get("turnstile") == "blocked":
            checks.append(_result("turnstile_reachable", "fail",
                                  "Cloudflare 验证域被阻断（疑似广告拦截扩展）",
                                  "在启动选项勾选「禁用广告拦截」或使用 cloudflare 预设，"
                                  "否则 Turnstile 会报 Can't verify the user is human"))
        else:
            checks.append(_result("turnstile_reachable", "warn",
                                  f"验证域探测异常: {str(data.get('turnstile'))[:40]}"))

        return _report(profile, checks)
    finally:
        try:
            await page.close()
        except Exception:
            pass
        if started_by_us:
            await manager.stop(profile["id"])


def _report(profile: dict, checks: list[dict]) -> dict:
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    score = max(0, 100 - 25 * len(fails) - 8 * len(warns))
    verdict = "ready" if not fails and not warns else \
              "needs_work" if not fails else "danger"
    return {
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "kernel": profile["kernel"],
        "preset": (profile.get("launch") or {}).get("preset", "standard"),
        "score": score,
        "verdict": verdict,
        "verdict_label": {"ready": "环境就绪", "needs_work": "建议优化", "danger": "存在穿帮风险"}[verdict],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "checks": checks,
    }
