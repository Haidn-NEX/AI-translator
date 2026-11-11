import {
  DEFAULT_HOST,
  DEFAULT_PORT,
  NATIVE_HOST_NAME,
  getSettings,
  saveSettings,
  getSessionState,
  setSessionState
} from "./shared/protocol.js";

async function updateBadge(state, detail = "") {
  try {
    if (state === "starting") {
      await chrome.action.setBadgeText({ text: "…" });
      await chrome.action.setBadgeBackgroundColor({ color: "#64748b" });
      await chrome.action.setTitle({
        title: detail || "⏳ Đang kết nối / nạp model dịch..."
      });
    } else if (state === "translating") {
      await chrome.action.setBadgeText({ text: "VI" });
      await chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
      await chrome.action.setTitle({
        title: detail || "🟢 Đang dịch phụ đề tiếng Việt (Alt+Shift+T để tắt)"
      });
    } else if (state === "error") {
      await chrome.action.setBadgeText({ text: "!" });
      await chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
      await chrome.action.setTitle({
        title: detail || "🔴 Lỗi kết nối máy dịch"
      });
    } else {
      await chrome.action.setBadgeText({ text: "" });
      await chrome.action.setTitle({
        title: "Local AI Live Translator — Nhấn Alt+Shift+T để dịch video"
      });
    }
  } catch (_) {}
}

async function ensureOffscreenDocument() {
  if (chrome.offscreen && chrome.offscreen.hasDocument) {
    const exists = await chrome.offscreen.hasDocument();
    if (exists) return;
  }
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Thu âm thanh tab bằng tabCapture và stream PCM qua WebSocket tới máy dịch AI cục bộ"
    });
  } catch (err) {
    if (!String(err).includes("Only a single offscreen")) {
      throw err;
    }
  }
}

async function closeOffscreenDocumentSafe() {
  try {
    if (chrome.offscreen && chrome.offscreen.hasDocument) {
      const exists = await chrome.offscreen.hasDocument();
      if (!exists) return;
    }
    await chrome.offscreen.closeDocument();
  } catch (_) {}
}

function isYoutubeTabUrl(urlStr) {
  if (!urlStr) return false;
  try {
    const host = new URL(urlStr).hostname.toLowerCase();
    return host === "youtube.com" || host.endsWith(".youtube.com");
  } catch (_) {
    return false;
  }
}

async function ensureYoutubeHookInjected(tabId, tabUrl) {
  if (!tabId || !isYoutubeTabUrl(tabUrl)) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["yt_caption_hook.js"],
      world: "MAIN"
    });
  } catch (_) {}
}

async function sendToTabSafe(tabId, message, autoInject = false, tabUrl = "") {
  if (!tabId) return false;
  if (autoInject && tabUrl) {
    await ensureYoutubeHookInjected(tabId, tabUrl);
  }
  try {
    await chrome.tabs.sendMessage(tabId, message);
    return true;
  } catch (_) {
    if (!autoInject) return false;
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["content_script.js"]
      });
      await chrome.tabs.sendMessage(tabId, message);
      return true;
    } catch (_) {
      return false;
    }
  }
}

async function checkBackendHealth(port = DEFAULT_PORT, timeoutMs = 800) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(`http://${DEFAULT_HOST}:${port}/health`, {
      method: "GET",
      signal: controller.signal,
      cache: "no-store"
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    if (data && data.ok && data.app === "local-live-translator") {
      return data;
    }
    return null;
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function callNativeHost(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, payload, (response) => {
        if (chrome.runtime.lastError) {
          resolve({
            ok: false,
            error: chrome.runtime.lastError.message || "native_host_error"
          });
        } else {
          resolve(response || { ok: false, error: "empty_response" });
        }
      });
    } catch (err) {
      resolve({ ok: false, error: String(err) });
    }
  });
}

async function stopActiveSession(reason = "client", notifyTab = true) {
  const session = await getSessionState();
  const prevTabId = session.activeTabId;

  try {
    await chrome.runtime.sendMessage({ target: "offscreen", cmd: "capture:stop" });
  } catch (_) {}
  await closeOffscreenDocumentSafe();

  if (notifyTab && prevTabId) {
    await sendToTabSafe(prevTabId, { cmd: "overlay:hide", reason }, false);
  }

  await setSessionState({
    activeTabId: null,
    activeTabTitle: "",
    activeTabUrl: "",
    sessionId: null,
    startedAt: null,
    backendState: "idle",
    lastStatusMessage: "",
    overlayHidden: false
  });
  await updateBadge("idle");
}

async function startSessionOnTab(tab) {
  if (!tab || !tab.id) {
    return { ok: false, error: "Không xác định được tab hiện tại." };
  }

  const prev = await getSessionState();
  if (prev.activeTabId && prev.activeTabId !== tab.id) {
    await stopActiveSession("replaced", true);
  }

  const settings = await getSettings();
  const sessionId = `tab-${tab.id}-${Date.now().toString(36)}`;

  // BẮT BUỘC gọi getMediaStreamId NGAY LẬP TỨC khi còn user gesture (streamId hết hạn sau vài giây)
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (err) {
    const msg = `❌ Không lấy được luồng âm thanh tab (${err.message || err}). Hãy thử trên tab web có video.`;
    await updateBadge("error", msg);
    return { ok: false, error: msg };
  }

  await setSessionState({
    activeTabId: tab.id,
    activeTabTitle: tab.title || "Video trên Chrome",
    activeTabUrl: tab.url || "",
    sessionId,
    startedAt: Date.now(),
    backendState: "starting",
    lastStatusMessage: "⏳ Đang kết nối máy dịch...",
    overlayHidden: false
  });
  await updateBadge("starting", `⏳ Đang khởi động dịch: ${tab.title || ""}`);

  await ensureOffscreenDocument();

  const captureResp = await chrome.runtime.sendMessage({
    target: "offscreen",
    cmd: "capture:start",
    streamId,
    tabId: tab.id,
    sessionId,
    tabTitle: tab.title || "Video trên Chrome",
    tabUrl: tab.url || "",
    settings,
    connectImmediately: false
  });

  if (!captureResp || !captureResp.ok) {
    const errMsg = (captureResp && captureResp.error) || "Không thể khởi tạo thu âm tab.";
    await stopActiveSession("error", false);
    await updateBadge("error", errMsg);
    return { ok: false, error: errMsg };
  }

  await sendToTabSafe(
    tab.id,
    {
      cmd: "overlay:show",
      settings,
      status: {
        level: "loading",
        message: "⏳ Đang kết nối máy dịch..."
      }
    },
    true,
    tab.url || ""
  );

  // Kiểm tra nhanh xem backend đã chạy sẵn chưa
  let port = settings.port || DEFAULT_PORT;
  let token = settings.token || "";
  let health = await checkBackendHealth(port, 800);

  if (!health) {
    // Thử tự đánh thức backend qua Native Messaging Host (P3)
    const nativeRes = await callNativeHost({ cmd: "ensure_running" });
    if (nativeRes && nativeRes.ok) {
      if (nativeRes.port) port = nativeRes.port;
      if (nativeRes.token) {
        token = nativeRes.token;
        await saveSettings({ token, port });
      }
      await sendToTabSafe(tab.id, {
        cmd: "overlay:event",
        event: {
          type: "status",
          level: "loading",
          message: "⏳ Đang tự khởi động máy dịch cục bộ..."
        }
      });

      const deadline = Date.now() + 90000;
      while (Date.now() < deadline) {
        const curSession = await getSessionState();
        if (curSession.sessionId !== sessionId) {
          return { ok: false, error: "cancelled" };
        }
        health = await checkBackendHealth(port, 1000);
        if (health) break;
        await new Promise((r) => setTimeout(r, 1000));
      }
    }

    if (!health) {
      const noHostMsg =
        "❌ Chưa chạy được máy dịch. Hãy chạy start_bridge.bat hoặc cài Native Host (xem README).";
      await setSessionState({
        backendState: "error",
        lastStatusMessage: noHostMsg
      });
      await updateBadge("error", noHostMsg);
      await sendToTabSafe(tab.id, {
        cmd: "overlay:event",
        event: {
          type: "status",
          level: "error",
          message: noHostMsg
        }
      });
      // Vẫn kích hoạt kết nối WS ở offscreen để nếu người dùng bật start_bridge.bat ngay sau đó thì tự bắt kết nối luôn
      await chrome.runtime.sendMessage({
        target: "offscreen",
        cmd: "capture:connect_ws",
        port,
        token
      }).catch(() => {});
      return { ok: false, error: noHostMsg };
    }
  }

  await chrome.runtime.sendMessage({
    target: "offscreen",
    cmd: "capture:connect_ws",
    port,
    token
  });

  return { ok: true, sessionId };
}

async function toggleSessionForTab(tab) {
  if (!tab || !tab.id) {
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    tab = activeTab;
  }
  if (!tab || !tab.id) {
    return { ok: false, error: "Không tìm thấy tab đang mở." };
  }

  const session = await getSessionState();
  if (session.activeTabId === tab.id && session.backendState !== "idle") {
    await stopActiveSession("client", true);
    return { ok: true, active: false };
  }

  const res = await startSessionOnTab(tab);
  return { ...res, active: Boolean(res.ok) };
}

chrome.commands.onCommand.addListener(async (command) => {
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!activeTab || !activeTab.id) return;

  if (command === "toggle-translate") {
    await toggleSessionForTab(activeTab);
  } else if (command === "toggle-source-line") {
    const settings = await getSettings();
    const onlyVietnamese = !settings.onlyVietnamese;
    await saveSettings({ onlyVietnamese });
    const session = await getSessionState();
    const targetTabId = session.activeTabId || activeTab.id;
    await sendToTabSafe(targetTabId, {
      cmd: "overlay:config",
      settings: { ...settings, onlyVietnamese }
    });
  } else if (command === "toggle-overlay") {
    const session = await getSessionState();
    if (!session.activeTabId) return;
    const overlayHidden = !session.overlayHidden;
    await setSessionState({ overlayHidden });
    await sendToTabSafe(session.activeTabId, {
      cmd: "overlay:visibility",
      hidden: overlayHidden
    });
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const session = await getSessionState();
  if (session.activeTabId === tabId) {
    await stopActiveSession("tab_closed", false);
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  const session = await getSessionState();
  if (session.activeTabId !== tabId || session.backendState === "idle") return;

  const settings = await getSettings();
  await setSessionState({
    activeTabTitle: tab.title || session.activeTabTitle,
    activeTabUrl: tab.url || session.activeTabUrl
  });
  await sendToTabSafe(
    tabId,
    {
      cmd: "overlay:show",
      settings,
      hidden: session.overlayHidden,
      status: {
        level: session.backendState === "translating" ? "ready" : "loading",
        message: session.lastStatusMessage || "🟢 Đang dịch âm thanh tab..."
      }
    },
    true,
    tab.url || session.activeTabUrl || ""
  );
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;

  if (msg.from === "content_script") {
    (async () => {
      const session = await getSessionState();
      const senderTabId = sender && sender.tab ? sender.tab.id : null;
      if (!senderTabId || senderTabId !== session.activeTabId || session.backendState === "idle") {
        sendResponse({ ok: false, error: "inactive_tab" });
        return;
      }

      if (msg.cmd === "caption:load" || msg.cmd === "caption:unavailable") {
        const patch = {};
        if (msg.tabTitle) patch.activeTabTitle = msg.tabTitle;
        if (msg.tabUrl) patch.activeTabUrl = msg.tabUrl;
        if (Object.keys(patch).length > 0) {
          await setSessionState(patch);
        }
      }

      if (
        msg.cmd === "caption:load" ||
        msg.cmd === "caption:unavailable" ||
        msg.cmd === "caption:playhead"
      ) {
        await chrome.runtime
          .sendMessage({
            target: "offscreen",
            ...msg
          })
          .catch(() => {});
        sendResponse({ ok: true });
        return;
      }

      sendResponse({ ok: false, error: "unsupported_cmd" });
    })();
    return true;
  }

  if (msg.from === "offscreen") {
    (async () => {
      const session = await getSessionState();
      if (msg.type === "token:discovered" && msg.token) {
        await saveSettings({ token: msg.token });
        return;
      }

      if (msg.sessionId && session.sessionId && msg.sessionId !== session.sessionId) {
        return;
      }

      if (msg.type === "capture:track_ended") {
        await stopActiveSession("track_ended", true);
        return;
      }

      if (msg.type === "capture:error") {
        await setSessionState({
          backendState: "error",
          lastStatusMessage: msg.message || "Lỗi thu âm tab"
        });
        await updateBadge("error", msg.message);
        if (session.activeTabId) {
          await sendToTabSafe(session.activeTabId, {
            cmd: "overlay:event",
            event: { type: "status", level: "error", message: msg.message }
          });
        }
        return;
      }

      if (msg.type === "ws:event" && msg.event) {
        const evt = msg.event;
        if (evt.type === "status") {
          let nextState = session.backendState;
          if (evt.level === "ready") nextState = "translating";
          else if (evt.level === "loading") nextState = "starting";
          else if (evt.level === "error") nextState = "error";

          await setSessionState({
            backendState: nextState,
            lastStatusMessage: evt.message || ""
          });
          await updateBadge(nextState, evt.message);
        } else if (
          evt.type === "result" ||
          evt.type === "source" ||
          evt.type === "summary" ||
          evt.type === "caption_track_ready" ||
          evt.type === "caption_cues"
        ) {
          if (session.backendState !== "translating") {
            await setSessionState({ backendState: "translating" });
            await updateBadge("translating");
          }
        } else if (evt.type === "mode" && evt.translation_mode) {
          await saveSettings({ translationMode: evt.translation_mode });
        } else if (evt.type === "stopped") {
          if (evt.reason === "desktop_takeover") {
            if (session.activeTabId) {
              await sendToTabSafe(session.activeTabId, {
                cmd: "overlay:event",
                event: {
                  type: "status",
                  level: "warn",
                  message: "🖥️ Đã chuyển sang Chiều Nghe trên ứng dụng Desktop."
                }
              });
            }
          }
          await stopActiveSession(evt.reason || "stopped", evt.reason !== "desktop_takeover");
          return;
        }

        if (session.activeTabId) {
          await sendToTabSafe(session.activeTabId, {
            cmd: "overlay:event",
            event: evt
          });
        }
      }
    })();
    sendResponse({ ok: true });
    return false;
  }

  if (msg.cmd === "session:status") {
    (async () => {
      const settings = await getSettings();
      const session = await getSessionState();
      const health = await checkBackendHealth(settings.port || DEFAULT_PORT, 600);
      sendResponse({
        ok: true,
        session,
        settings,
        health
      });
    })();
    return true;
  }

  if (msg.cmd === "session:toggle") {
    (async () => {
      let targetTab = msg.tab;
      if (!targetTab || !targetTab.id) {
        const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
        targetTab = activeTab;
      }
      const result = await toggleSessionForTab(targetTab);
      const session = await getSessionState();
      sendResponse({ ...result, session });
    })();
    return true;
  }

  if (msg.cmd === "config:update") {
    (async () => {
      const partial = msg.config || {};
      await saveSettings(partial);
      const settings = await getSettings();
      const session = await getSessionState();

      if (session.activeTabId && session.backendState !== "idle") {
        await chrome.runtime.sendMessage({
          target: "offscreen",
          cmd: "capture:config",
          config: settings
        }).catch(() => {});

        await sendToTabSafe(session.activeTabId, {
          cmd: "overlay:config",
          settings
        });
      }
      sendResponse({ ok: true, settings });
    })();
    return true;
  }

  if (msg.cmd === "summary:now") {
    (async () => {
      const session = await getSessionState();
      if (session.activeTabId && session.backendState !== "idle") {
        await chrome.runtime.sendMessage({
          target: "offscreen",
          cmd: "summary:now"
        }).catch(() => {});
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: "Phiên dịch chưa bật." });
      }
    })();
    return true;
  }

  if (msg.cmd === "native:get_token") {
    (async () => {
      const settings = await getSettings();
      const port = msg.port || settings.port || DEFAULT_PORT;
      const nativeRes = await callNativeHost({ cmd: "get_token" });
      if (nativeRes && nativeRes.ok && nativeRes.token) {
        await saveSettings({ token: nativeRes.token });
        sendResponse({ ok: true, token: nativeRes.token, source: "native_host" });
        return;
      }
      try {
        const resp = await fetch(`http://${DEFAULT_HOST}:${port}/token`, {
          method: "POST",
          cache: "no-store"
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.token) {
            await saveSettings({ token: data.token });
            sendResponse({ ok: true, token: data.token, source: "bridge_http" });
            return;
          }
        }
      } catch (_) {}
      sendResponse({ ok: false, error: nativeRes.error || "Không lấy được token tự động." });
    })();
    return true;
  }

  return false;
});

