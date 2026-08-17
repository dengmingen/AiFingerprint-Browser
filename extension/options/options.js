/* 插件设置页逻辑 */
"use strict";

const $ = (s) => document.querySelector(s);

async function getConfig() {
  const { serverUrl = "http://127.0.0.1:18080", apiKey = "" } =
    await chrome.storage.local.get(["serverUrl", "apiKey"]);
  $("#serverUrl").value = serverUrl;
  $("#apiKey").value = apiKey;
}

$("#btnSave").addEventListener("click", async () => {
  const serverUrl = $("#serverUrl").value.trim().replace(/\/+$/, "");
  const apiKey = $("#apiKey").value.trim();
  $("#status").textContent = "测试中…";
  $("#status").className = "status";
  try {
    const resp = await fetch(serverUrl + "/api/v1/status", {
      headers: apiKey ? { "X-API-Key": apiKey } : {},
    });
    const json = await resp.json();
    if (json.code !== 0) throw new Error(json.msg || `code=${json.code}`);
    await chrome.storage.local.set({ serverUrl, apiKey });
    chrome.runtime.sendMessage({ type: "refresh-menu" }).catch(() => {});
    $("#status").textContent = `连接成功：${json.data.app} v${json.data.version} ✔`;
    $("#status").className = "status ok";
  } catch (e) {
    $("#status").textContent = `连接失败：${e.message}`;
    $("#status").className = "status err";
  }
});

$("#btnClear").addEventListener("click", async () => {
  await chrome.storage.local.clear();
  await getConfig();
  $("#status").textContent = "已清除配置";
  $("#status").className = "status";
});

getConfig();
