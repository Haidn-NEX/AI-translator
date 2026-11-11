export const DEFAULT_PORT = 47863;
export const DEFAULT_HOST = "127.0.0.1";
export const NATIVE_HOST_NAME = "com.local_translator.bridge";
export const FIXED_EXTENSION_ID = "cgbbacpgahelimaelidkgmphgcbiclfd";

export const DEFAULT_SETTINGS = {
  port: DEFAULT_PORT,
  token: "",
  sourceLang: "en",
  translationMode: "ollama",
  onlyVietnamese: false,
  saveTranscript: true,
  showHud: true,
  fontSize: 26,
  bgOpacity: 0.62,
  overlayPosition: "bottom",
  glossary: "",
  summaryMode: "interval",
  summaryIntervalSec: 30,
  summaryPauseSec: 2,
  summaryMinNewSentences: 3,
  showSummary: true
};

export async function getSettings() {
  try {
    const stored = await chrome.storage.sync.get(DEFAULT_SETTINGS);
    return { ...DEFAULT_SETTINGS, ...stored };
  } catch (_) {
    const stored = await chrome.storage.local.get(DEFAULT_SETTINGS);
    return { ...DEFAULT_SETTINGS, ...stored };
  }
}

export async function saveSettings(partial) {
  try {
    await chrome.storage.sync.set(partial);
  } catch (_) {
    await chrome.storage.local.set(partial);
  }
}

export async function getSessionState() {
  const defaults = {
    activeTabId: null,
    activeTabTitle: "",
    activeTabUrl: "",
    sessionId: null,
    startedAt: null,
    backendState: "idle", // "idle" | "starting" | "translating" | "error"
    lastStatusMessage: "",
    overlayHidden: false
  };
  const data = await chrome.storage.session.get(defaults);
  return { ...defaults, ...data };
}

export async function setSessionState(partial) {
  await chrome.storage.session.set(partial);
}

