/* 指纹浏览器工作台 · Service Worker
   负责：右键菜单「在指纹环境中打开」+ 环境列表缓存刷新 */

const MENU_ROOT = "fpwb-open-in-env";

async function getConfig() {
  const { serverUrl = "http://127.0.0.1:18080", apiKey = "" } =
    await chrome.storage.local.get(["serverUrl", "apiKey"]);
  return { serverUrl, apiKey };
}

async function api(method, path, body) {
  const { serverUrl, apiKey } = await getConfig();
  const resp = await fetch(serverUrl + "/api/v1" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败" }));
  if (json.code !== 0) throw new Error(json.msg || `code=${json.code}`);
  return json.data;
}

async function refreshMenu() {
  await chrome.contextMenus.removeAll();
  let profiles = [];
  try {
    profiles = await api("GET", "/profiles");
  } catch (e) {
    return; // 未连接/不可达时静默
  }
  if (!profiles.length) return;
  await chrome.contextMenus.create({
    id: MENU_ROOT,
    title: "在指纹环境中打开",
    contexts: ["link", "page"],
  });
  for (const p of profiles.slice(0, 10)) {
    await chrome.contextMenus.create({
      id: `open-env:${p.id}`,
      parentId: MENU_ROOT,
      title: `${p.name}（${p.kernel}）`,
      contexts: ["link", "page"],
    });
  }
}

chrome.runtime.onInstalled.addListener(() => {
  refreshMenu();
  chrome.alarms.create("fpwb-refresh-menu", { periodInMinutes: 10 });
});

chrome.runtime.onStartup.addListener(refreshMenu);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "fpwb-refresh-menu") refreshMenu();
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "refresh-menu") refreshMenu();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!info.menuItemId.startsWith("open-env:")) return;
  const profileId = info.menuItemId.slice("open-env:".length);
  const targetUrl = info.linkUrl || info.pageUrl || tab?.url;
  try {
    await api("POST", "/browser/start", {
      profile_id: profileId,
      ...(targetUrl ? { start_url: targetUrl } : {}),
    });
  } catch (e) {
    // 已在运行时导航（仅 camoufox 支持服务端导航）
    try {
      await api("POST", `/browser/${profileId}/navigate`, { url: targetUrl });
    } catch (e2) {
      console.warn("启动环境失败:", e.message, e2.message);
    }
  }
});
