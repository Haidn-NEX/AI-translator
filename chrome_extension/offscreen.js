import { DEFAULT_HOST, DEFAULT_PORT } from "./shared/protocol.js";

let activeState = null;

function isYoutubeVideoUrl(urlStr) {
  if (!urlStr) return false;
  try {
    const u = new URL(urlStr);
    const host = u.hostname.toLowerCase();
    if (host !== "youtube.com" && !host.endsWith(".youtube.com")) {
      return false;
    }
    return (
      u.pathname === "/watch" ||
      u.pathname.startsWith("/shorts/") ||
      u.pathname.startsWith("/live/")
    );
  } catch (_) {
    return false;
  }
}

function sendCaptionLoadOverWs(ws, track, lastPlayhead) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !track || !track.raw) return;
  const playheadMs =
    lastPlayhead && lastPlayhead.videoId === track.videoId
      ? lastPlayhead.playheadMs
      : track.playheadMs || 0;
  const playbackRate =
    lastPlayhead && lastPlayhead.videoId === track.videoId
      ? lastPlayhead.playbackRate
      : track.playbackRate || 1.0;

  ws.send(
    JSON.stringify({
      type: "captions_load",
      video_id: track.videoId || "yt",
      lang: track.lang || "en",
      kind: track.kind || "",
      is_asr: Boolean(track.isAsr),
      fmt: track.fmt || "auto",
      raw: track.raw,
      playhead_ms: Math.round(playheadMs || 0),
      playback_rate: Number(playbackRate || 1.0),
      tab_title: track.tabTitle || "",
      tab_url: track.tabUrl || ""
    })
  );
}

function notifyServiceWorker(payload) {
  chrome.runtime.sendMessage({ from: "offscreen", ...payload }).catch(() => {});
}

async function fetchTokenIfNeeded(port, currentToken) {
  if (currentToken && currentToken.trim().length >= 16) {
    return currentToken.trim();
  }
  try {
    const resp = await fetch(`http://${DEFAULT_HOST}:${port}/token`, {
      method: "POST",
      cache: "no-store"
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data && data.token) {
        return data.token;
      }
    }
  } catch (_) {}
  return currentToken || "";
}

function clearTimers(state) {
  if (!state) return;
  if (state.pingTimer) {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
}

async function cleanupCapture(sendStopFrame = true) {
  const state = activeState;
  activeState = null;
  if (!state) return;

  state.stopping = true;
  clearTimers(state);

  if (state.ws) {
    try {
      if (sendStopFrame && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "stop", session_id: state.sessionId }));
      }
      state.ws.close();
    } catch (_) {}
    state.ws = null;
  }

  if (state.workletNode) {
    try {
      state.workletNode.port.onmessage = null;
      state.workletNode.disconnect();
    } catch (_) {}
    state.workletNode = null;
  }

  if (state.sourceNode) {
    try {
      state.sourceNode.disconnect();
    } catch (_) {}
    state.sourceNode = null;
  }

  if (state.mediaStream) {
    try {
      for (const track of state.mediaStream.getTracks()) {
        track.onended = null;
        track.stop();
      }
    } catch (_) {}
    state.mediaStream = null;
  }

  if (state.audioCtx) {
    try {
      await state.audioCtx.close();
    } catch (_) {}
    state.audioCtx = null;
  }
}

async function connectWebSocket(state) {
  if (!state || state.stopping || activeState !== state) return;
  clearTimers(state);

  const port = state.settings.port || DEFAULT_PORT;
  const token = await fetchTokenIfNeeded(port, state.settings.token);
  if (token && token !== state.settings.token) {
    state.settings.token = token;
    notifyServiceWorker({ type: "token:discovered", token });
  }

  const wsUrl = `ws://${DEFAULT_HOST}:${port}/ws?token=${encodeURIComponent(token || "")}`;
  let ws;
  try {
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
  } catch (err) {
    scheduleReconnect(state, `Không mở được WebSocket: ${err.message || err}`);
    return;
  }

  state.ws = ws;

  ws.onopen = () => {
    if (state.stopping || activeState !== state) return;
    state.reconnectAttempts = 0;

    ws.send(JSON.stringify({
      type: "hello",
      client: "chrome-ext",
      version: "1.0.0",
      summary_mode: state.settings.summaryMode || "interval",
      summary_interval_sec: Number(state.settings.summaryIntervalSec ?? 30),
      summary_pause_sec: Number(state.settings.summaryPauseSec ?? 2),
      summary_min_new_sentences: Number(state.settings.summaryMinNewSentences ?? 3)
    }));

    ws.send(JSON.stringify({
      type: "start",
      session_id: state.sessionId,
      sample_rate: state.sampleRate,
      tab_title: state.tabTitle || "Video trên Chrome",
      tab_url: state.tabUrl || "",
      source_lang: state.settings.sourceLang || "en",
      translation_mode: state.settings.translationMode || "ollama",
      save_transcript: state.settings.saveTranscript !== false,
      glossary: state.settings.glossary || "",
      prefer_caption: Boolean(state.preferCaption || state.activeCaptionTrack),
      summary_mode: state.settings.summaryMode || "interval",
      summary_interval_sec: Number(state.settings.summaryIntervalSec ?? 30),
      summary_pause_sec: Number(state.settings.summaryPauseSec ?? 2),
      summary_min_new_sentences: Number(state.settings.summaryMinNewSentences ?? 3)
    }));

    if (state.activeCaptionTrack && state.activeCaptionTrack.raw) {
      sendCaptionLoadOverWs(ws, state.activeCaptionTrack, state.lastPlayhead);
    } else if (state.captionUnavailableNotice) {
      ws.send(
        JSON.stringify({
          type: "caption_unavailable",
          video_id: state.captionUnavailableNotice.videoId || "",
          reason: state.captionUnavailableNotice.reason || "unavailable",
          tab_title: state.captionUnavailableNotice.tabTitle || state.tabTitle || "",
          tab_url: state.captionUnavailableNotice.tabUrl || state.tabUrl || ""
        })
      );
    }

    notifyServiceWorker({
      type: "ws:open",
      tabId: state.tabId,
      sessionId: state.sessionId,
      hasCaptionTrack: Boolean(state.activeCaptionTrack && state.activeCaptionTrack.raw)
    });

    state.pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
      }
    }, 10000);
  };

  ws.onmessage = (evt) => {
    if (state.stopping || activeState !== state) return;
    if (typeof evt.data !== "string") return;

    let data;
    try {
      data = JSON.parse(evt.data);
    } catch (_) {
      return;
    }

    if (data.type === "pong") {
      return;
    }

    if (data.type === "caption_track_ready") {
      state.pcmPaused = true;
    } else if (data.type === "caption_fallback") {
      state.pcmPaused = false;
      state.activeCaptionTrack = null;
    }

    notifyServiceWorker({
      type: "ws:event",
      tabId: state.tabId,
      sessionId: state.sessionId,
      event: data
    });

    if (data.type === "stopped" && data.reason !== "client") {
      cleanupCapture(false);
    }
  };

  ws.onerror = () => {};

  ws.onclose = (evt) => {
    if (state.stopping || activeState !== state) return;
    clearTimers(state);
    if (evt.code === 1008 || evt.code === 4003) {
      notifyServiceWorker({
        type: "ws:event",
        tabId: state.tabId,
        sessionId: state.sessionId,
        event: {
          type: "error",
          code: "auth_failed",
          message: "❌ Token kết nối máy dịch không hợp lệ. Hãy kiểm tra trong trang Tùy chọn."
        }
      });
      return;
    }
    scheduleReconnect(state);
  };
}

function scheduleReconnect(state) {
  if (!state || state.stopping || activeState !== state) return;
  const attempt = state.reconnectAttempts || 0;
  state.reconnectAttempts = attempt + 1;
  const delayMs = Math.min(5000, 500 * Math.pow(2, Math.min(attempt, 3)));

  notifyServiceWorker({
    type: "ws:event",
    tabId: state.tabId,
    sessionId: state.sessionId,
    event: {
      type: "status",
      level: "warn",
      message: "⚠️ Mất kết nối máy dịch, đang thử kết nối lại..."
    }
  });

  state.reconnectTimer = setTimeout(() => {
    connectWebSocket(state);
  }, delayMs);
}

async function startCapture(msg) {
  await cleanupCapture(true);

  const {
    streamId,
    tabId,
    sessionId,
    tabTitle,
    tabUrl,
    settings,
    connectImmediately = true
  } = msg;

  let mediaStream;
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId
        }
      },
      video: false
    });
  } catch (err) {
    notifyServiceWorker({
      type: "capture:error",
      tabId,
      sessionId,
      message: `❌ Không thể thu âm thanh tab: ${err.message || err}`
    });
    return { ok: false, error: String(err) };
  }

  const audioCtx = new AudioContext({ latencyHint: "interactive" });
  if (audioCtx.state === "suspended") {
    try {
      await audioCtx.resume();
    } catch (_) {}
  }

  const sourceNode = audioCtx.createMediaStreamSource(mediaStream);
  // BẮT BUỘC: Nối lại ra loa để người dùng vẫn nghe thấy tiếng video bình thường
  sourceNode.connect(audioCtx.destination);

  await audioCtx.audioWorklet.addModule("pcm_worklet.js");
  const workletNode = new AudioWorkletNode(audioCtx, "pcm-capture-processor");
  sourceNode.connect(workletNode);

  const preferCaption = Boolean(
    msg.preferCaption !== undefined ? msg.preferCaption : isYoutubeVideoUrl(tabUrl)
  );

  const state = {
    tabId,
    sessionId,
    tabTitle,
    tabUrl,
    settings: { ...settings },
    preferCaption,
    pcmPaused: preferCaption,
    activeCaptionTrack: null,
    captionUnavailableNotice: null,
    lastPlayhead: {
      videoId: "",
      playheadMs: 0,
      playbackRate: 1.0,
      paused: false
    },
    mediaStream,
    audioCtx,
    sourceNode,
    workletNode,
    sampleRate: Math.round(audioCtx.sampleRate || 48000),
    ws: null,
    pingTimer: null,
    reconnectTimer: null,
    reconnectAttempts: 0,
    stopping: false
  };
  activeState = state;

  const [audioTrack] = mediaStream.getAudioTracks();
  if (audioTrack) {
    audioTrack.onended = () => {
      if (!state.stopping && activeState === state) {
        notifyServiceWorker({
          type: "capture:track_ended",
          tabId: state.tabId,
          sessionId: state.sessionId
        });
        cleanupCapture(true);
      }
    };
  }

  workletNode.port.onmessage = (evt) => {
    if (state.stopping || activeState !== state || state.pcmPaused) return;
    const ws = state.ws;
    if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount <= 512 * 1024) {
      ws.send(evt.data);
    }
  };

  if (connectImmediately) {
    connectWebSocket(state);
  }

  return { ok: true, sampleRate: state.sampleRate };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.target !== "offscreen") {
    return false;
  }

  if (msg.cmd === "capture:start") {
    startCapture(msg).then(sendResponse);
    return true;
  }

  if (msg.cmd === "capture:connect_ws") {
    if (activeState) {
      if (msg.port) activeState.settings.port = msg.port;
      if (msg.token) activeState.settings.token = msg.token;
      connectWebSocket(activeState);
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "no_active_capture" });
    }
    return false;
  }

  if (msg.cmd === "capture:config") {
    if (activeState && msg.config) {
      Object.assign(activeState.settings, msg.config);
      if (activeState.ws && activeState.ws.readyState === WebSocket.OPEN) {
        activeState.ws.send(JSON.stringify({
          type: "config",
          source_lang: msg.config.sourceLang,
          translation_mode: msg.config.translationMode,
          save_transcript: msg.config.saveTranscript,
          glossary: msg.config.glossary,
          summary_mode: msg.config.summaryMode,
          summary_interval_sec: msg.config.summaryIntervalSec !== undefined ? Number(msg.config.summaryIntervalSec) : undefined,
          summary_pause_sec: msg.config.summaryPauseSec !== undefined ? Number(msg.config.summaryPauseSec) : undefined,
          summary_min_new_sentences: msg.config.summaryMinNewSentences !== undefined ? Number(msg.config.summaryMinNewSentences) : undefined
        }));
      }
    }
    sendResponse({ ok: true });
    return false;
  }

  if (msg.cmd === "summary:now") {
    if (activeState && activeState.ws && activeState.ws.readyState === WebSocket.OPEN) {
      activeState.ws.send(JSON.stringify({ type: "summary_now" }));
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "ws_not_connected" });
    }
    return false;
  }

  if (msg.cmd === "caption:load") {
    if (activeState) {
      activeState.pcmPaused = true;
      activeState.captionUnavailableNotice = null;
      if (msg.tabTitle) activeState.tabTitle = msg.tabTitle;
      if (msg.tabUrl) activeState.tabUrl = msg.tabUrl;
      const trackObj = {
        videoId: msg.videoId || "yt",
        lang: msg.lang || "en",
        kind: msg.kind || "",
        isAsr: Boolean(msg.isAsr),
        fmt: msg.fmt || "auto",
        raw: msg.raw || "",
        tabTitle: activeState.tabTitle || "",
        tabUrl: activeState.tabUrl || "",
        playheadMs: Number(msg.playheadMs || 0),
        playbackRate: Number(msg.playbackRate || 1.0)
      };
      activeState.activeCaptionTrack = trackObj;
      activeState.lastPlayhead = {
        videoId: trackObj.videoId,
        playheadMs: trackObj.playheadMs,
        playbackRate: trackObj.playbackRate,
        paused: Boolean(msg.paused)
      };
      if (activeState.ws && activeState.ws.readyState === WebSocket.OPEN) {
        sendCaptionLoadOverWs(activeState.ws, trackObj, activeState.lastPlayhead);
      }
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "no_active_capture" });
    }
    return false;
  }

  if (msg.cmd === "caption:unavailable") {
    if (activeState) {
      activeState.activeCaptionTrack = null;
      activeState.pcmPaused = false;
      if (msg.tabTitle) activeState.tabTitle = msg.tabTitle;
      if (msg.tabUrl) activeState.tabUrl = msg.tabUrl;
      const reason = msg.reason || "unavailable";
      activeState.captionUnavailableNotice = {
        videoId: msg.videoId || "",
        reason,
        tabTitle: activeState.tabTitle || "",
        tabUrl: activeState.tabUrl || ""
      };
      if (activeState.ws && activeState.ws.readyState === WebSocket.OPEN) {
        activeState.ws.send(
          JSON.stringify({
            type: "caption_unavailable",
            video_id: msg.videoId || "",
            reason,
            tab_title: activeState.tabTitle || "",
            tab_url: activeState.tabUrl || ""
          })
        );
      }
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "no_active_capture" });
    }
    return false;
  }

  if (msg.cmd === "caption:playhead") {
    if (activeState) {
      const vid = msg.videoId || "";
      const playheadMs = Number(msg.playheadMs || 0);
      const playbackRate = Number(msg.playbackRate || 1.0);
      const paused = Boolean(msg.paused);
      activeState.lastPlayhead = { videoId: vid, playheadMs, playbackRate, paused };
      if (activeState.activeCaptionTrack && activeState.activeCaptionTrack.videoId === vid) {
        activeState.activeCaptionTrack.playheadMs = playheadMs;
        activeState.activeCaptionTrack.playbackRate = playbackRate;
      }
      if (activeState.ws && activeState.ws.readyState === WebSocket.OPEN) {
        activeState.ws.send(
          JSON.stringify({
            type: msg.isSeek ? "caption_seek" : "caption_progress",
            video_id: vid,
            playhead_ms: Math.round(playheadMs),
            playback_rate: playbackRate,
            paused
          })
        );
      }
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "no_active_capture" });
    }
    return false;
  }

  if (msg.cmd === "capture:stop") {
    cleanupCapture(true).then(() => sendResponse({ ok: true }));
    return true;
  }

  return false;
});

