(() => {
  const CONTENT_SCRIPT_VERSION = 3;
  if (Number(window.__LAT_CONTENT_SCRIPT_VERSION__ || 0) >= CONTENT_SCRIPT_VERSION) {
    return;
  }
  if (typeof window.__LAT_CONTENT_SCRIPT_DISPOSE__ === "function") {
    try {
      window.__LAT_CONTENT_SCRIPT_DISPOSE__();
    } catch (_) {}
  }
  window.__LAT_CONTENT_SCRIPT_LOADED__ = true;
  window.__LAT_CONTENT_SCRIPT_VERSION__ = CONTENT_SCRIPT_VERSION;

  let hostEl = null;
  let shadowRoot = null;
  let containerEl = null;
  let hudEl = null;
  let prevEl = null;
  let srcEl = null;
  let viEl = null;
  let dragHandleEl = null;

  let summaryCardEl = null;
  let summaryTopicEl = null;
  let summaryToggleBtn = null;
  let summaryBodyEl = null;
  let summaryPointsEl = null;
  let summaryQuestionEl = null;
  let summaryStatusEl = null;
  let summaryFooterEl = null;
  let isSummaryCollapsed = false;
  let hasSummaryData = false;
  let summaryUpdatedTs = 0;
  let summaryTriggerTag = "⏱ chu kỳ";
  let summaryHighlightTimer = null;
  let summaryFooterTimer = null;

  let pacer = null;
  let SubtitlePacerClass = null;

  let currentSettings = {
    sourceLang: "en",
    onlyVietnamese: false,
    showHud: true,
    fontSize: 26,
    bgOpacity: 0.62,
    overlayPosition: "bottom",
    summaryMode: "interval",
    summaryIntervalSec: 30,
    summaryPauseSec: 2,
    summaryMinNewSentences: 3,
    showSummary: true
  };

  let isOverlayActive = false;
  let isUserHidden = false;
  let targetVideo = null;
  let resizeObserver = null;
  let mutationObserver = null;
  let lastMutationScanTs = 0;
  let rafPending = false;
  let pauseDimTimer = null;

  // Trạng thái Caption Mode cho YouTube (đồng bộ trực tiếp theo video.currentTime)
  const YT_HOOK_OUT_EVENT = "__lat_yt_caption_out";
  const YT_HOOK_IN_EVENT = "__lat_yt_caption_in";
  let pendingAcquireReqId = null;
  let pendingAcquireTimer = null;
  let pendingAcquireIsBgRetry = false;
  let pendingRetryTimer = null;
  let lastProbedVideoKey = "";
  let retriedVideoKeys = new Set();
  let lastSentTrackKey = "";
  let lastSentTrackTs = 0;

  const captionState = {
    active: false,
    videoId: "",
    lang: "en",
    isAsr: false,
    cues: [],
    cueMap: new Map(),
    activeCueId: -1,
    prevVi: "",
    lastTimeMs: 0,
    syncTimer: null,
    progressTimer: null
  };

  // Offset kéo thả theo từng hostname (lưu trong chrome.storage.local)
  const dragStorageKey = `dragOffset:${location.hostname || "default"}`;
  let dragOffset = { dx: 0, dy: 0 };
  let dragState = null;

  async function loadSubtitlePacerModule() {
    if (SubtitlePacerClass) return SubtitlePacerClass;
    try {
      const mod = await import(chrome.runtime.getURL("subtitle_pacer.js"));
      SubtitlePacerClass = mod.SubtitlePacer;
    } catch (_) {
      SubtitlePacerClass = window.SubtitlePacer || null;
    }
    return SubtitlePacerClass;
  }

  async function loadSavedDragOffset() {
    try {
      const data = await chrome.storage.local.get(dragStorageKey);
      if (data && data[dragStorageKey]) {
        dragOffset = {
          dx: Number(data[dragStorageKey].dx) || 0,
          dy: Number(data[dragStorageKey].dy) || 0
        };
      } else {
        dragOffset = { dx: 0, dy: 0 };
      }
    } catch (_) {
      dragOffset = { dx: 0, dy: 0 };
    }
  }

  async function saveDragOffset() {
    try {
      await chrome.storage.local.set({ [dragStorageKey]: dragOffset });
    } catch (_) { }
  }

  function initPacerInstance() {
    if (pacer || !SubtitlePacerClass) return;
    pacer = new SubtitlePacerClass({
      onRender: ({ prev, src, vi, showBilingual }) => {
        if (!containerEl) return;
        if (prevEl) {
          prevEl.textContent = prev || "";
          prevEl.classList.toggle("lat-line-hidden", !prev);
        }
        if (srcEl) {
          srcEl.textContent = src ? `🗣️ ${src}` : "";
          const hideSrc = !showBilingual || !src;
          srcEl.classList.toggle("lat-line-hidden", hideSrc);
        }
        if (viEl) {
          if (vi) {
            viEl.className = "lat-vi";
            viEl.textContent = vi;
          }
        }
        schedulePositionUpdate();
      },
      onHud: (hudText) => {
        if (hudEl && hudText) {
          hudEl.textContent = hudText;
        }
      }
    });
    pacer.set_show_source_subtitle(!currentSettings.onlyVietnamese);
  }

  async function ensureOverlayDom() {
    await loadSubtitlePacerModule();
    await loadSavedDragOffset();
    initPacerInstance();

    if (hostEl && containerEl) {
      attachHostToProperParent();
      return;
    }

    hostEl = document.createElement("div");
    hostEl.id = "lat-overlay-host";
    shadowRoot = hostEl.attachShadow({ mode: "closed" });

    const styleEl = document.createElement("style");
    try {
      const cssUrl = chrome.runtime.getURL("overlay.css");
      const resp = await fetch(cssUrl);
      styleEl.textContent = await resp.text();
    } catch (_) {
      styleEl.textContent = `
        .lat-container { position: fixed; left: 50%; bottom: 10%; transform: translateX(-50%); background: rgba(10,14,23,0.68); color: #fff; padding: 10px 18px; border-radius: 12px; z-index: 2147483647; text-align: center; }
        .lat-vi { color: #ffd54a; font-size: 26px; font-weight: bold; }
      `;
    }
    shadowRoot.appendChild(styleEl);

    containerEl = document.createElement("div");
    containerEl.className = "lat-container";

    dragHandleEl = document.createElement("div");
    dragHandleEl.className = "lat-drag-handle";
    dragHandleEl.title = "Kéo để di chuyển vị trí phụ đề (nhấp đúp để đặt lại)";
    dragHandleEl.textContent = "⋮⋮";
    setupDragEvents(dragHandleEl);

    hudEl = document.createElement("div");
    hudEl.className = "lat-hud";
    hudEl.textContent = "⏳ Đang kết nối máy dịch...";

    prevEl = document.createElement("div");
    prevEl.className = "lat-prev lat-line-hidden";

    srcEl = document.createElement("div");
    srcEl.className = "lat-src lat-line-hidden";

    viEl = document.createElement("div");
    viEl.className = "lat-vi";
    viEl.textContent = "⏳ Đang khởi tạo phụ đề AI...";

    containerEl.appendChild(dragHandleEl);
    containerEl.appendChild(hudEl);
    containerEl.appendChild(prevEl);
    containerEl.appendChild(srcEl);
    containerEl.appendChild(viEl);
    shadowRoot.appendChild(containerEl);

    summaryCardEl = document.createElement("div");
    summaryCardEl.className = "lat-summary-card lat-hidden";

    const headerEl = document.createElement("div");
    headerEl.className = "lat-summary-header";

    summaryTopicEl = document.createElement("div");
    summaryTopicEl.className = "lat-summary-topic";
    summaryTopicEl.textContent = "🧭 Ý chính đang bàn";

    summaryToggleBtn = document.createElement("button");
    summaryToggleBtn.className = "lat-summary-toggle";
    summaryToggleBtn.type = "button";
    summaryToggleBtn.title = "Thu gọn / mở rộng thẻ ý chính";
    summaryToggleBtn.textContent = "–";
    summaryToggleBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      isSummaryCollapsed = !isSummaryCollapsed;
      if (summaryBodyEl) {
        summaryBodyEl.classList.toggle("lat-collapsed", isSummaryCollapsed);
      }
      summaryToggleBtn.textContent = isSummaryCollapsed ? "+" : "–";
    });

    headerEl.appendChild(summaryTopicEl);
    headerEl.appendChild(summaryToggleBtn);
    summaryCardEl.appendChild(headerEl);

    summaryBodyEl = document.createElement("div");
    summaryBodyEl.className = "lat-summary-body";

    summaryPointsEl = document.createElement("div");
    summaryPointsEl.className = "lat-summary-points";

    summaryQuestionEl = document.createElement("div");
    summaryQuestionEl.className = "lat-summary-question lat-line-hidden";

    summaryStatusEl = document.createElement("div");
    summaryStatusEl.className = "lat-summary-status lat-line-hidden";

    summaryFooterEl = document.createElement("div");
    summaryFooterEl.className = "lat-summary-footer lat-line-hidden";

    summaryBodyEl.appendChild(summaryPointsEl);
    summaryBodyEl.appendChild(summaryQuestionEl);
    summaryBodyEl.appendChild(summaryStatusEl);
    summaryBodyEl.appendChild(summaryFooterEl);
    summaryCardEl.appendChild(summaryBodyEl);
    shadowRoot.appendChild(summaryCardEl);

    attachHostToProperParent();
    applySettingsToDom();
    startVideoTracking();
  }

  function setupDragEvents(handle) {
    handle.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      dragState = {
        startX: e.clientX,
        startY: e.clientY,
        origDx: dragOffset.dx,
        origDy: dragOffset.dy
      };
      window.addEventListener("mousemove", onDragMove, true);
      window.addEventListener("mouseup", onDragEnd, true);
    });

    handle.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      dragOffset = { dx: 0, dy: 0 };
      saveDragOffset();
      schedulePositionUpdate();
    });
  }

  function onDragMove(e) {
    if (!dragState) return;
    dragOffset.dx = dragState.origDx + (e.clientX - dragState.startX);
    dragOffset.dy = dragState.origDy + (e.clientY - dragState.startY);
    schedulePositionUpdate();
  }

  function onDragEnd() {
    if (!dragState) return;
    dragState = null;
    window.removeEventListener("mousemove", onDragMove, true);
    window.removeEventListener("mouseup", onDragEnd, true);
    saveDragOffset();
  }

  function attachHostToProperParent() {
    if (!hostEl) return;
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    const desiredParent = fsEl || document.documentElement || document.body;
    if (desiredParent && hostEl.parentNode !== desiredParent) {
      desiredParent.appendChild(hostEl);
    }
  }

  function applySettingsToDom() {
    if (!containerEl) return;
    const fontSize = Number(currentSettings.fontSize) || 26;
    const bgOpacity = Number(currentSettings.bgOpacity ?? 0.62);
    containerEl.style.setProperty("--lat-vi-size", `${fontSize}px`);
    containerEl.style.setProperty("--lat-bg-opacity", String(bgOpacity));

    if (pacer) {
      pacer.set_show_source_subtitle(!currentSettings.onlyVietnamese);
    }
    if (hudEl) {
      hudEl.classList.toggle("lat-line-hidden", !currentSettings.showHud);
    }
    if (srcEl) {
      const hideSrc = Boolean(currentSettings.onlyVietnamese) || !srcEl.textContent.trim();
      srcEl.classList.toggle("lat-line-hidden", hideSrc);
    }
    containerEl.classList.toggle("lat-hidden", Boolean(isUserHidden));
    updateSummaryVisibility();
    schedulePositionUpdate();
  }

  function updateSummaryVisibility() {
    if (!summaryCardEl) return;
    const enabled =
      Boolean(currentSettings.showSummary !== false) &&
      currentSettings.summaryMode !== "off" &&
      !isUserHidden &&
      hasSummaryData;
    summaryCardEl.classList.toggle("lat-hidden", !enabled);
  }

  function refreshSummaryFooter() {
    if (!summaryFooterEl || !summaryUpdatedTs) return;
    const elapsedSec = Math.max(0, Math.floor((Date.now() - summaryUpdatedTs) / 1000));
    summaryFooterEl.textContent = `${summaryTriggerTag} · ${elapsedSec}s`;
    summaryFooterEl.classList.remove("lat-line-hidden");
  }

  function resetSummaryCard() {
    hasSummaryData = false;
    summaryUpdatedTs = 0;
    if (summaryHighlightTimer) {
      clearTimeout(summaryHighlightTimer);
      summaryHighlightTimer = null;
    }
    if (summaryFooterTimer) {
      clearInterval(summaryFooterTimer);
      summaryFooterTimer = null;
    }
    if (summaryTopicEl) {
      summaryTopicEl.textContent = "🧭 Ý chính đang bàn";
    }
    if (summaryPointsEl) {
      summaryPointsEl.textContent = "";
    }
    if (summaryQuestionEl) {
      summaryQuestionEl.textContent = "";
      summaryQuestionEl.classList.add("lat-line-hidden");
    }
    if (summaryStatusEl) {
      summaryStatusEl.textContent = "";
      summaryStatusEl.classList.add("lat-line-hidden");
    }
    if (summaryFooterEl) {
      summaryFooterEl.textContent = "";
      summaryFooterEl.classList.add("lat-line-hidden");
    }
    updateSummaryVisibility();
  }

  function isYoutubeVideoPage() {
    const host = (location.hostname || "").toLowerCase();
    if (host !== "youtube.com" && !host.endsWith(".youtube.com")) {
      return false;
    }
    const p = location.pathname || "";
    return p === "/watch" || p.startsWith("/shorts/") || p.startsWith("/live/");
  }

  function getCurrentYoutubeVideoId() {
    if (!isYoutubeVideoPage()) return "";
    try {
      const u = new URL(location.href);
      const v = u.searchParams.get("v");
      if (v) return v;
      const sm = u.pathname.match(/^\/shorts\/([a-zA-Z0-9_-]{6,})/);
      if (sm) return sm[1];
      const lm = u.pathname.match(/^\/live\/([a-zA-Z0-9_-]{6,})/);
      if (lm) return lm[1];
    } catch (_) { }
    return "";
  }

  function getVideoPlaybackSnapshot() {
    const v = targetVideo || findBestVideoElement();
    if (!v) {
      return { playheadMs: 0, playbackRate: 1.0, paused: false };
    }
    let curSec = Number(v.currentTime) || 0;
    const durSec = Number(v.duration);
    if (Number.isFinite(durSec) && durSec > 0 && curSec > durSec + 1.5) {
      curSec = 0;
    }
    const playheadMs = Math.max(0, Math.round(curSec * 1000));
    const playbackRate = Math.max(0.25, Math.min(3.0, Number(v.playbackRate) || 1.0));
    return {
      playheadMs,
      playbackRate,
      paused: Boolean(v.paused)
    };
  }

  function formatPrevCaptionLine(text) {
    const clean = (text || "").trim();
    if (!clean) return "";
    if (SubtitlePacerClass && typeof SubtitlePacerClass._format_prev_line === "function") {
      return SubtitlePacerClass._format_prev_line(clean);
    }
    return clean.length <= 135 ? `⤴ ${clean}` : `⤴ ${clean.slice(0, 128)}…`;
  }

  function computeTranslatedAheadSec(cues, nowMs) {
    let furthestMs = nowMs;
    for (let i = 0; i < cues.length; i++) {
      const c = cues[i];
      if (c.end_ms < nowMs) continue;
      if (c.vi) {
        furthestMs = Math.max(furthestMs, c.end_ms);
      } else {
        break;
      }
    }
    return Math.max(0, Math.round((furthestMs - nowMs) / 1000));
  }

  function findCueForTimeMs(cues, nowMs) {
    if (!cues || cues.length === 0) {
      return { cue: null, clearSilent: false };
    }
    let lo = 0;
    let hi = cues.length - 1;
    let candidateIdx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start_ms <= nowMs) {
        candidateIdx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }

    if (candidateIdx < 0) {
      return { cue: null, clearSilent: nowMs + 1500 < cues[0].start_ms };
    }

    const cand = cues[candidateIdx];
    if (nowMs <= cand.end_ms) {
      return { cue: cand, clearSilent: false };
    }

    const nextStart =
      candidateIdx + 1 < cues.length ? cues[candidateIdx + 1].start_ms : Number.POSITIVE_INFINITY;
    const afterEndMs = nowMs - cand.end_ms;
    // Giữ nhẹ 450ms sau khi dứt câu nếu câu kế tiếp chưa bắt đầu để tránh nhấp nháy phụ đề
    if (afterEndMs <= 450 && nowMs < nextStart) {
      return { cue: cand, clearSilent: false };
    }
    if (afterEndMs >= 2600 && nowMs < nextStart) {
      return { cue: null, clearSilent: true };
    }
    return { cue: null, clearSilent: false };
  }

  function renderCaptionFrame() {
    if (!captionState.active || !containerEl) return;
    if (!targetVideo || !targetVideo.isConnected) {
      targetVideo = findBestVideoElement();
    }
    const { playheadMs } = getVideoPlaybackSnapshot();
    captionState.lastTimeMs = playheadMs;

    const { cue, clearSilent } = findCueForTimeMs(captionState.cues, playheadMs);
    const aheadSec = computeTranslatedAheadSec(captionState.cues, playheadMs);
    const kindLabel = captionState.isAsr ? "Tự động" : "Chuẩn";

    if (!cue) {
      if (clearSilent && captionState.activeCueId >= 0) {
        const prevCue = captionState.cueMap.get(captionState.activeCueId);
        if (prevCue && prevCue.vi) {
          captionState.prevVi = prevCue.vi;
        }
        captionState.activeCueId = -1;
        if (prevEl) {
          const pl = formatPrevCaptionLine(captionState.prevVi);
          prevEl.textContent = pl;
          prevEl.classList.toggle("lat-line-hidden", !pl);
        }
        if (srcEl) {
          srcEl.textContent = "";
          srcEl.classList.add("lat-line-hidden");
        }
        if (viEl) {
          viEl.className = "lat-vi";
          viEl.textContent = "…";
        }
        schedulePositionUpdate();
      }
      if (hudEl && currentSettings.showHud) {
        hudEl.textContent = `⚡ Phụ đề gốc YT (${captionState.lang.toUpperCase()} · ${kindLabel}) | Đón đầu: +${aheadSec}s`;
      }
      return;
    }

    if (cue.cue_id !== captionState.activeCueId) {
      if (captionState.activeCueId >= 0) {
        const prevCue = captionState.cueMap.get(captionState.activeCueId);
        if (prevCue && prevCue.vi) {
          captionState.prevVi = prevCue.vi;
        }
      }
      captionState.activeCueId = cue.cue_id;
    }

    if (prevEl) {
      const prevLine = formatPrevCaptionLine(captionState.prevVi);
      prevEl.textContent = prevLine;
      prevEl.classList.toggle("lat-line-hidden", !prevLine);
    }

    if (srcEl) {
      srcEl.textContent = cue.src ? `🗣️ ${cue.src}` : "";
      const hideSrc = Boolean(currentSettings.onlyVietnamese) || !cue.src;
      srcEl.classList.toggle("lat-line-hidden", hideSrc);
    }

    if (viEl) {
      viEl.className = "lat-vi";
      viEl.textContent = cue.vi ? cue.vi : "⏳ Đang dịch sang Tiếng Việt...";
    }

    if (hudEl && currentSettings.showHud) {
      const engTag = cue.engine || "AI";
      hudEl.textContent = `⚡ Phụ đề gốc YT (${captionState.lang.toUpperCase()} · ${kindLabel}) | Đón đầu: +${aheadSec}s | ${engTag}`;
    }

    schedulePositionUpdate();
  }

  function sendPlayheadToBackend(isSeek = false) {
    if (!isOverlayActive || !captionState.active || !captionState.videoId) return;
    const snap = getVideoPlaybackSnapshot();
    chrome.runtime
      .sendMessage({
        from: "content_script",
        cmd: "caption:playhead",
        videoId: captionState.videoId,
        playheadMs: snap.playheadMs,
        playbackRate: snap.playbackRate,
        paused: snap.paused,
        isSeek: Boolean(isSeek)
      })
      .catch(() => { });
  }

  function startCaptionSync(initPayload) {
    stopCaptionSync();
    const rawCues = Array.isArray(initPayload.cues) ? initPayload.cues : [];
    captionState.active = true;
    captionState.videoId = String(initPayload.video_id || getCurrentYoutubeVideoId() || "yt");
    captionState.lang = String(initPayload.lang || "en");
    captionState.isAsr = Boolean(initPayload.is_asr);
    captionState.cues = rawCues.map((c) => ({
      cue_id: Number(c.cue_id),
      start_ms: Number(c.start_ms) || 0,
      end_ms: Number(c.end_ms) || 0,
      src: String(c.src || ""),
      vi: String(c.vi || ""),
      engine: String(c.engine || ""),
      trans_ms: Number(c.trans_ms) || 0
    }));
    captionState.cues.sort((a, b) => a.start_ms - b.start_ms);
    captionState.cueMap = new Map(captionState.cues.map((c) => [c.cue_id, c]));
    captionState.activeCueId = -1;
    captionState.prevVi = "";

    if (pacer) {
      pacer.reset_subtitles();
    }

    renderCaptionFrame();
    captionState.syncTimer = setInterval(renderCaptionFrame, 80);
    captionState.progressTimer = setInterval(() => sendPlayheadToBackend(false), 1500);
  }

  function stopCaptionSync() {
    if (captionState.syncTimer) {
      clearInterval(captionState.syncTimer);
      captionState.syncTimer = null;
    }
    if (captionState.progressTimer) {
      clearInterval(captionState.progressTimer);
      captionState.progressTimer = null;
    }
    captionState.active = false;
    captionState.videoId = "";
    captionState.cues = [];
    captionState.cueMap.clear();
    captionState.activeCueId = -1;
    captionState.prevVi = "";
  }

  function clearPendingAcquire() {
    pendingAcquireReqId = null;
    pendingAcquireIsBgRetry = false;
    if (pendingAcquireTimer) {
      clearTimeout(pendingAcquireTimer);
      pendingAcquireTimer = null;
    }
  }

  function clearPendingRetry() {
    if (pendingRetryTimer) {
      clearTimeout(pendingRetryTimer);
      pendingRetryTimer = null;
    }
  }

  function sendCapturedTrackToBackend(track) {
    if (!isOverlayActive || !track || !track.raw) return;
    const vid = track.videoId || getCurrentYoutubeVideoId() || "yt";
    const trackKey = `${vid}:${track.lang || ""}:${Boolean(track.isAsr)}:${track.raw.length}`;
    const now = Date.now();
    if (trackKey === lastSentTrackKey && now - lastSentTrackTs < 5000) {
      return;
    }
    lastSentTrackKey = trackKey;
    lastSentTrackTs = now;
    clearPendingRetry();

    const snap = getVideoPlaybackSnapshot();
    chrome.runtime
      .sendMessage({
        from: "content_script",
        cmd: "caption:load",
        videoId: vid,
        lang: track.lang || currentSettings.sourceLang || "en",
        kind: track.kind || "",
        isAsr: Boolean(track.isAsr),
        fmt: track.fmt || "auto",
        raw: track.raw,
        playheadMs: snap.playheadMs,
        playbackRate: snap.playbackRate,
        paused: snap.paused,
        tabTitle: document.title || "YouTube",
        tabUrl: location.href
      })
      .catch(() => { });
  }

  function sendCaptionUnavailableToBackend(videoId = "", reason = "unavailable") {
    if (!isOverlayActive) return;
    lastSentTrackKey = "";
    lastSentTrackTs = 0;
    stopCaptionSync();
    chrome.runtime
      .sendMessage({
        from: "content_script",
        cmd: "caption:unavailable",
        videoId: videoId || getCurrentYoutubeVideoId() || "",
        reason: String(reason || "unavailable"),
        tabTitle: document.title || "YouTube",
        tabUrl: location.href
      })
      .catch(() => { });
  }

  function requestYoutubeCaptions(force = false, isBgRetry = false) {
    if (!isOverlayActive || !isYoutubeVideoPage()) return;
    const vid = getCurrentYoutubeVideoId();
    const probeKey = `${vid}:${currentSettings.sourceLang || "en"}`;
    if (!force && vid && probeKey === lastProbedVideoKey && (captionState.active || pendingAcquireReqId)) {
      return;
    }
    lastProbedVideoKey = probeKey;
    clearPendingAcquire();
    if (!isBgRetry) {
      clearPendingRetry();
    }

    const reqId = `acq-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    pendingAcquireReqId = reqId;
    pendingAcquireIsBgRetry = Boolean(isBgRetry);
    pendingAcquireTimer = setTimeout(() => {
      if (pendingAcquireReqId !== reqId) return;
      const wasBgRetry = pendingAcquireIsBgRetry;
      clearPendingAcquire();
      const retryKey = `${vid}:${currentSettings.sourceLang || "en"}`;
      if (!wasBgRetry && vid && !retriedVideoKeys.has(retryKey)) {
        retriedVideoKeys.add(retryKey);
        clearPendingRetry();
        pendingRetryTimer = setTimeout(() => {
          pendingRetryTimer = null;
          if (!isOverlayActive || captionState.active) return;
          if (getCurrentYoutubeVideoId() !== vid) return;
          requestYoutubeCaptions(true, true);
        }, 900);
        return;
      }
      sendCaptionUnavailableToBackend(vid, "acquire_timeout");
    }, 5500);

    try {
      document.dispatchEvent(
        new CustomEvent(YT_HOOK_IN_EVENT, {
          detail: JSON.stringify({
            cmd: "acquire",
            reqId,
            videoId: vid,
            sourceLang: currentSettings.sourceLang || "en"
          })
        })
      );
    } catch (_) {
      clearPendingAcquire();
      sendCaptionUnavailableToBackend(vid, "dispatch_error");
    }
  }

  const onHookOutEvent = (evt) => {
    if (!evt || !evt.detail) return;
    let msg;
    try {
      msg = typeof evt.detail === "string" ? JSON.parse(evt.detail) : evt.detail;
    } catch (_) {
      return;
    }
    if (!msg || !msg.event || !isOverlayActive) return;

    if (msg.event === "acquire_result") {
      let wasBgRetry = false;
      if (pendingAcquireReqId && msg.reqId === pendingAcquireReqId) {
        wasBgRetry = pendingAcquireIsBgRetry;
        clearPendingAcquire();
      }
      if (msg.status === "ready" && msg.track && msg.track.raw) {
        sendCapturedTrackToBackend(msg.track);
      } else {
        const vid = msg.videoId || getCurrentYoutubeVideoId() || "";
        const transientReasons = new Set(["flash_timeout", "no_player", "player_api_error", "no_captions"]);
        const retryKey = `${vid}:${currentSettings.sourceLang || "en"}`;
        if (
          !wasBgRetry &&
          vid &&
          transientReasons.has(msg.reason) &&
          !retriedVideoKeys.has(retryKey)
        ) {
          // Thử lại thêm 1 nhịp sau 1200ms trước khi báo unavailable để tránh chuyển nhầm sang Audio Mode sớm
          retriedVideoKeys.add(retryKey);
          clearPendingRetry();
          pendingRetryTimer = setTimeout(() => {
            pendingRetryTimer = null;
            if (!isOverlayActive || captionState.active) return;
            if (getCurrentYoutubeVideoId() !== vid) return;
            requestYoutubeCaptions(true, true);
          }, 1200);
          return;
        }
        sendCaptionUnavailableToBackend(vid, msg.reason || "unavailable");
      }
      return;
    }

    if (msg.event === "track_captured" && msg.track && msg.track.raw) {
      const curVid = getCurrentYoutubeVideoId();
      if (!curVid || msg.track.videoId !== curVid) return;
      // Tự nâng cấp nóng lên Caption Mode nếu đang ở Audio Mode hoặc khi vừa nạp được phụ đề chuẩn thay cho ASR
      const shouldUpgrade =
        !captionState.active ||
        captionState.videoId !== curVid ||
        (captionState.isAsr && !msg.track.isAsr);
      if (shouldUpgrade) {
        clearPendingAcquire();
        sendCapturedTrackToBackend(msg.track);
      }
    }
  };
  document.addEventListener(YT_HOOK_OUT_EVENT, onHookOutEvent);

  function isIgnoredPreviewVideo(v) {
    if (!v || typeof v.closest !== "function") return false;
    return Boolean(
      v.closest(
        "ytd-video-preview, ytd-inline-preview-player, ytd-moving-thumbnail-renderer, #inline-preview-player"
      )
    );
  }

  function findBestVideoElement() {
    if (isYoutubeVideoPage()) {
      const mainYtVideo = document.querySelector(
        "#movie_player video.html5-main-video, #shorts-player video.html5-main-video, #movie_player video"
      );
      if (mainYtVideo) {
        const r = mainYtVideo.getBoundingClientRect();
        if (r.width >= 120 && r.height >= 80) {
          return mainYtVideo;
        }
      }
    }

    const videos = Array.from(document.querySelectorAll("video"));
    if (videos.length === 0) return null;

    let best = null;
    let bestScore = 0;

    for (const v of videos) {
      if (isIgnoredPreviewVideo(v)) continue;
      const rect = v.getBoundingClientRect();
      if (rect.width < 120 || rect.height < 80) continue;
      const area = rect.width * rect.height;
      const playingBonus = (!v.paused && !v.ended && v.readyState > 1) ? 4.0 : 1.0;
      const score = area * playingBonus;
      if (score > bestScore) {
        bestScore = score;
        best = v;
      }
    }
    return best;
  }

  function updateOverlayPosition() {
    rafPending = false;
    if (!containerEl || !isOverlayActive) return;

    if (!targetVideo || !targetVideo.isConnected) {
      targetVideo = findBestVideoElement();
    }

    let baseCenterX = window.innerWidth / 2;
    let baseTop = null;
    let baseBottom = 48;
    let maxWidthCss = "min(86vw, 920px)";

    if (targetVideo) {
      const rect = targetVideo.getBoundingClientRect();
      if (rect.width >= 120 && rect.height >= 80) {
        baseCenterX = rect.left + rect.width / 2;
        const maxW = Math.max(280, Math.min(960, Math.round(rect.width * 0.82)));
        maxWidthCss = `${maxW}px`;

        if (currentSettings.overlayPosition === "top") {
          baseTop = Math.max(12, Math.round(rect.top + rect.height * 0.08));
          baseBottom = null;
        } else {
          baseBottom = Math.max(
            16,
            Math.round(window.innerHeight - rect.bottom + rect.height * 0.12)
          );
          baseTop = null;
        }
      }
    }

    containerEl.style.maxWidth = maxWidthCss;
    const finalCenterX = Math.max(
      140,
      Math.min(window.innerWidth - 140, Math.round(baseCenterX + dragOffset.dx))
    );
    containerEl.style.left = `${finalCenterX}px`;

    if (baseTop !== null) {
      const finalTop = Math.max(8, Math.min(window.innerHeight - 60, Math.round(baseTop + dragOffset.dy)));
      containerEl.style.top = `${finalTop}px`;
      containerEl.style.bottom = "auto";
    } else {
      const finalBottom = Math.max(8, Math.min(window.innerHeight - 60, Math.round(baseBottom - dragOffset.dy)));
      containerEl.style.bottom = `${finalBottom}px`;
      containerEl.style.top = "auto";
    }

    if (summaryCardEl) {
      let summaryTop = 16;
      let summaryRight = 16;
      let summaryMaxW = "min(32vw, 360px)";
      if (targetVideo) {
        const rect = targetVideo.getBoundingClientRect();
        if (rect.width >= 120 && rect.height >= 80) {
          summaryTop = Math.max(12, Math.round(rect.top + 14));
          summaryRight = Math.max(12, Math.round(window.innerWidth - rect.right + 14));
          const cardMaxW = Math.max(220, Math.min(380, Math.round(rect.width * 0.32)));
          summaryMaxW = `${cardMaxW}px`;
        }
      }
      summaryCardEl.style.top = `${summaryTop}px`;
      summaryCardEl.style.right = `${summaryRight}px`;
      summaryCardEl.style.maxWidth = summaryMaxW;
    }
  }

  function schedulePositionUpdate() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(updateOverlayPosition);
  }

  function onMutationThrottled() {
    const now = performance.now();
    if (now - lastMutationScanTs < 1000) return;
    lastMutationScanTs = now;
    const candidate = findBestVideoElement();
    if (candidate && candidate !== targetVideo) {
      targetVideo = candidate;
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver.observe(targetVideo);
      }
      schedulePositionUpdate();
    }
  }

  function onYtNavigateFinish() {
    if (!isOverlayActive) return;
    const currentVid = getCurrentYoutubeVideoId();
    if (captionState.active && captionState.videoId && captionState.videoId === currentVid) {
      targetVideo = findBestVideoElement();
      if (resizeObserver) {
        resizeObserver.disconnect();
        if (targetVideo) resizeObserver.observe(targetVideo);
      }
      schedulePositionUpdate();
      renderCaptionFrame();
      sendPlayheadToBackend(true);
      return;
    }
    stopCaptionSync();
    clearPendingRetry();
    lastProbedVideoKey = "";
    lastSentTrackKey = "";
    lastSentTrackTs = 0;
    if (pacer) {
      pacer.reset_subtitles();
    }
    resetSummaryCard();
    if (prevEl) {
      prevEl.textContent = "";
      prevEl.classList.add("lat-line-hidden");
    }
    if (srcEl) {
      srcEl.textContent = "";
      srcEl.classList.add("lat-line-hidden");
    }
    if (viEl) {
      viEl.className = "lat-vi";
      viEl.textContent = "⏳ Đang nạp phụ đề cho video mới...";
    }
    targetVideo = findBestVideoElement();
    if (resizeObserver) {
      resizeObserver.disconnect();
      if (targetVideo) resizeObserver.observe(targetVideo);
    }
    schedulePositionUpdate();
    requestYoutubeCaptions(true);
  }

  function onMediaSeekOrRateChangeCapture(evt) {
    if (!captionState.active) return;
    if (evt && evt.target && evt.target.tagName === "VIDEO") {
      if (isIgnoredPreviewVideo(evt.target)) return;
      if (!targetVideo || evt.target === targetVideo) {
        targetVideo = evt.target;
        const { playheadMs } = getVideoPlaybackSnapshot();
        if (Math.abs(playheadMs - captionState.lastTimeMs) > 2500) {
          captionState.prevVi = "";
          captionState.activeCueId = -1;
        }
        renderCaptionFrame();
        sendPlayheadToBackend(evt.type === "seeked");
      }
    }
  }

  function onMediaPlayCapture(evt) {
    if (evt && evt.target && evt.target.tagName === "VIDEO") {
      if (isIgnoredPreviewVideo(evt.target)) return;
      if (pauseDimTimer) {
        clearTimeout(pauseDimTimer);
        pauseDimTimer = null;
      }
      if (containerEl) {
        containerEl.classList.remove("lat-paused-dim");
      }
      targetVideo = evt.target;
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver.observe(targetVideo);
      }
      schedulePositionUpdate();
    }
  }

  function onMediaPauseCapture(evt) {
    if (evt && evt.target && evt.target === targetVideo) {
      if (pauseDimTimer) clearTimeout(pauseDimTimer);
      pauseDimTimer = setTimeout(() => {
        if (containerEl && targetVideo && targetVideo.paused) {
          containerEl.classList.add("lat-paused-dim");
        }
      }, 5000);
    }
  }

  function onFullscreenChange() {
    attachHostToProperParent();
    schedulePositionUpdate();
  }

  function startVideoTracking() {
    targetVideo = findBestVideoElement();
    if (resizeObserver) {
      resizeObserver.disconnect();
    }
    resizeObserver = new ResizeObserver(() => schedulePositionUpdate());
    if (targetVideo) {
      resizeObserver.observe(targetVideo);
    }

    if (mutationObserver) {
      mutationObserver.disconnect();
    }
    mutationObserver = new MutationObserver(onMutationThrottled);
    if (document.body) {
      mutationObserver.observe(document.body, { childList: true, subtree: true });
    }

    window.addEventListener("resize", schedulePositionUpdate, { passive: true });
    window.addEventListener("scroll", schedulePositionUpdate, { passive: true });
    window.addEventListener("yt-navigate-finish", onYtNavigateFinish);
    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    document.addEventListener("play", onMediaPlayCapture, true);
    document.addEventListener("pause", onMediaPauseCapture, true);
    document.addEventListener("seeked", onMediaSeekOrRateChangeCapture, true);
    document.addEventListener("ratechange", onMediaSeekOrRateChangeCapture, true);
    schedulePositionUpdate();
  }

  function stopVideoTracking() {
    if (pauseDimTimer) {
      clearTimeout(pauseDimTimer);
      pauseDimTimer = null;
    }
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
    if (mutationObserver) {
      mutationObserver.disconnect();
      mutationObserver = null;
    }
    window.removeEventListener("resize", schedulePositionUpdate);
    window.removeEventListener("scroll", schedulePositionUpdate);
    window.removeEventListener("yt-navigate-finish", onYtNavigateFinish);
    document.removeEventListener("fullscreenchange", onFullscreenChange);
    document.removeEventListener("webkitfullscreenchange", onFullscreenChange);
    document.removeEventListener("play", onMediaPlayCapture, true);
    document.removeEventListener("pause", onMediaPauseCapture, true);
    document.removeEventListener("seeked", onMediaSeekOrRateChangeCapture, true);
    document.removeEventListener("ratechange", onMediaSeekOrRateChangeCapture, true);
    targetVideo = null;
  }

  function handleBackendEvent(evt) {
    if (!evt || !containerEl) return;

    if (evt.type === "caption_track_ready") {
      startCaptionSync(evt);
      return;
    }

    if (evt.type === "caption_cues") {
      if (!captionState.active) return;
      if (evt.video_id && captionState.videoId && evt.video_id !== captionState.videoId) return;
      const incoming = Array.isArray(evt.cues) ? evt.cues : [];
      for (const item of incoming) {
        const target = captionState.cueMap.get(Number(item.cue_id));
        if (target && item.vi) {
          target.vi = String(item.vi);
          target.engine = String(item.engine || target.engine || "AI");
          target.trans_ms = Number(item.trans_ms || target.trans_ms || 0);
        }
      }
      renderCaptionFrame();
      return;
    }

    if (evt.type === "caption_fallback") {
      lastSentTrackKey = "";
      lastSentTrackTs = 0;
      stopCaptionSync();
      return;
    }

    if (evt.type === "status") {
      if (captionState.active && captionState.activeCueId >= 0 && evt.level !== "error") {
        return;
      }
      if (hudEl) {
        hudEl.textContent = evt.message || "";
      }
      const hasSubtitle =
        Boolean(pacer && pacer._current_vi) ||
        Boolean(captionState.active && captionState.activeCueId >= 0);
      if (evt.level === "error") {
        viEl.className = "lat-vi lat-status-error";
        viEl.textContent = evt.message || "❌ Lỗi máy dịch";
      } else if (evt.level === "warn" && !hasSubtitle) {
        viEl.className = "lat-vi lat-status-warn";
        viEl.textContent = evt.message || "";
      } else if (!hasSubtitle) {
        viEl.className = "lat-vi";
        viEl.textContent = evt.message || "🟢 Đang nghe âm thanh tab...";
      }
      return;
    }

    if (evt.type === "source") {
      const text = (evt.text || "").trim();
      if (!text) return;
      if (pacer) {
        pacer.show_incoming_source(text);
        if (!pacer._current_vi) {
          viEl.className = "lat-vi";
          viEl.textContent = "⏳ Đang dịch sang Tiếng Việt...";
        }
      } else {
        srcEl.textContent = `🗣️ ${text}`;
        srcEl.classList.toggle("lat-line-hidden", Boolean(currentSettings.onlyVietnamese));
      }
      return;
    }

    if (evt.type === "result") {
      const src = (evt.src || "").trim();
      const vi = (evt.vi || "").trim();
      if (!vi) return;
      if (pacer) {
        pacer.update_incoming_subtitle(src, vi, evt.stt_ms, evt.trans_ms, evt.engine || "AI");
      } else {
        viEl.className = "lat-vi";
        viEl.textContent = vi;
      }
      return;
    }

    if (evt.type === "summary_status") {
      const msg = (evt.message || "").trim();
      if (!msg || !summaryStatusEl) return;
      hasSummaryData = true;
      summaryStatusEl.textContent = msg;
      summaryStatusEl.classList.remove("lat-line-hidden");
      updateSummaryVisibility();
      schedulePositionUpdate();
      return;
    }

    if (evt.type === "summary") {
      if (!summaryCardEl) return;
      hasSummaryData = true;
      const topic = (evt.topic || "Ý chính đang bàn").trim();
      const points = Array.isArray(evt.points) ? evt.points : [];
      const newCount = Math.max(0, Number(evt.new_count) || 0);
      const question = (evt.question || "").trim();
      const trigger = evt.trigger || "interval";

      const triggerMap = {
        interval: "⏱ chu kỳ",
        pause: "⏸ nhịp nghỉ",
        manual: "✋ thủ công",
        cap: "⛑ chốt an toàn"
      };
      summaryTriggerTag = triggerMap[trigger] || "⏱ chu kỳ";
      summaryUpdatedTs = Date.now();

      if (summaryTopicEl) {
        summaryTopicEl.textContent = `🧭 ${topic}`;
      }

      if (summaryStatusEl) {
        summaryStatusEl.textContent = "";
        summaryStatusEl.classList.add("lat-line-hidden");
      }

      if (summaryPointsEl) {
        summaryPointsEl.textContent = "";
        points.forEach((pt, idx) => {
          const item = document.createElement("div");
          item.className = idx < newCount ? "lat-summary-point lat-point-new" : "lat-summary-point";
          item.textContent = `• ${pt}`;
          summaryPointsEl.appendChild(item);
        });
      }

      if (summaryHighlightTimer) {
        clearTimeout(summaryHighlightTimer);
      }
      if (newCount > 0) {
        summaryHighlightTimer = setTimeout(() => {
          if (!summaryPointsEl) return;
          const items = summaryPointsEl.querySelectorAll(".lat-point-new");
          items.forEach((el) => el.classList.remove("lat-point-new"));
        }, 4000);
      }

      if (summaryQuestionEl) {
        if (question) {
          summaryQuestionEl.textContent = `❓ ${question}`;
          summaryQuestionEl.classList.remove("lat-line-hidden");
        } else {
          summaryQuestionEl.textContent = "";
          summaryQuestionEl.classList.add("lat-line-hidden");
        }
      }

      refreshSummaryFooter();
      if (!summaryFooterTimer) {
        summaryFooterTimer = setInterval(refreshSummaryFooter, 1000);
      }

      updateSummaryVisibility();
      schedulePositionUpdate();
    }
  }

  function destroyOverlay() {
    isOverlayActive = false;
    isUserHidden = false;
    lastProbedVideoKey = "";
    lastSentTrackKey = "";
    lastSentTrackTs = 0;
    retriedVideoKeys.clear();
    clearPendingAcquire();
    clearPendingRetry();
    stopCaptionSync();
    if (pacer) {
      pacer.reset_subtitles();
    }
    resetSummaryCard();
    stopVideoTracking();
    if (hostEl && hostEl.parentNode) {
      hostEl.parentNode.removeChild(hostEl);
    }
    hostEl = null;
    shadowRoot = null;
    containerEl = null;
    hudEl = null;
    prevEl = null;
    srcEl = null;
    viEl = null;
    dragHandleEl = null;
    summaryCardEl = null;
    summaryTopicEl = null;
    summaryToggleBtn = null;
    summaryBodyEl = null;
    summaryPointsEl = null;
    summaryQuestionEl = null;
    summaryStatusEl = null;
    summaryFooterEl = null;
  }

  const onStorageChanged = (changes, areaName) => {
    if (areaName === "local" && changes[dragStorageKey]) {
      const nv = changes[dragStorageKey].newValue;
      dragOffset = nv ? { dx: Number(nv.dx) || 0, dy: Number(nv.dy) || 0 } : { dx: 0, dy: 0 };
      schedulePositionUpdate();
    }
  };
  chrome.storage.onChanged.addListener(onStorageChanged);

  const onRuntimeMessage = (msg, _sender, sendResponse) => {
    if (!msg || !msg.cmd) return false;

    if (msg.cmd === "overlay:show") {
      isOverlayActive = true;
      if (msg.settings) {
        currentSettings = { ...currentSettings, ...msg.settings };
      }
      if (typeof msg.hidden === "boolean") {
        isUserHidden = msg.hidden;
      }
      ensureOverlayDom().then(() => {
        applySettingsToDom();
        if (msg.status && !captionState.active) {
          handleBackendEvent({ type: "status", ...msg.status });
        }
        if (isYoutubeVideoPage()) {
          requestYoutubeCaptions(false);
        }
        sendResponse({ ok: true });
      });
      return true;
    }

    if (msg.cmd === "caption:reprobe") {
      if (isOverlayActive && isYoutubeVideoPage() && !captionState.active) {
        requestYoutubeCaptions(true);
      }
      sendResponse({ ok: true });
      return false;
    }

    if (msg.cmd === "overlay:hide") {
      destroyOverlay();
      sendResponse({ ok: true });
      return false;
    }

    if (msg.cmd === "overlay:visibility") {
      isUserHidden = Boolean(msg.hidden);
      applySettingsToDom();
      sendResponse({ ok: true });
      return false;
    }

    if (msg.cmd === "overlay:config") {
      if (msg.settings) {
        const prevLang = currentSettings.sourceLang;
        currentSettings = { ...currentSettings, ...msg.settings };
        applySettingsToDom();
        if (captionState.active) {
          renderCaptionFrame();
        }
        if (isYoutubeVideoPage() && msg.settings.sourceLang && msg.settings.sourceLang !== prevLang) {
          requestYoutubeCaptions(true);
        }
      }
      sendResponse({ ok: true });
      return false;
    }

    if (msg.cmd === "overlay:event") {
      if (isOverlayActive && msg.event) {
        handleBackendEvent(msg.event);
      }
      sendResponse({ ok: true });
      return false;
    }

    return false;
  };
  chrome.runtime.onMessage.addListener(onRuntimeMessage);

  window.__LAT_CONTENT_SCRIPT_DISPOSE__ = () => {
    try {
      window.removeEventListener(YT_HOOK_OUT_EVENT, onHookOutEvent);
      chrome.storage.onChanged.removeListener(onStorageChanged);
      chrome.runtime.onMessage.removeListener(onRuntimeMessage);
      destroyOverlay();
    } catch (_) {}
  };
})();
