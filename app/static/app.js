/* 指纹浏览器工作台 - 前端逻辑（Phase 2） */
"use strict";

const API = "/api/v1";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let profiles = [];
let tasks = [];
let editingId = null;      // 环境编辑
let editingTaskId = null;  // 任务编辑
let currentRunId = null;   // 运行详情
let currentRunTaskId = null; // 待运行的任务
let logOffset = 0;

/* ------------------------------------------------ 通用 */

function apiKey() { return localStorage.getItem("fpwb_api_key") || ""; }

async function api(method, path, body, retry = true) {
  const headers = { "Content-Type": "application/json" };
  if (apiKey()) headers["X-API-Key"] = apiKey();
  const resp = await fetch(API + path, {
    method, headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败" }));
  if (resp.status === 401 && json.code === 40100 && retry && !path.startsWith("/auth/verify")) {
    const key = prompt("本服务已开启 API Key 认证，请输入密钥：");
    if (key !== null) {
      localStorage.setItem("fpwb_api_key", key.trim());
      return api(method, path, body, false);
    }
  }
  if (json.code !== 0) throw new Error(json.msg || `code=${json.code}`);
  return json.data;
}

let toastTimer = null;
function toast(msg, type = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------ 页签 */

$$("nav.tabs button").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$("nav.tabs button").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "tasks") loadTasks().catch(() => {});
    if (btn.dataset.tab === "schedules") loadSchedules().catch(() => {});
    if (btn.dataset.tab === "matrix") loadMatrix().catch(() => {});
    if (btn.dataset.tab === "logs") { logOffset = 0; loadLogs().catch(() => {}); }
    if (btn.dataset.tab === "settings") loadSettings().catch(() => {});
  }));

/* ------------------------------------------------ 环境管理 */

async function loadStatus() {
  const s = await api("GET", "/status");
  $("#ver").textContent = `v${s.version} · Phase ${s.phase}`;
  $("#kernelBadges").innerHTML = [
    badge("Camoufox", s.kernels.camoufox.available),
    badge("fp-chromium", s.kernels["fp-chromium"].available),
    badge("Chromium", s.kernels.chromium.available),
    s.security.api_key_enabled ? '<span class="badge ok">API Key ✔</span>' : "",
    s.sync.server_enabled ? '<span class="badge ok">同步服务器 ✔</span>' : "",
  ].join("");
  $("#runningCount").textContent = `${profiles.filter((p) => p.running).length} 个环境运行中`;
  $("#aboutBox").innerHTML = `
    <div class="item"><div class="k">版本</div><div class="v">v${esc(s.version)}（Phase ${s.phase}）</div></div>
    <div class="item"><div class="k">Camoufox 内核</div><div class="v">${s.kernels.camoufox.available ? "已安装 ✔" : "未安装"}</div></div>
    <div class="item"><div class="k">fp-chromium 内核</div><div class="v">${s.kernels["fp-chromium"].available ? "已安装 ✔" : "未安装"}</div></div>`;
  const fpErr = s.kernels["fp-chromium"].error;
  $("#kernelGuide").innerHTML = `
    <b>fp-chromium</b>（真实 Chromium 指纹伪装 + CDP 自动化）：
    ${s.kernels["fp-chromium"].available
      ? `已安装：<span class="mono">${esc(s.kernels["fp-chromium"].path)}</span>`
      : `从 <a href="https://github.com/adryfish/fingerprint-chromium/releases" target="_blank" style="color:var(--primary)">GitHub Releases</a> 下载 Windows 版并解压，
         设置环境变量 <code>FPWB_FPCHROMIUM</code> 指向 chrome.exe，或放入项目的 <code>tools/fp-chromium/</code> 目录后重启服务。`}
    <br><b>Camoufox</b> 未安装时运行 <code>python -m camoufox fetch</code>。`;
}

function badge(name, okFlag) {
  return `<span class="badge ${okFlag ? "ok" : "down"}">${name} ${okFlag ? "✔" : "未安装"}</span>`;
}

async function loadProfiles() {
  profiles = await api("GET", "/profiles");
  renderRows();
  $("#runningCount").textContent = `${profiles.filter((p) => p.running).length} 个环境运行中`;
  updateBatchBar();
}

function healthCell(fp) {
  const h = fp?.health || { score: 100, warnings: [] };
  const cls = h.score >= 90 ? "h-good" : h.score >= 70 ? "h-mid" : "h-bad";
  const title = (h.warnings || []).join("\n") || "一致性检查通过";
  return `<span class="health ${cls}" title="${esc(title)}">${h.score}${h.warnings?.length ? " ⚠" : ""}</span>`;
}

function renderRows() {
  const tbody = $("#profileRows");
  if (!profiles.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">还没有环境，点击「新建环境」或「批量新建」开始</td></tr>';
    return;
  }
  tbody.innerHTML = profiles.map((p) => {
    const proxy = p.proxy
      ? `<span class="mono">${esc(p.proxy.scheme)}://${esc(p.proxy.host)}:${p.proxy.port}</span>`
      : '<span class="muted">直连</span>';
    const fs = p.fingerprint_summary || {};
    const ua = fs.user_agent || (fs.kernel_driven ? "Chrome 系指纹 · 由内核种子实时驱动" : "—");
    const uaShort = ua.length > 40 ? ua.slice(0, 40) + "…" : ua;
    const mode = p.fingerprint_summary?.mode || "";
    return `<tr data-id="${p.id}">
      <td><input type="checkbox" class="row-check" data-id="${p.id}"></td>
      <td title="${esc(p.notes)}"><b>${esc(p.name)}</b></td>
      <td>${esc(p.group_name)}</td>
      <td><span class="tag ${p.kernel}">${p.kernel}</span></td>
      <td>${esc(p.target_os)}</td>
      <td>${proxy}</td>
      <td>
        <div class="mono" title="${esc(ua)}">${esc(uaShort)}</div>
        <span class="tag ${p.fingerprint_summary?.mode === "真实预设" ? "preset" : "generate"}" style="margin-top:2px">${esc(mode)}</span>
      </td>
      <td>${healthCell(p.fingerprint_summary)}</td>
      <td><span class="status ${p.running ? "running" : ""}"><span class="dot"></span>${p.running ? "运行中" : "已停止"}</span></td>
      <td>
        <button class="btn small primary" data-act="start" ${p.running ? "disabled" : ""}>启动</button>
        <button class="btn small" data-act="stop" ${p.running ? "" : "disabled"}>停止</button>
        <button class="btn small" data-act="readiness">体检</button>
        <button class="btn small" data-act="edit">编辑</button>
        <button class="btn small" data-act="fp">指纹</button>
        <button class="btn small danger" data-act="del">删除</button>
      </td>
    </tr>`;
  }).join("");
}

/* ------------------------------------------------ 批量操作 */

function selectedIds() {
  return $$(".row-check:checked").map((c) => c.dataset.id);
}

function updateBatchBar() {
  const ids = selectedIds();
  $("#batchBar").classList.toggle("show", ids.length > 0);
  $("#batchCount").textContent = `已选 ${ids.length} 项`;
}

$("#checkAll").addEventListener("change", (e) => {
  $$(".row-check").forEach((c) => { c.checked = e.target.checked; });
  updateBatchBar();
});

$("#profileRows").addEventListener("change", (e) => {
  if (e.target.classList.contains("row-check")) updateBatchBar();
});

$("#btnBatchClear").addEventListener("click", () => {
  $$(".row-check").forEach((c) => { c.checked = false; });
  $("#checkAll").checked = false;
  updateBatchBar();
});

$("#btnBatchStart").addEventListener("click", async () => {
  const ids = selectedIds();
  toast(`正在批量启动 ${ids.length} 个环境（逐个拉起，请耐心等待）…`);
  const r = await api("POST", "/browser/start-batch", { profile_ids: ids });
  toast(`批量启动完成：成功 ${r.started}/${ids.length}`);
  await Promise.all([loadProfiles(), loadStatus()]);
});

$("#btnBatchStop").addEventListener("click", async () => {
  const ids = selectedIds();
  const r = await api("POST", "/browser/stop-batch", { profile_ids: ids });
  toast(`批量停止完成：${r.stopped}/${ids.length}`);
  await Promise.all([loadProfiles(), loadStatus()]);
});

$("#btnBatchDelete").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!confirm(`确定删除选中的 ${ids.length} 个环境？数据将一并删除，不可恢复。`)) return;
  const r = await api("POST", "/profiles/batch-delete", { profile_ids: ids });
  toast(`已删除 ${r.deleted}/${ids.length} 个环境`);
  await loadProfiles();
});

$("#btnBatchExport").addEventListener("click", async () => {
  const ids = selectedIds();
  const exports = [];
  for (const id of ids) exports.push(await api("GET", `/profiles/${id}/export`));
  downloadJson(exports, `fpwb-profiles-${ids.length}.json`);
  toast(`已导出 ${ids.length} 个环境`);
});

/* ------------------------------------------------ 环境就绪度体检 */

const VENDOR_NAMES = { cloudflare: "Cloudflare", google: "Google", geetest: "极验", yidun: "易盾" };

async function runReadiness(id) {
  $("#readinessTitle").textContent = "";
  $("#readinessBody").innerHTML =
    '<p class="muted">检测中（约 10~40 秒）：实测网络出口、时区、语言、WebRTC、自动化标记与指纹稳定性…</p>';
  $("#readinessModal").classList.remove("hidden");
  try {
    const r = await api("POST", `/profiles/${id}/readiness`);
    const statusIcon = { pass: '<span class="pill ok">✔ 通过</span>',
                         warn: '<span class="pill warn">⚠ 建议</span>',
                         fail: '<span class="pill err">✘ 风险</span>' };
    const verdictColor = { ready: "var(--ok)", needs_work: "var(--warn)", danger: "var(--danger)" };
    const verdictPill = { ready: "success", needs_work: "warn", danger: "failed" };
    $("#readinessTitle").textContent = `${r.profile_name} · ${r.kernel} · 预设 ${r.preset} · ${r.checked_at.replace("T", " ").slice(0, 19)}`;
    $("#readinessBody").innerHTML = `
      <div class="kv" style="margin-bottom:12px">
        <div class="item"><div class="k">综合得分</div>
          <div class="v" style="font-size:26px;color:${verdictColor[r.verdict]}">${r.score}<span style="font-size:14px;color:var(--muted)">/100</span></div></div>
        <div class="item"><div class="k">结论</div><div class="v"><span class="pill ${verdictPill[r.verdict]}" style="font-size:13px">${r.verdict_label}</span></div></div>
      </div>
      <table class="list"><thead><tr><th style="width:80px">状态</th><th>检测项</th><th>详情 / 整改建议</th><th>关联风控</th></tr></thead>
      <tbody>${r.checks.map((c) => `<tr>
        <td>${statusIcon[c.status]}</td>
        <td><b>${{ip_reachable: "网络出口", tz_match: "时区一致性", locale_match: "语言一致性",
                   webrtc_leak: "WebRTC 泄露", webdriver: "自动化标记", canvas_stable: "Canvas 稳定性",
                   webgl_sane: "WebGL 渲染器", fonts_render: "字体渲染"}[c.id] || c.id}</b></td>
        <td>${esc(c.detail)}${c.advice ? `<br><span class="muted">建议：${esc(c.advice)}</span>` : ""}</td>
        <td>${c.vendors.map((v) => `<span class="tag">${VENDOR_NAMES[v] || v}</span>`).join(" ")}</td>
      </tr>`).join("")}</tbody></table>`;
  } catch (e) {
    $("#readinessBody").innerHTML = `<p style="color:var(--danger)">检测失败：${esc(e.message)}</p>`;
  }
}

/* ------------------------------------------------ 环境表单 */

function openProfileModal(profile, batchMode = false) {
  editingId = profile ? profile.id : null;
  $("#profileModalTitle").textContent = batchMode ? "批量新建环境"
    : profile ? `编辑环境：${profile.name}` : "新建环境";
  $("#regenRow").style.display = profile && !batchMode ? "" : "none";
  $("#batchCountWrap").style.display = batchMode ? "" : "none";
  const f = $("#profileForm");
  f.reset();
  f.querySelector('[name=proxy_scheme]').value = "http";
    if (profile) {
      f.kernel.value = profile.kernel;
      f.target_os.value = profile.target_os;
      f.fingerprint_mode.value = profile.fingerprint_summary?.mode === "真实预设" ? "preset" : "generate";
      f.group_name.value = profile.group_name;
      f.name.value = profile.name;
      f.notes.value = profile.notes;
      const l = profile.launch || {};
      f.preset.value = l.preset || "standard";
      f.headless.checked = !!l.headless;
      f.geoip.checked = l.geoip !== false;
      f.humanize.checked = l.humanize !== false;
      f.block_webrtc.checked = !!l.block_webrtc;
      f.disable_coop.checked = !!l.disable_coop;
      f.disable_adblock.checked = !!l.disable_adblock;
      f.locale.value = l.locale || "";
      f.timezone.value = l.timezone || "";
      f.start_url.value = l.start_url || "about:blank";
    if (profile.proxy_full) {
      f.proxy_scheme.value = profile.proxy_full.scheme;
      f.proxy_host.value = profile.proxy_full.host;
      f.proxy_port.value = profile.proxy_full.port;
      f.proxy_username.value = profile.proxy_full.username || "";
      f.proxy_password.value = profile.proxy_full.password || "";
    }
  }
  $("#proxyResult").textContent = "";
  $("#profileModal").classList.remove("hidden");
}

function collectProfileForm() {
  const f = $("#profileForm");
  const fd = new FormData(f);
  const launch = {
    preset: fd.get("preset"),
    headless: fd.get("headless") === "on",
    geoip: fd.get("geoip") === "on",
    humanize: fd.get("humanize") === "on",
    block_webrtc: fd.get("block_webrtc") === "on",
    disable_coop: fd.get("disable_coop") === "on",
    disable_adblock: fd.get("disable_adblock") === "on",
    locale: fd.get("locale").trim() || null,
    timezone: fd.get("timezone").trim() || null,
    start_url: fd.get("start_url").trim() || "about:blank",
  };
  let proxy = null;
  if (fd.get("proxy_host").trim()) {
    proxy = {
      scheme: fd.get("proxy_scheme"),
      host: fd.get("proxy_host").trim(),
      port: Number(fd.get("proxy_port")),
      username: fd.get("proxy_username") || null,
      password: fd.get("proxy_password") || null,
    };
  }
  return {
    mode: fd.get("batch_count") && fd.get("batch_count") !== "" && $("#batchCountWrap").style.display !== "none" ? "batch" : "single",
    count: Number(fd.get("batch_count")) || 1,
    body: {
      name: fd.get("name").trim(),
      group_name: fd.get("group_name").trim() || "默认分组",
      notes: fd.get("notes"),
      kernel: fd.get("kernel"),
      target_os: fd.get("target_os"),
      fingerprint_mode: fd.get("fingerprint_mode"),
      proxy, launch,
    },
    regen: fd.get("regen_fingerprint") === "on",
  };
}

$("#btnNew").addEventListener("click", () => openProfileModal(null, false));
$("#btnBatchNew").addEventListener("click", () => openProfileModal(null, true));

$("#profileForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { mode, count, body, regen } = collectProfileForm();
  try {
    if (mode === "batch") {
      const r = await api("POST", "/profiles/batch", { count, template: body });
      toast(`已批量创建 ${r.count} 个环境`);
    } else if (editingId) {
      if (!body.proxy) body.clear_proxy = true;
      if (regen) body.regen_fingerprint = true;
      await api("PUT", `/profiles/${editingId}`, body);
      toast("环境已更新");
    } else {
      await api("POST", "/profiles", body);
      toast("环境已创建");
    }
    $("#profileModal").classList.add("hidden");
    await loadProfiles();
  } catch (err) {
    toast(`保存失败：${err.message}`, "err");
  }
});

/* ------------------------------------------------ 环境行操作 */

async function startProfile(id) {
  toast("正在启动环境（首次启动约 30~60 秒）…");
  try {
    const info = await api("POST", "/browser/start", { profile_id: id });
    if (info.kernel === "chromium" && info.ws_endpoint) {
      toast(`已启动，CDP 端点：${info.ws_endpoint}`);
    } else toast("环境已启动");
  } catch (e) { toast(`启动失败：${e.message}`, "err"); }
  await Promise.all([loadProfiles(), loadStatus()]);
}

async function stopProfile(id) {
  try {
    await api("POST", "/browser/stop", { profile_id: id });
    toast("环境已停止");
  } catch (e) { toast(`停止失败：${e.message}`, "err"); }
  await Promise.all([loadProfiles(), loadStatus()]);
}

async function showFingerprint(id) {
  try {
    const p = await api("GET", `/profiles/${id}`);
    $("#fpDetail").textContent = JSON.stringify({
      摘要: p.fingerprint_summary,
      指纹: p.fingerprint,
    }, null, 2);
    $("#fpModal").classList.remove("hidden");
  } catch (e) { toast(`读取指纹失败：${e.message}`, "err"); }
}

async function exportProfile(id) {
  const p = profiles.find((x) => x.id === id);
  if (!confirm(`导出「${p.name}」？（含数据目录归档时文件较大且包含 Cookie，请妥善保管）\n\n确定=含数据目录归档\n取消=仅配置与指纹`)) {
    const data = await api("GET", `/profiles/${id}/export?include_data=false`);
    downloadJson(data, `fpwb-${p.name}.json`);
  } else {
    const data = await api("GET", `/profiles/${id}/export?include_data=true`);
    downloadJson(data, `fpwb-${p.name}-full.json`);
  }
  toast("已导出");
}

$("#profileRows").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn || btn.disabled) return;
  const id = btn.closest("tr").dataset.id;
  const p = profiles.find((x) => x.id === id);
  const act = btn.dataset.act;
  if (act === "start") startProfile(id);
  else if (act === "stop") stopProfile(id);
  else if (act === "readiness") runReadiness(id);
  else if (act === "edit") openProfileModal(p);
  else if (act === "fp") showFingerprint(id);
  else if (act === "export") exportProfile(id);
  else if (act === "del") {
    if (confirm(`确定删除环境「${p.name}」？不可恢复。`)) {
      api("DELETE", `/profiles/${id}`).then(() => { toast("已删除"); loadProfiles(); })
        .catch((err) => toast(`删除失败：${err.message}`, "err"));
    }
  }
});

/* ------------------------------------------------ 导入 */

$("#btnImport").addEventListener("click", () => {
  $("#importFile").value = "";
  $("#importModal").classList.remove("hidden");
});

$("#btnDoImport").addEventListener("click", async () => {
  const file = $("#importFile").files[0];
  if (!file) { toast("请选择 JSON 文件", "err"); return; }
  try {
    const raw = JSON.parse(await file.text());
    const list = Array.isArray(raw) ? raw : [raw];
    let count = 0;
    for (const item of list) {
      await api("POST", "/profiles/import", item);
      count++;
    }
    toast(`成功导入 ${count} 个环境`);
    $("#importModal").classList.add("hidden");
    await loadProfiles();
  } catch (e) {
    toast(`导入失败：${e.message}`, "err");
  }
});

/* ------------------------------------------------ 代理测试 */

async function testProxyFromForm() {
  const f = $("#profileForm");
  const host = f.proxy_host.value.trim();
  const box = $("#proxyResult");
  if (!host) { box.textContent = "请先填写代理地址"; box.className = "proxy-result bad"; return; }
  box.textContent = "测试中…"; box.className = "proxy-result";
  try {
    const r = await api("POST", "/proxy/test", {
      proxy: {
        scheme: f.proxy_scheme.value, host,
        port: Number(f.proxy_port.value),
        username: f.proxy_username.value || null,
        password: f.proxy_password.value || null,
      },
    });
    if (r.ok) {
      box.textContent = `✔ 出口 ${r.exit_ip}（${r.country} ${r.city}，${r.timezone}）延迟 ${r.latency_ms}ms`;
      box.className = "proxy-result good";
    } else { box.textContent = `✘ ${r.error}`; box.className = "proxy-result bad"; }
  } catch (e) { box.textContent = `✘ ${e.message}`; box.className = "proxy-result bad"; }
}

$("#btnTestProxyInForm").addEventListener("click", testProxyFromForm);
$("#btnProxyTest").addEventListener("click", async () => {
  toast("直连测试中…");
  try {
    const r = await api("POST", "/proxy/test", { proxy: null });
    if (r.ok) toast(`当前直连出口：${r.exit_ip}（${r.country} ${r.city}）`);
    else toast(`直连测试失败：${r.error}`, "err");
  } catch (e) { toast(`测试失败：${e.message}`, "err"); }
});

/* ------------------------------------------------ 自检链接 */

async function loadDetectLinks() {
  try {
    const links = await api("GET", "/detect-links");
    $("#detectLinks").innerHTML = Object.entries(links)
      .map(([name, url]) => `<a href="${esc(url)}" target="_blank">${esc(name)}</a>`).join("");
  } catch (e) { toast(`加载链接失败：${e.message}`, "err"); }
}
$("#btnDetect").addEventListener("click", () => $("#detectModal").classList.remove("hidden"));

/* ------------------------------------------------ RPA 任务 */

const STEP_FIELDS = {
  navigate: [["url", "URL", true], ["timeout", "超时(ms)", false]],
  click: [["selector", "CSS 选择器", true], ["timeout", "超时(ms)", false]],
  type: [["selector", "CSS 选择器", true], ["text", "输入文本", true], ["press_enter", "回车提交", false, "check"]],
  press: [["key", "按键（如 Enter）", true]],
  wait: [["ms", "等待毫秒", true]],
  wait_for: [["selector", "CSS 选择器", true], ["timeout", "超时(ms)", false]],
  scroll: [["amount", "滚动像素（负为向上）", true]],
  screenshot: [["name", "截图名（可选）", false], ["full_page", "整页截图", false, "check"]],
  extract: [["selector", "CSS 选择器", true], ["attr", "属性名（留空取文本）", false], ["var", "存入变量名（可选）", false]],
  evaluate: [["expression", "JS 表达式", true, "textarea"], ["var", "存入变量名（可选）", false]],
  hover: [["selector", "CSS 选择器", true], ["timeout", "超时(ms)", false]],
  select: [["selector", "CSS 选择器", true], ["value", "选项值或文本", true]],
  upload: [["selector", "文件输入框选择器", true], ["path", "本地文件路径", true]],
  download: [["url", "下载地址（留空按回车触发）", false], ["timeout", "超时(ms)", false]],
  tab_open: [["url", "打开的 URL", false]],
  tab_switch: [["value", "标签页序号或标题包含文本", true]],
  tab_close: [],
  set_var: [["name", "变量名", true], ["value", "值（支持 {{var}} 引用）", false]],
  label: [["label", "标签名", true]],
  goto: [["label", "目标标签名", true]],
  if: [["var", "变量名", true], ["op", "比较方式", false, "select", [["equals", "等于"], ["contains", "包含"], ["exists", "已定义"]]], ["value", "比较值", false], ["then_goto", "成立时跳转标签", true], ["else_goto", "否则跳转标签（可选）", false]],
};

/* 通用高级字段：所有动作可配（label/goto/if/set_var 除外——语义冲突） */
const STEP_COMMON_FIELDS = new Set(["label", "goto", "if", "set_var"]);
const STEP_COMMON = [
  ["frame", "iframe 选择器（可选，限定步骤在 iframe 内执行）", "text"],
  ["retry", "失败重试(0-5)", "number"],
  ["on_error", "失败处置：abort=终止 / continue=继续 / goto:标签名", "text"],
];

const STEP_LABELS = {
  navigate: "打开页面", click: "点击", type: "输入文本", press: "按键",
  wait: "等待", wait_for: "等待元素", scroll: "滚动", screenshot: "截图",
  extract: "抽取数据", evaluate: "执行 JS",
  hover: "悬停", select: "下拉选择", upload: "上传文件", download: "下载文件",
  tab_open: "新标签页", tab_switch: "切换标签", tab_close: "关闭标签",
  set_var: "设置变量", label: "标签", goto: "跳转", if: "条件分支",
};

function stepFieldHtml(key, label, required, kind, val, options) {
  if (kind === "check") {
    return `<label class="checkbox w-s"><input type="checkbox" data-k="${key}" ${val ? "checked" : ""}>${label}</label>`;
  }
  if (kind === "textarea") {
    return `<label style="flex-basis:100%">${label}<textarea data-k="${key}" rows="2">${esc(val)}</textarea></label>`;
  }
  if (kind === "select") {
    const opts = (options || []).map(([v, l]) =>
      `<option value="${esc(v)}" ${String(val ?? "") === String(v) ? "selected" : ""}>${l}</option>`).join("");
    return `<label>${label}<select data-k="${key}"><option value="" ${!val ? "selected" : ""}></option>${opts}</select></label>`;
  }
  const type = kind === "number" ? "number" : "text";
  return `<label>${label}<input type="${type}" data-k="${key}" value="${esc(val)}" ${required ? `data-req="1" placeholder="必填"` : ""}></label>`;
}

function stepRowHtml(step = { action: "navigate" }) {
  const action = step.action || "navigate";
  const fields = STEP_FIELDS[action] || [];
  const params = fields.map(([key, label, required, kind, options]) =>
    stepFieldHtml(key, label, required, kind, step[key] ?? "", options)).join("");
  // 通用高级字段（frame / retry / on_error），折叠收纳避免喧宾夺主
  const common = STEP_COMMON_FIELDS.has(action) ? "" :
    `<details class="step-advanced"><summary>高级</summary><div class="step-params">` +
    STEP_COMMON.map(([key, label, kind]) =>
      stepFieldHtml(key, label, false, kind, step[key] ?? "")).join("") +
    `</div></details>`;
  return `<div class="step-row">
    <select class="step-action">
      ${Object.entries(STEP_LABELS).map(([v, l]) =>
        `<option value="${v}" ${v === action ? "selected" : ""}>${l}</option>`).join("")}
    </select>
    <div class="step-params">${params}</div>
    ${common}
    <button type="button" class="btn small danger step-del">✕</button>
  </div>`;
}

function renderStepEditor(steps) {
  $("#stepEditor").innerHTML = (steps && steps.length ? steps : [{}]).map((s) => stepRowHtml(s)).join("");
}

$("#stepEditor").addEventListener("change", (e) => {
  if (e.target.classList.contains("step-action")) {
    const row = e.target.closest(".step-row");
    // 切换动作时保留通用字段（selector/url 等）
    const kept = {};
    row.querySelectorAll("[data-k]").forEach((inp) => {
      if (inp.type === "checkbox") { if (inp.checked) kept[inp.dataset.k] = true; }
      else if (inp.value) kept[inp.dataset.k] = inp.value;
    });
    const tmp = document.createElement("div");
    tmp.innerHTML = stepRowHtml({ action: e.target.value, ...kept });
    row.replaceWith(tmp.firstChild);
  }
});

$("#stepEditor").addEventListener("click", (e) => {
  if (e.target.classList.contains("step-del")) {
    if ($$("#stepEditor .step-row").length <= 1) { toast("至少保留一个步骤", "err"); return; }
    e.target.closest(".step-row").remove();
  }
});

$("#btnAddStep").addEventListener("click", () => {
  $("#stepEditor").insertAdjacentHTML("beforeend", stepRowHtml());
});

function collectSteps() {
  return $$("#stepEditor .step-row").map((row) => {
    const step = { action: row.querySelector(".step-action").value };
    row.querySelectorAll("[data-k]").forEach((inp) => {
      const k = inp.dataset.k;
      if (inp.type === "checkbox") { if (inp.checked) step[k] = true; }
      else if (inp.value !== "") {
        step[k] = (k === "timeout" || k === "ms" || k === "amount" || k === "retry") ? Number(inp.value) : inp.value;
      }
    });
    return step;
  });
}

$("#btnNewTask").addEventListener("click", () => {
  editingTaskId = null;
  $("#taskModalTitle").textContent = "新建任务";
  $("#taskName").value = "";
  $("#taskNotes").value = "";
  $("#taskWebhook").value = "";
  renderStepEditor([]);
  $("#taskModal").classList.remove("hidden");
});

$("#taskSave").addEventListener("click", async () => {
  const name = $("#taskName").value.trim();
  if (!name) { toast("请填写任务名称", "err"); return; }
  const steps = collectSteps();
  const webhook = $("#taskWebhook").value.trim() || null;
  try {
    if (editingTaskId) await api("PUT", `/tasks/${editingTaskId}`, { name, notes: $("#taskNotes").value, steps, webhook_url: webhook });
    else await api("POST", "/tasks", { name, notes: $("#taskNotes").value, steps, webhook_url: webhook });
    toast("任务已保存");
    $("#taskModal").classList.add("hidden");
    await loadTasks();
  } catch (e) { toast(`保存失败：${e.message}`, "err"); }
});

async function loadTasks() {
  tasks = await api("GET", "/tasks");
  const tbody = $("#taskRows");
  tbody.innerHTML = tasks.length ? tasks.map((t) => `<tr data-id="${t.id}">
    <td><b>${esc(t.name)}</b></td>
    <td>${t.steps_count ?? t.steps.length}</td>
    <td class="muted">${esc(t.notes)}</td>
    <td class="mono">${esc(t.updated_at?.replace("T", " ").slice(0, 19))}</td>
    <td>
      <button class="btn small primary" data-act="run">运行</button>
      <button class="btn small" data-act="edit">编辑</button>
      <button class="btn small danger" data-act="del">删除</button>
    </td>
  </tr>`).join("") : '<tr><td colspan="5" class="empty">暂无任务，点击「新建任务」创建</td></tr>';
  await loadRuns();
}

$("#btnWarmupTpl").addEventListener("click", async () => {
  if (!confirm("创建「环境预热」任务模板？（访问中性高流量站点积累浏览历史与信誉）")) return;
  try {
    await api("POST", "/task-templates/warmup/create");
    toast("预热任务已创建，可在任务列表中对其运行（建议有头模式+人机化）");
    await loadTasks();
  } catch (e) { toast(e.message, "err"); }
});

$("#taskRows").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const t = tasks.find((x) => x.id === id);
  if (btn.dataset.act === "edit") {
    editingTaskId = id;
    $("#taskModalTitle").textContent = `编辑任务：${t.name}`;
    $("#taskName").value = t.name;
    $("#taskNotes").value = t.notes;
    $("#taskWebhook").value = t.webhook_url || "";
    renderStepEditor(t.steps);
    $("#taskModal").classList.remove("hidden");
  } else if (btn.dataset.act === "run") {
    currentRunTaskId = t.id;
    openRunModal(t);
  } else if (btn.dataset.act === "del") {
    if (confirm(`确定删除任务「${t.name}」？`)) {
      api("DELETE", `/tasks/${id}`).then(() => { toast("已删除"); loadTasks(); })
        .catch((err) => toast(`删除失败：${err.message}`, "err"));
    }
  }
});

/* ------------------------------------------------ 任务运行 */

function openRunModal(task) {
  $("#runModalTitle").textContent = `运行任务：${task.name}`;
  $("#runProfileList").innerHTML = profiles.map((p) => `
    <label class="checkbox"><input type="checkbox" value="${p.id}" class="run-p">
      ${esc(p.name)}（${esc(p.kernel)} / ${esc(p.target_os)}）</label>`).join("")
    || '<p class="muted">没有可选环境</p>';
  $("#runAutoClose").checked = true;
  $("#runVisible").checked = false;
  $("#runModal").classList.remove("hidden");
}

$("#btnDoRun").addEventListener("click", async () => {
  const ids = $$(".run-p:checked").map((c) => c.value);
  if (!ids.length) { toast("请选择至少一个环境", "err"); return; }
  try {
    const r = await api("POST", `/tasks/${currentRunTaskId}/run`, {
      profile_ids: ids,
      headless: !$("#runVisible").checked,
      auto_close: $("#runAutoClose").checked,
      humanize: $("#runHumanize").checked,
    });
    toast(`已提交 ${r.count} 个运行（Run: ${r.run_ids.join(", ")}）`);
    $("#runModal").classList.add("hidden");
    await loadRuns();
  } catch (e) { toast(`运行失败：${e.message}`, "err"); }
});

async function loadRuns() {
  const runs = await api("GET", "/task-runs?limit=50");
  const tbody = $("#runRows");
  tbody.innerHTML = runs.length ? runs.map((r) => `<tr data-id="${r.id}">
    <td class="mono">${esc(r.id)}</td>
    <td>${esc(r.task_name)}</td>
    <td>${esc(r.profile_name)}</td>
    <td><span class="pill ${{ success: "success", failed: "failed", running: "running", cancelled: "cancelled" }[r.status] || "muted"}">${{ success: "✔ 成功", failed: "✘ 失败", running: "⟳ 运行中", cancelled: "已取消" }[r.status] || esc(r.status)}</span></td>
    <td class="mono">${esc(r.started_at?.replace("T", " ").slice(0, 19))}</td>
    <td>
      <button class="btn small" data-act="detail">详情</button>
      ${r.status === "running" ? '<button class="btn small danger" data-act="cancel">取消</button>' : ""}
    </td>
  </tr>`).join("") : '<tr><td colspan="6" class="empty">暂无运行记录</td></tr>';
}

$("#runRows").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  if (btn.dataset.act === "detail") showRunDetail(id);
  else if (btn.dataset.act === "cancel") {
    api("POST", `/task-runs/${id}/cancel`).then(() => { toast("已请求取消"); loadRuns(); })
      .catch((err) => toast(`取消失败：${err.message}`, "err"));
  }
});

async function showRunDetail(runId) {
  currentRunId = runId;
  await refreshRunDetail();
  $("#runDetailModal").classList.remove("hidden");
}

async function refreshRunDetail() {
  if (!currentRunId) return;
  const r = await api("GET", `/task-runs/${currentRunId}`);
  $("#runDetailId").textContent = `${r.id} · ${r.task_name} · ${r.profile_name}`;
  $("#runDetailBody").innerHTML = `
    <div class="kv">
      <div class="item"><div class="k">状态</div><div class="v run-status ${esc(r.status)}">${esc(r.status)}</div></div>
      <div class="item"><div class="k">开始</div><div class="v mono">${esc(r.started_at?.replace("T", " ").slice(0, 19))}</div></div>
      <div class="item"><div class="k">结束</div><div class="v mono">${esc(r.finished_at?.replace("T", " ").slice(0, 19) || "—")}</div></div>
      ${r.error ? `<div class="item"><div class="k">错误</div><div class="v" style="color:var(--danger)">${esc(r.error)}</div></div>` : ""}
    </div>
    ${(r.results || []).map((s) => `
      <div class="step-result s-${esc(s.status)}">
        <div class="head"><b>${STEP_LABELS[s.action] || esc(s.action)}</b>
          <span class="muted">#${s.index + 1}</span>
          <span class="${s.status === "ok" ? "" : "run-status " + esc(s.status)}">${s.status === "ok" ? "✔" : esc(s.status)}</span>
        </div>
        ${s.detail ? `<div class="muted" style="margin-top:4px">${esc(s.detail)}</div>` : ""}
        ${s.screenshot ? `<img src="${esc(s.screenshot)}" loading="lazy">` : ""}
        ${s.extracted ? `<div class="extracted"><b>抽取结果 (${s.extracted.length})：</b><br>${esc(s.extracted.join(" | "))}</div>` : ""}
        ${s.value !== undefined ? `<div class="extracted"><b>返回值：</b><br>${esc(JSON.stringify(s.value, null, 2))}</div>` : ""}
      </div>`).join("") || '<p class="muted">尚无步骤结果</p>'}`;
}

$("#btnRefreshRun").addEventListener("click", () => refreshRunDetail().catch((e) => toast(e.message, "err")));

/* ------------------------------------------------ 审计日志 */

async function loadLogs(append = false) {
  const action = $("#logFilter").value.trim();
  const qs = `/audit-logs?limit=50&offset=${logOffset}${action ? `&action=${encodeURIComponent(action)}` : ""}`;
  const logs = await api("GET", qs);
  const rows = logs.map((l) => `<tr>
    <td class="mono">${esc(l.ts?.replace("T", " ").slice(0, 19))}</td>
    <td class="mono" style="color:var(--primary)">${esc(l.action)}</td>
    <td>${esc(l.target)}</td>
    <td class="muted">${esc(l.detail)}</td>
    <td>${l.result === "ok" ? '<span class="pill ok">成功</span>' : `<span class="pill err">${esc(l.result)}</span>`}</td>
  </tr>`);
  const tbody = $("#logRows");
  if (append) tbody.insertAdjacentHTML("beforeend", rows.join(""));
  else tbody.innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">暂无日志</td></tr>';
  $("#btnMoreLogs").disabled = logs.length < 50;
}

$("#btnRefreshLogs").addEventListener("click", () => { logOffset = 0; loadLogs().catch((e) => toast(e.message, "err")); });
$("#logFilter").addEventListener("keydown", (e) => { if (e.key === "Enter") { logOffset = 0; loadLogs().catch(() => {}); } });
$("#btnMoreLogs").addEventListener("click", () => { logOffset += 50; loadLogs(true).catch(() => {}); });

/* ------------------------------------------------ 设置 */

async function loadSettings() {
  const s = await api("GET", "/settings");
  $("#keyState").textContent = s.api_key_enabled ? "已开启 🔒" : "未开启";
  $("#keyMasked").textContent = s.api_key_masked;
  $("#btnToggleKey").textContent = s.api_key_enabled ? "关闭认证" : "开启认证";
  $("#fullKeyHint").textContent = "";
  $("#btnToggleSyncServer").textContent = s.sync_server_enabled ? "关闭同步服务器" : "开启同步服务器";
  $("#syncTokenHint").textContent = s.sync_server_enabled ? `令牌：${s.sync_token_masked}` : "";
  if (document.activeElement !== $("#syncRemoteUrl")) $("#syncRemoteUrl").value = s.sync_remote_url || "";
  if (!s.sync_remote_configured) $("#syncRemoteToken").value = "";
  await Promise.all([loadMembers(), loadIdentity()]);
}

async function loadIdentity() {
  try {
    const me = await api("GET", "/me");
    $("#identityBadge").innerHTML =
      `身份：<b>${esc(me.name)}</b>（${me.role === "admin" ? "管理员" : "操作员"}）` +
      (me.auth_enabled ? "" : " · 认证未开启");
  } catch (e) { $("#identityBadge").textContent = ""; }
}

async function loadMembers() {
  try {
    const members = await api("GET", "/members");
    $("#memberRows").innerHTML = members.map((m) => `<tr data-id="${m.id}">
      <td><b>${esc(m.name)}</b></td>
      <td>${m.role === "admin" ? "admin（管理员）" : "operator（操作员）"}</td>
      <td class="mono">${esc(m.api_key_masked)}</td>
      <td>${m.enabled ? '<span class="pill ok">启用</span>' : '<span class="pill muted">停用</span>'}</td>
      <td>
        <button class="btn small" data-act="toggle">${m.enabled ? "停用" : "启用"}</button>
        <button class="btn small danger" data-act="del">删除</button>
      </td>
    </tr>`).join("");
  } catch (e) {
    $("#memberRows").innerHTML = `<tr><td colspan="5" class="empty muted">${esc(e.message)}</td></tr>`;
  }
}

$("#memberRows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  try {
    if (btn.dataset.act === "toggle") { await api("POST", `/members/${id}/toggle`); await loadMembers(); }
    else if (btn.dataset.act === "del") {
      if (confirm("确定删除该成员？其密钥立即失效。")) {
        await api("DELETE", `/members/${id}`); await loadMembers();
      }
    }
  } catch (err) { toast(err.message, "err"); }
});

$("#btnAddMember").addEventListener("click", async () => {
  const name = $("#memberName").value.trim();
  if (!name) { toast("请填写成员名称", "err"); return; }
  try {
    const m = await api("POST", "/members", { name, role: $("#memberRole").value });
    $("#newMemberKey").textContent = `${name} 的密钥：${m.api_key}（仅显示一次，请复制保存）`;
    $("#memberName").value = "";
    await loadMembers();
  } catch (e) { toast(`添加失败：${e.message}`, "err"); }
});

$("#btnToggleKey").addEventListener("click", async () => {
  try {
    const s = await api("GET", "/settings");
    const enable = !s.api_key_enabled;
    if (enable && !confirm("开启后所有 API 请求都需要成员 X-API-Key，确认开启？")) return;
    const r = await api("POST", "/settings", { api_key_enabled: enable });
    if (enable) {
      $("#fullKeyHint").textContent = "本浏览器已沿用管理员密钥";
      toast("认证已开启");
    } else toast("认证已关闭");
    await Promise.all([loadSettings(), loadStatus(), loadIdentity()]);
  } catch (e) { toast(e.message, "err"); }
});

$("#btnRegenKey").addEventListener("click", async () => {
  if (!confirm("重新生成后旧管理员密钥立即失效，确认？")) return;
  const r = await api("POST", "/settings", { regenerate_key: true });
  localStorage.setItem("fpwb_api_key", r.api_key);
  $("#fullKeyHint").textContent = `新管理员密钥：${r.api_key}（已自动填入本浏览器）`;
  toast("管理员密钥已重新生成");
  await loadSettings();
});

$("#btnPairCode").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/pair/create");
    $("#fullKeyHint").textContent =
      `插件配对码：${r.pairing_code}（${Math.round(r.expires_in / 60)} 分钟内有效，一次性）`;
    toast(`配对码 ${r.pairing_code} 已生成，请在插件中输入`);
  } catch (e) { toast(`生成失败：${e.message}`, "err"); }
});

$("#btnToggleSyncServer").addEventListener("click", async () => {
  const s = await api("GET", "/settings");
  const enable = !s.sync_server_enabled;
  if (enable && !confirm("开启后其它节点可用同步令牌向本机推拉环境配置，确认开启？")) return;
  const r = await api("POST", "/settings", { sync_server_enabled: enable });
  if (enable && r.sync_token) {
    $("#syncTokenHint").textContent = `新同步令牌：${r.sync_token}（请复制保存）`;
  }
  toast(enable ? "同步服务器已开启" : "同步服务器已关闭");
  await Promise.all([loadSettings(), loadStatus()]);
});

$("#btnRegenSyncToken").addEventListener("click", async () => {
  if (!confirm("重新生成后旧同步令牌立即失效，确认？")) return;
  const r = await api("POST", "/settings", { regenerate_sync_token: true });
  $("#syncTokenHint").textContent = `新同步令牌：${r.sync_token}（请复制保存）`;
  await loadSettings();
});

$("#btnSaveSyncRemote").addEventListener("click", async () => {
  try {
    const body = { sync_remote_url: $("#syncRemoteUrl").value.trim() };
    const token = $("#syncRemoteToken").value.trim();
    if (token) body.sync_remote_token = token;
    await api("POST", "/settings", body);
    $("#syncRemoteToken").value = "";
    toast("远端同步配置已保存");
    await loadSettings();
  } catch (e) { toast(e.message, "err"); }
});

$("#btnSyncPush").addEventListener("click", async () => {
  $("#syncResult").textContent = "推送中…";
  try {
    const r = await api("POST", "/sync/push");
    $("#syncResult").textContent = `推送完成：新建 ${r.created} 更新 ${r.updated} 跳过 ${r.skipped}`;
    toast("推送完成");
  } catch (e) { $("#syncResult").textContent = ""; toast(`推送失败：${e.message}`, "err"); }
});

$("#btnSyncPull").addEventListener("click", async () => {
  if (!confirm("拉取会按最新修改合并本地环境（远端删除会同步删除本地），确认？")) return;
  $("#syncResult").textContent = "拉取中…";
  try {
    const r = await api("POST", "/sync/pull");
    $("#syncResult").textContent = `拉取完成：新建 ${r.created} 更新 ${r.updated} 删除 ${r.deleted}`;
    toast("拉取完成");
    await loadProfiles();
  } catch (e) { $("#syncResult").textContent = ""; toast(`拉取失败：${e.message}`, "err"); }
});

$("#btnBackup").addEventListener("click", async () => {
  const backup = await api("GET", "/system/backup");
  downloadJson(backup, `fpwb-backup-${new Date().toISOString().slice(0, 10)}.json`);
  toast(`已下载备份（${backup.count} 个环境）`);
});

$("#btnRestore").addEventListener("click", async () => {
  const file = $("#restoreFile").files[0];
  if (!file) { toast("请选择备份 JSON 文件", "err"); return; }
  if (!confirm("恢复将清空并重建现有全部环境（数据目录不保留），确认继续？")) return;
  try {
    const backup = JSON.parse(await file.text());
    const r = await api("POST", "/system/restore", backup);
    toast(`已恢复 ${r.restored} 个环境`);
    await Promise.all([loadProfiles(), loadStatus()]);
  } catch (e) { toast(`恢复失败：${e.message}`, "err"); }
});

/* ================================================================ Phase 3: 调度计划 */

let editingScheduleId = null;

async function loadSchedules() {
  const list = await api("GET", "/schedules");
  const tbody = $("#scheduleRows");
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无调度计划，点击「新建调度」创建</td></tr>';
    return;
  }
  const taskNames = Object.fromEntries(tasks.map((t) => [t.id, t.name]));
  if (!tasks.length) { try { tasks = await api("GET", "/tasks"); } catch (e) {} }
  tbody.innerHTML = list.map((s) => {
    const task = tasks.find((t) => t.id === s.task_id);
    return `<tr data-id="${s.id}">
      <td><b>${esc(s.name)}</b></td>
      <td>${esc(task ? task.name : s.task_id)}</td>
      <td>${esc(s.describe || (s.kind === "daily" ? `每日 ${s.daily_time}` : `每 ${s.interval_minutes} 分钟`))}</td>
      <td>${s.profile_ids.length}</td>
      <td class="mono">${esc(s.last_run_at?.replace("T", " ").slice(0, 19) || "—")}</td>
      <td class="mono">${esc(s.next_run_at?.replace("T", " ").slice(5, 19) || "—")}</td>
      <td>${s.enabled ? '<span style="color:var(--ok)">启用</span>' : '<span class="muted">暂停</span>'}</td>
      <td>
        <button class="btn small primary" data-act="run">立即运行</button>
        <button class="btn small" data-act="toggle">${s.enabled ? "暂停" : "启用"}</button>
        <button class="btn small" data-act="edit">编辑</button>
        <button class="btn small danger" data-act="del">删除</button>
      </td>
    </tr>`;
  }).join("");
}

function openScheduleModal(schedule) {
  editingScheduleId = schedule ? schedule.id : null;
  $("#scheduleModalTitle").textContent = schedule ? `编辑调度：${schedule.name}` : "新建调度";
  $("#scheduleTask").innerHTML = tasks.map((t) =>
    `<option value="${t.id}" ${schedule && schedule.task_id === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")
    || '<option value="">（请先创建 RPA 任务）</option>';
  $("#scheduleName").value = schedule ? schedule.name : "";
  $("#scheduleKind").value = schedule ? schedule.kind : "daily";
  $("#scheduleDailyTime").value = schedule?.daily_time || "01:30";
  $("#scheduleInterval").value = schedule?.interval_minutes || 60;
  $("#scheduleTimezone").value = schedule?.timezone || "";
  $$("#scheduleWeekdays input[type=checkbox]").forEach((c) =>
    c.checked = schedule ? (schedule.weekdays || []).includes(Number(c.value)) : false);
  $("#scheduleHeadless").checked = schedule ? schedule.headless : true;
  $("#scheduleAutoClose").checked = schedule ? schedule.auto_close : true;
  const selected = new Set(schedule ? schedule.profile_ids : []);
  $("#scheduleProfileList").innerHTML = profiles.map((p) => `
    <label class="checkbox"><input type="checkbox" value="${p.id}" class="sched-p" ${selected.has(p.id) ? "checked" : ""}>
      ${esc(p.name)}（${esc(p.kernel)}）</label>`).join("") || '<p class="muted">没有可选环境</p>';
  syncScheduleKindFields();
  $("#scheduleModal").classList.remove("hidden");
}

function syncScheduleKindFields() {
  const daily = $("#scheduleKind").value === "daily";
  $("#scheduleDailyWrap").style.display = daily ? "" : "none";
  $("#scheduleIntervalWrap").style.display = daily ? "none" : "";
  $("#scheduleTimezoneWrap").style.display = daily ? "" : "none";
  $("#scheduleWeekdayWrap").style.display = daily ? "" : "none";
}
$("#scheduleKind").addEventListener("change", syncScheduleKindFields);

$("#btnNewSchedule").addEventListener("click", async () => {
  if (!tasks.length) { try { tasks = await api("GET", "/tasks"); } catch (e) {} }
  openScheduleModal(null);
});
$("#btnRefreshSchedules").addEventListener("click", () => loadSchedules().then(() => toast("已刷新")).catch((e) => toast(e.message, "err")));

$("#scheduleSave").addEventListener("click", async () => {
  const name = $("#scheduleName").value.trim();
  if (!name) { toast("请填写调度名称", "err"); return; }
  const taskId = $("#scheduleTask").value;
  if (!taskId) { toast("请选择任务（先创建 RPA 任务）", "err"); return; }
  const ids = $$(".sched-p:checked").map((c) => c.value);
  if (!ids.length) { toast("请选择至少一个环境", "err"); return; }
  const body = {
    name, task_id: taskId, kind: $("#scheduleKind").value,
    daily_time: $("#scheduleKind").value === "daily" ? $("#scheduleDailyTime").value : null,
    interval_minutes: $("#scheduleKind").value === "interval" ? Number($("#scheduleInterval").value) : null,
    profile_ids: ids,
    headless: $("#scheduleHeadless").checked, auto_close: $("#scheduleAutoClose").checked,
    timezone: $("#scheduleKind").value === "daily" ? ($("#scheduleTimezone").value.trim() || null) : null,
    weekdays: $("#scheduleKind").value === "daily"
      ? $$("#scheduleWeekdays input[type=checkbox]:checked").map((c) => Number(c.value)) : [],
  };
  try {
    if (editingScheduleId) await api("PUT", `/schedules/${editingScheduleId}`, body);
    else await api("POST", "/schedules", body);
    toast("调度已保存");
    $("#scheduleModal").classList.add("hidden");
    await loadSchedules();
  } catch (e) { toast(`保存失败：${e.message}`, "err"); }
});

$("#scheduleRows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  try {
    if (btn.dataset.act === "run") {
      const r = await api("POST", `/schedules/${id}/run-now`);
      toast(`已提交 ${r.submitted} 个运行，可在 RPA 任务页查看`);
    } else if (btn.dataset.act === "toggle") {
      const list = await api("GET", "/schedules");
      const s = list.find((x) => x.id === id);
      await api("PUT", `/schedules/${id}`, { enabled: !s.enabled });
      await loadSchedules();
    } else if (btn.dataset.act === "edit") {
      const list = await api("GET", "/schedules");
      openScheduleModal(list.find((x) => x.id === id));
    } else if (btn.dataset.act === "del") {
      if (confirm("确定删除该调度？")) { await api("DELETE", `/schedules/${id}`); await loadSchedules(); }
    }
  } catch (err) { toast(err.message, "err"); }
});

/* ================================================================ Phase 3: 矩阵风控 */

let lastMatrix = null;

async function loadMatrix() {
  lastMatrix = await api("GET", "/matrix/report");
    const m = lastMatrix;
    $("#matrixSummary").innerHTML =
      `共 <b>${m.total}</b> 个环境 · <span class="pill ok">健康 ${m.summary.clean}</span> ` +
      `<span class="pill warn">中危 ${m.summary.medium}</span> ` +
      `<span class="pill err">高危 ${m.summary.high}</span>`;
  $("#btnRegenRisky").style.display = m.risks.length ? "" : "none";

  const distSection = (title, items) => {
    if (!items.length) return "";
    const max = items[0].count;
    return `<div class="settings-section"><h3>${title}</h3>` + items.slice(0, 8).map((it) => `
      <div class="matrix-bar-row">
        <span class="matrix-bar-label" title="${esc(it.value)}">${esc(String(it.value))}</span>
        <div class="matrix-bar-track">
          <div class="matrix-bar-fill" style="width:${(it.count / max) * 100}%"></div>
        </div>
        <b class="matrix-bar-count">${it.count}</b>
      </div>`).join("") + "</div>";
  };
  $("#matrixDist").innerHTML =
    distSection("操作系统", m.distribution.os) +
    distSection("GPU 渲染器", m.distribution.gpu) +
    distSection("屏幕分辨率", m.distribution.screen) +
    distSection("CPU 核心", m.distribution.hardware_concurrency);

  $("#matrixDups").innerHTML = m.duplicates.length
    ? m.duplicates.map((g) => `
      <div class="step-result s-failed">
        <div class="head"><b>⚠ ${g.size} 个环境指纹完全相同</b></div>
        <div class="muted" style="margin-top:4px">${esc(g.members.map((x) => x.name).join("、"))}</div>
        <div class="mono muted" style="margin-top:2px;font-size:11px">${esc(g.signature[1])} · ${esc(String(g.signature[2]).slice(0, 60))}</div>
      </div>`).join("")
    : '<p class="muted" style="margin-bottom:12px">✔ 未发现指纹重复</p>';

  $("#matrixRisks").innerHTML = m.risks.length
    ? `<table class="list"><thead><tr><th>环境</th><th>风险</th><th>原因</th><th>操作</th></tr></thead><tbody>` +
      m.risks.map((r) => `<tr data-id="${r.profile_id}">
        <td><b>${esc(r.name)}</b></td>
        <td>${r.risk === "high" ? '<span class="pill failed">高危</span>' : '<span class="pill warn">中危</span>'}</td>
        <td class="muted">${esc(r.reason)}</td>
        <td><button class="btn small primary" data-act="regen">重生成指纹</button></td>
      </tr>`).join("") + "</tbody></table>"
    : '<p class="muted">✔ 全部环境指纹健康</p>';
}

$("#btnRefreshMatrix").addEventListener("click", () => loadMatrix().then(() => toast("扫描完成")).catch((e) => toast(e.message, "err")));

async function regenFingerprints(ids) {
  if (!confirm(`确定为 ${ids.length} 个环境重新生成指纹？（Cookie 保留，指纹焕新）`)) return;
  try {
    const r = await api("POST", "/matrix/regenerate", { profile_ids: ids });
    const okCount = r.filter((x) => x.ok).length;
    toast(`已重生成 ${okCount}/${ids.length} 个环境指纹`);
    await Promise.all([loadMatrix(), loadProfiles()]);
  } catch (e) { toast(e.message, "err"); }
}

$("#matrixRisks").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act='regen']");
  if (btn) regenFingerprints([btn.closest("tr").dataset.id]);
});

$("#btnRegenRisky").addEventListener("click", () => {
  if (!lastMatrix) return;
  regenFingerprints(lastMatrix.risks.map((r) => r.profile_id));
});

/* ------------------------------------------------ 通用模态/初始化 */

$("#btnRefresh").addEventListener("click", () =>
  Promise.all([loadProfiles(), loadStatus()]).then(() => toast("已刷新")));

document.querySelectorAll("[data-close]").forEach((btn) =>
  btn.addEventListener("click", (e) => e.target.closest(".modal-backdrop").classList.add("hidden")));
document.querySelectorAll(".modal-backdrop").forEach((bd) =>
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.classList.add("hidden"); }));

(async function init() {
  try {
    await Promise.all([loadStatus(), loadProfiles(), loadDetectLinks(), loadSettings()]);
    setInterval(() => {
      loadProfiles().catch(() => {});
      loadStatus().catch(() => {});
      if ($("#tab-tasks").classList.contains("active")) loadRuns().catch(() => {});
    }, 5000);
  } catch (e) {
    toast(`初始化失败：${e.message}`, "err");
  }
})();
