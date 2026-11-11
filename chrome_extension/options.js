import { DEFAULT_HOST, DEFAULT_PORT, getSettings } from "./shared/protocol.js";

const portInputEl = document.getElementById("portInput");
const tokenInputEl = document.getElementById("tokenInput");
const btnCheckHealthEl = document.getElementById("btnCheckHealth");
const btnFetchTokenEl = document.getElementById("btnFetchToken");
const connStatusEl = document.getElementById("connStatus");
const glossaryInputEl = document.getElementById("glossaryInput");
const overlayPositionEl = document.getElementById("overlayPosition");
const bgOpacityEl = document.getElementById("bgOpacity");
const bgOpacityValEl = document.getElementById("bgOpacityVal");
const btnResetPositionsEl = document.getElementById("btnResetPositions");
const resetStatusEl = document.getElementById("resetStatus");
const btnSaveAllEl = document.getElementById("btnSaveAll");
const saveBannerEl = document.getElementById("saveBanner");
const extIdLabelEl = document.getElementById("extIdLabel");

function showStatus(boxEl, text, isError = false) {
  boxEl.style.display = "block";
  boxEl.style.color = isError ? "#f87171" : "#4ade80";
  boxEl.textContent = text;
}

async function initOptions() {
  extIdLabelEl.textContent = chrome.runtime.id;
  const s = await getSettings();
  portInputEl.value = s.port || DEFAULT_PORT;
  tokenInputEl.value = s.token || "";
  glossaryInputEl.value = s.glossary || "";
  overlayPositionEl.value = s.overlayPosition || "bottom";
  const opacity = Number(s.bgOpacity ?? 0.62);
  bgOpacityEl.value = opacity;
  bgOpacityValEl.textContent = `${Math.round(opacity * 100)}%`;
}

bgOpacityEl.addEventListener("input", () => {
  bgOpacityValEl.textContent = `${Math.round(Number(bgOpacityEl.value) * 100)}%`;
});

btnCheckHealthEl.addEventListener("click", async () => {
  const port = Number(portInputEl.value) || DEFAULT_PORT;
  try {
    const resp = await fetch(`http://${DEFAULT_HOST}:${port}/health`, { cache: "no-store" });
    if (!resp.ok) {
      showStatus(connStatusEl, `❌ Máy chủ trả về mã lỗi HTTP ${resp.status}.`, true);
      return;
    }
    const data = await resp.json();
    showStatus(
      connStatusEl,
      `✅ Kết nối thành công! Chế độ: ${data.mode} | Thiết bị: ${data.device} | Dịch: ${data.translation_mode} | Model đã nạp: ${data.models_loaded ? "Có" : "Chưa"}`,
      false
    );
  } catch (err) {
    showStatus(
      connStatusEl,
      `❌ Không kết nối được tới http://${DEFAULT_HOST}:${port}/health. Hãy chạy start_bridge.bat hoặc mở ứng dụng Desktop.`,
      true
    );
  }
});

btnFetchTokenEl.addEventListener("click", async () => {
  const port = Number(portInputEl.value) || DEFAULT_PORT;
  const res = await chrome.runtime.sendMessage({ cmd: "native:get_token", port });
  if (res && res.ok && res.token) {
    tokenInputEl.value = res.token;
    showStatus(connStatusEl, `✅ Đã tự động lấy Token thành công (${res.source}).`, false);
  } else {
    showStatus(
      connStatusEl,
      `⚠️ Chưa lấy được Token tự động. Hãy bật start_bridge.bat trước rồi thử lại, hoặc dán từ bridge_token.txt.`,
      true
    );
  }
});

btnResetPositionsEl.addEventListener("click", async () => {
  const allLocal = await chrome.storage.local.get(null);
  const keysToRemove = Object.keys(allLocal).filter((k) => k.startsWith("dragOffset:"));
  if (keysToRemove.length > 0) {
    await chrome.storage.local.remove(keysToRemove);
  }
  resetStatusEl.textContent = `✅ Đã đặt lại vị trí phụ đề trên ${keysToRemove.length} trang web.`;
});

btnSaveAllEl.addEventListener("click", async () => {
  const updated = {
    port: Number(portInputEl.value) || DEFAULT_PORT,
    token: tokenInputEl.value.trim(),
    glossary: glossaryInputEl.value.trim(),
    overlayPosition: overlayPositionEl.value,
    bgOpacity: Number(bgOpacityEl.value)
  };
  await chrome.runtime.sendMessage({
    cmd: "config:update",
    config: updated
  });
  showStatus(saveBannerEl, "✅ Đã lưu và áp dụng toàn bộ thiết lập thành công!", false);
});

initOptions();

