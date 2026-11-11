const statusLineEl = document.getElementById("statusLine");
const deviceBadgeEl = document.getElementById("deviceBadge");
const btnToggleEl = document.getElementById("btnToggle");
const sourceLangEl = document.getElementById("sourceLang");
const translationModeEl = document.getElementById("translationMode");
const onlyVietnameseEl = document.getElementById("onlyVietnamese");
const saveTranscriptEl = document.getElementById("saveTranscript");
const showHudEl = document.getElementById("showHud");
const fontSizeEl = document.getElementById("fontSize");
const fontSizeValEl = document.getElementById("fontSizeVal");
const summaryModeEl = document.getElementById("summaryMode");
const summaryParamLabelEl = document.getElementById("summaryParamLabel");
const summaryParamValEl = document.getElementById("summaryParamVal");
const summaryMinSentencesEl = document.getElementById("summaryMinSentences");
const showSummaryEl = document.getElementById("showSummary");
const btnSummaryNowEl = document.getElementById("btnSummaryNow");
const openOptionsEl = document.getElementById("openOptions");

let currentTab = null;
let cachedSettings = {
  summaryMode: "interval",
  summaryIntervalSec: 30,
  summaryPauseSec: 2,
  summaryMinNewSentences: 3,
  showSummary: true
};

function syncSummaryInputs(settings) {
  cachedSettings = { ...cachedSettings, ...(settings || {}) };
  const mode = cachedSettings.summaryMode || "interval";
  summaryModeEl.value = mode;
  summaryMinSentencesEl.value = Number(cachedSettings.summaryMinNewSentences ?? 3);
  showSummaryEl.checked = cachedSettings.showSummary !== false;

  if (mode === "pause") {
    summaryParamLabelEl.textContent = "Im lặng (giây):";
    summaryParamValEl.min = "1";
    summaryParamValEl.max = "5";
    summaryParamValEl.step = "0.5";
    summaryParamValEl.value = Number(cachedSettings.summaryPauseSec ?? 2);
    summaryParamValEl.disabled = false;
    summaryMinSentencesEl.disabled = false;
  } else if (mode === "interval") {
    summaryParamLabelEl.textContent = "Mỗi (giây):";
    summaryParamValEl.min = "15";
    summaryParamValEl.max = "90";
    summaryParamValEl.step = "5";
    summaryParamValEl.value = Number(cachedSettings.summaryIntervalSec ?? 30);
    summaryParamValEl.disabled = false;
    summaryMinSentencesEl.disabled = false;
  } else {
    summaryParamLabelEl.textContent = "Mỗi (giây):";
    summaryParamValEl.min = "15";
    summaryParamValEl.max = "90";
    summaryParamValEl.step = "5";
    summaryParamValEl.value = Number(cachedSettings.summaryIntervalSec ?? 30);
    summaryParamValEl.disabled = true;
    summaryMinSentencesEl.disabled = true;
  }
}

function renderState(resp) {
  if (!resp || !resp.ok) return;
  const { session, settings, health } = resp;

  if (settings) {
    sourceLangEl.value = settings.sourceLang || "en";
    translationModeEl.value = settings.translationMode || "ollama";
    onlyVietnameseEl.checked = Boolean(settings.onlyVietnamese);
    saveTranscriptEl.checked = settings.saveTranscript !== false;
    showHudEl.checked = settings.showHud !== false;
    fontSizeEl.value = settings.fontSize || 26;
    fontSizeValEl.textContent = `${fontSizeEl.value} px`;
    syncSummaryInputs(settings);
  }

  if (health && health.device) {
    deviceBadgeEl.textContent = health.device.replace(/\s*\(.*?\)/, "");
    deviceBadgeEl.title = `${health.device} | Chế độ: ${health.mode}`;
    if (health.translation_mode && (!session || session.backendState !== "idle")) {
      translationModeEl.value = health.translation_mode;
    }
  } else {
    deviceBadgeEl.textContent = "Máy dịch: Chưa bật";
  }

  const isThisTabActive =
    currentTab &&
    session &&
    session.activeTabId === currentTab.id &&
    session.backendState !== "idle";

  if (isThisTabActive) {
    btnToggleEl.classList.add("active");
    btnToggleEl.innerHTML = `⏹ Tắt dịch <span class="shortcut-hint">(Alt+Shift+T)</span>`;
    if (session.backendState === "starting") {
      statusLineEl.textContent = session.lastStatusMessage || "⏳ Đang nạp model AI...";
    } else if (session.backendState === "error") {
      statusLineEl.textContent = session.lastStatusMessage || "🔴 Lỗi kết nối máy dịch";
    } else {
      statusLineEl.textContent = `🟢 Đang dịch: ${session.activeTabTitle || currentTab.title || "Tab hiện tại"}`;
    }
  } else {
    btnToggleEl.classList.remove("active");
    btnToggleEl.innerHTML = `▶ Bật dịch tab này <span class="shortcut-hint">(Alt+Shift+T)</span>`;
    if (session && session.activeTabId && session.backendState !== "idle") {
      statusLineEl.textContent = `🟢 Đang dịch ở tab khác: ${session.activeTabTitle || "Tab #" + session.activeTabId}`;
    } else if (health && health.ok) {
      statusLineEl.textContent = `⚪ Đang tắt (Máy dịch sẵn sàng — ${health.translation_mode === "ollama" ? "Ollama" : "NLLB-200"})`;
    } else {
      statusLineEl.textContent = "⚪ Đang tắt (Nhấn Bật dịch hoặc chạy start_bridge.bat)";
    }
  }
}

async function refreshPopup() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  const resp = await chrome.runtime.sendMessage({ cmd: "session:status" });
  renderState(resp);
}

async function pushConfigUpdate(partial) {
  await chrome.runtime.sendMessage({
    cmd: "config:update",
    config: partial
  });
}

btnToggleEl.addEventListener("click", async () => {
  btnToggleEl.disabled = true;
  try {
    await chrome.runtime.sendMessage({
      cmd: "session:toggle",
      tab: currentTab
    });
    await refreshPopup();
  } finally {
    btnToggleEl.disabled = false;
  }
});

sourceLangEl.addEventListener("change", () => {
  pushConfigUpdate({ sourceLang: sourceLangEl.value });
});

translationModeEl.addEventListener("change", () => {
  pushConfigUpdate({ translationMode: translationModeEl.value });
});

onlyVietnameseEl.addEventListener("change", () => {
  pushConfigUpdate({ onlyVietnamese: onlyVietnameseEl.checked });
});

saveTranscriptEl.addEventListener("change", () => {
  pushConfigUpdate({ saveTranscript: saveTranscriptEl.checked });
});

showHudEl.addEventListener("change", () => {
  pushConfigUpdate({ showHud: showHudEl.checked });
});

fontSizeEl.addEventListener("input", () => {
  fontSizeValEl.textContent = `${fontSizeEl.value} px`;
  pushConfigUpdate({ fontSize: Number(fontSizeEl.value) });
});

summaryModeEl.addEventListener("change", () => {
  const summaryMode = summaryModeEl.value;
  syncSummaryInputs({ summaryMode });
  pushConfigUpdate({ summaryMode });
});

summaryParamValEl.addEventListener("change", () => {
  const mode = summaryModeEl.value;
  const raw = Number(summaryParamValEl.value);
  if (mode === "pause") {
    const summaryPauseSec = Math.max(1.0, Math.min(5.0, isNaN(raw) ? 2.0 : raw));
    summaryParamValEl.value = summaryPauseSec;
    cachedSettings.summaryPauseSec = summaryPauseSec;
    pushConfigUpdate({ summaryPauseSec });
  } else {
    const summaryIntervalSec = Math.max(15, Math.min(90, isNaN(raw) ? 30 : raw));
    summaryParamValEl.value = summaryIntervalSec;
    cachedSettings.summaryIntervalSec = summaryIntervalSec;
    pushConfigUpdate({ summaryIntervalSec });
  }
});

summaryMinSentencesEl.addEventListener("change", () => {
  const raw = parseInt(summaryMinSentencesEl.value, 10);
  const summaryMinNewSentences = Math.max(1, Math.min(8, isNaN(raw) ? 3 : raw));
  summaryMinSentencesEl.value = summaryMinNewSentences;
  cachedSettings.summaryMinNewSentences = summaryMinNewSentences;
  pushConfigUpdate({ summaryMinNewSentences });
});

showSummaryEl.addEventListener("change", () => {
  const showSummary = showSummaryEl.checked;
  cachedSettings.showSummary = showSummary;
  pushConfigUpdate({ showSummary });
});

btnSummaryNowEl.addEventListener("click", async () => {
  btnSummaryNowEl.disabled = true;
  try {
    await chrome.runtime.sendMessage({ cmd: "summary:now" });
  } finally {
    setTimeout(() => {
      btnSummaryNowEl.disabled = false;
    }, 400);
  }
});

openOptionsEl.addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

refreshPopup();

