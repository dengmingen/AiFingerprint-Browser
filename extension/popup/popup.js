/* 指纹浏览器工作台 · Popup 逻辑 */
"use strict";

const $ = (s) => document.querySelector(s);
let config = { serverUrl: "http://127.0.0.1:18080", apiKey: "" };
let profiles = [];

/* ---------------- 基础 ---------------- */

function toast(msg, type = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2600);
}

async function loadConfig() {
  const stored = await chrome.storage.local.get(["serverUrl", "apiKey"]);
  config.serverUrl = stored.serverUrl || "http://127.0.0.1:18080";
  config.apiKey = stored.apiKey || "";
}

async function saveConfig() {
  await chrome.storage.local.set(config);
  chrome.runtime.sendMessage({ type: "refresh-menu" }).catch(() => {});
}

async function api(method, path, body) {
  const resp = await fetch(config.serverUrl + "/api/v1" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(config.apiKey ? { "X-API-Key": config.apiKey } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败" }));
  if (json.code !== 0) {
    const err = new Error(json.msg || `code=${json.code}`);
    err.status = resp.status;
    err.code = json.code;
    throw err;
  }
  return json.data;
}

/* ---------------- 一键配置 ---------------- */

function setupMsg(msg, cls = "") {
  $("#setupMsg").textContent = msg;
  $("#setupMsg").className = `msg ${cls}`;
}

async function quickConnect() {
  config.serverUrl = $("#serverUrl").value.trim().replace(/\/+$/, "");
  setupMsg("连接中…");
  try {
    const status = await api("GET", "/status");
    await saveConfig();
    setupMsg(`已连接 ${status.app} v${status.version} ✔`, "ok");
    await enterMain(status);
  } catch (e) {
    if (e.code === 40100) {
      setupMsg("工作台已开启认证：请使用下方配对码或高级设置中的 API Key", "err");
      $("#pairCode").focus();
    } else {
      setupMsg(`连接失败：${e.message}（确认工作台已启动）`, "err");
    }
  }
}

async function pairConnect() {
  const code = $("#pairCode").value.trim();
  if (code.length !== 6) { setupMsg("请输入 6 位配对码", "err"); return; }
  config.serverUrl = $("#serverUrl").value.trim().replace(/\/+$/, "");
  setupMsg("配对中…");
  try {
    const r = await api("POST", "/pair/exchange", { pairing_code: code });
    config.apiKey = r.api_key;
    await saveConfig();
    const status = await api("GET", "/status");
    setupMsg("配对成功 ✔", "ok");
    await enterMain(status);
  } catch (e) {
    setupMsg(`配对失败：${e.message}`, "err");
  }
}

async function saveKeyConnect() {
  config.serverUrl = $("#serverUrl").value.trim().replace(/\/+$/, "");
  config.apiKey = $("#apiKey").value.trim();
  setupMsg("测试中…");
  try {
    const status = await api("GET", "/status");
    await saveConfig();
    setupMsg("连接成功 ✔", "ok");
    await enterMain(status);
  } catch (e) {
    setupMsg(`失败：${e.message}`, "err");
  }
}

/* ---------------- 主面板 ---------------- */

async function enterMain(status) {
  $("#setup").hidden = true;
  $("#main").hidden = false;
  $("#connState").textContent = "已连接";
  $("#connState").className = "ok";
  renderStatus(status);
  await Promise.all([loadProfiles(), loadCurrentTab()]);
}

async function refreshAll() {
  try {
    const status = await api("GET", "/status");
    renderStatus(status);
    await loadProfiles();
    $("#lastSync").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (e.code === 40100) {
      await disconnect("认证已开启且密钥失效，请重新配对");
    } else {
      toast(`刷新失败：${e.message}`, "err");
    }
  }
}

function renderStatus(s) {
  $("#version").textContent = `v${s.version}`;
  $("#runningCount").textContent = `${s.running_count} 运行中`;
  $("#identity").textContent = s.security.api_key_enabled ? "认证开启" : "单用户模式";
  const names = { camoufox: "Camoufox", "fp-chromium": "fp-chromium", chromium: "Chromium" };
  $("#kernelBadges").innerHTML = Object.entries(s.kernels)
    .map(([k, v]) => `<span class="badge ${v.available ? "ok" : ""}">${names[k] || k} ${v.available ? "✔" : "未装"}</span>`)
    .join("") + (s.sync.server_enabled ? '<span class="badge ok">同步服务器 ✔</span>' : "");
}

async function loadProfiles() {
  profiles = await api("GET", "/profiles");
  const list = $("#envList");
  if (!profiles.length) {
    list.innerHTML = '<div class="empty">暂无环境，请到工作台创建</div>';
  } else {
    list.innerHTML = profiles.map((p) => `
      <div class="env-row" data-id="${p.id}">
        <span class="dot ${p.running ? "on" : ""}"></span>
        <div class="env-name">
          <b>${esc(p.name)}</b>
          <span>${esc(p.group_name)} · <span class="tag ${p.kernel}">${p.kernel}</span>
          ${p.fingerprint_summary?.health ? `<span class="tag">${p.fingerprint_summary.health.score}分</span>` : ""}</span>
        </div>
        <button data-act="toggle" class="small ${p.running ? "" : "primary"}">${p.running ? "停止" : "启动"}</button>
      </div>`).join("");
  }
  $("#envSelect").innerHTML = profiles.map((p) =>
    `<option value="${p.id}">${esc(p.name)}（${p.kernel}）</option>`).join("")
    || '<option value="">（无环境）</option>';
}

async function loadCurrentTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    $("#currentUrl").textContent = tab?.url || "（不可用）";
    $("#currentUrl").title = tab?.url || "";
  } catch (e) {
    $("#currentUrl").textContent = "（无法读取当前标签页）";
  }
}

async function toggleEnv(id, running) {
  const btn = document.querySelector(`.env-row[data-id="${id}"] button`);
  if (btn) { btn.disabled = true; btn.textContent = running ? "停止中…" : "启动中…"; }
  try {
    if (running) {
      await api("POST", "/browser/stop", { profile_id: id });
      toast("环境已停止");
    } else {
      toast("启动中（首次约 30~60 秒）…");
      await api("POST", "/browser/start", { profile_id: id });
      toast("环境已启动");
    }
  } catch (e) {
    toast(e.message, "err");
  }
  await loadProfiles();
}

async function openCurrentInEnv() {
  const id = $("#envSelect").value;
  if (!id) { toast("没有可用环境", "err"); return; }
  let url = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    url = tab?.url;
  } catch (e) { /* ignore */ }
  if (!url || !/^https?:/.test(url)) { toast("当前页面不是可打开的网址", "err"); return; }
  const p = profiles.find((x) => x.id === id);
  try {
    if (p?.running) {
      await api("POST", `/browser/${id}/navigate`, { url });  // camoufox 直接导航
      toast(`已在「${p.name}」中打开`);
    } else {
      toast("启动中…");
      await api("POST", "/browser/start", { profile_id: id, start_url: url });
      toast(`已在「${p.name}」中打开`);
    }
    await loadProfiles();
  } catch (e) {
    toast(e.code === 409 ? `「${p.name}」已在运行且不支持服务端导航，请在环境中手动打开` : e.message, "err");
  }
}

async function disconnect(reason) {
  config.apiKey = "";
  await chrome.storage.local.remove("apiKey");
  $("#main").hidden = true;
  $("#setup").hidden = false;
  $("#connState").textContent = "未连接";
  $("#connState").className = "bad";
  if (reason) setupMsg(reason, "err");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 事件 ---------------- */

$("#btnQuickConnect").addEventListener("click", quickConnect);
$("#btnPair").addEventListener("click", pairConnect);
$("#btnSaveKey").addEventListener("click", saveKeyConnect);
$("#btnOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("#btnRefresh").addEventListener("click", refreshAll);
$("#btnOpenHere").addEventListener("click", openCurrentInEnv);
$("#btnDisconnect").addEventListener("click", () => disconnect());
$("#envList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act='toggle']");
  if (!btn || btn.disabled) return;
  const row = btn.closest(".env-row");
  const p = profiles.find((x) => x.id === row.dataset.id);
  toggleEnv(p.id, p.running);
});
$("#pairCode").addEventListener("keydown", (e) => { if (e.key === "Enter") pairConnect(); });

/* ---------------- 启动 ---------------- */

(async function init() {
  await loadConfig();
  $("#serverUrl").value = config.serverUrl;
  try {
    const status = await api("GET", "/status");
    await enterMain(status);
    setInterval(refreshAll, 5000);
  } catch (e) {
    $("#connState").textContent = "未连接";
    $("#connState").className = "bad";
    if (e.code === 40100) setupMsg("工作台已开启认证：请用配对码或 API Key 连接", "err");
  }
})();
