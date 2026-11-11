(() => {
  if (window.__LAT_YT_CAPTION_HOOK_INSTALLED__) {
    return;
  }
  window.__LAT_YT_CAPTION_HOOK_INSTALLED__ = true;

  const EVENT_TO_ISOLATED = "__lat_yt_caption_out";
  const EVENT_FROM_ISOLATED = "__lat_yt_caption_in";
  const MAX_VIDEOS_IN_RAM = 8;

  // Map<videoId, Array<{videoId, lang, kind, isAsr, fmt, raw, capturedAt}>>
  const trackStore = new Map();
  let pendingFlash = null;

  function emitToContentScript(payload) {
    try {
      document.dispatchEvent(
        new CustomEvent(EVENT_TO_ISOLATED, {
          detail: JSON.stringify(payload)
        })
      );
    } catch (_) {}
  }

  function normalizeBaseLang(code) {
    let raw = String(code || "").trim().toLowerCase();
    if (!raw) return "";
    if (raw.startsWith("a.") && raw.length > 2) {
      raw = raw.slice(2);
    }
    return raw.split("-")[0].split("_")[0];
  }

  function isVietnameseLang(code) {
    const base = normalizeBaseLang(code);
    return base === "vi";
  }

  function getCurrentUrlVideoId() {
    try {
      const u = new URL(location.href);
      const v = u.searchParams.get("v");
      if (v) return v;
      const shortsMatch = u.pathname.match(/^\/shorts\/([a-zA-Z0-9_-]{6,})/);
      if (shortsMatch) return shortsMatch[1];
      const liveMatch = u.pathname.match(/^\/live\/([a-zA-Z0-9_-]{6,})/);
      if (liveMatch) return liveMatch[1];
    } catch (_) {}
    const player = getMoviePlayer();
    try {
      const vd = player && typeof player.getVideoData === "function" ? player.getVideoData() : null;
      if (vd && vd.video_id) return String(vd.video_id);
    } catch (_) {}
    return "";
  }

  function getMoviePlayer() {
    return document.getElementById("movie_player") || document.querySelector(".html5-video-player");
  }

  function isNonEmptyTimedTextBody(body) {
    const trimmed = String(body || "").trim();
    if (trimmed.length < 16) return false;
    if (trimmed.startsWith("{")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (!parsed || !Array.isArray(parsed.events)) return false;
        return parsed.events.some(
          (ev) => ev && Array.isArray(ev.segs) && ev.segs.some((s) => s && String(s.utf8 || "").trim())
        );
      } catch (_) {
        return false;
      }
    }
    if (trimmed.startsWith("<")) {
      return trimmed.includes("<p ") || trimmed.includes("<text ");
    }
    return trimmed.includes("-->");
  }

  function saveCapturedTrack(record) {
    const { videoId } = record;
    if (!trackStore.has(videoId)) {
      if (trackStore.size >= MAX_VIDEOS_IN_RAM) {
        const oldestKey = trackStore.keys().next().value;
        if (oldestKey) trackStore.delete(oldestKey);
      }
      trackStore.set(videoId, []);
    }
    const list = trackStore.get(videoId);
    const idx = list.findIndex(
      (item) => item.lang === record.lang && Boolean(item.isAsr) === Boolean(record.isAsr)
    );
    if (idx >= 0) {
      list[idx] = record;
    } else {
      list.push(record);
      if (list.length > 4) list.shift();
    }
  }

  function pickBestCapturedTrack(videoId, preferredLang = "en") {
    const list = trackStore.get(videoId);
    if (!list || list.length === 0) return null;
    const prefBase = normalizeBaseLang(preferredLang);

    let best = null;
    let bestScore = -1;
    for (const item of list) {
      if (isVietnameseLang(item.lang)) continue;
      let score = 10;
      const itemBase = normalizeBaseLang(item.lang);
      if (prefBase === "auto" || !prefBase || itemBase === prefBase) {
        score += 100;
      }
      if (!item.isAsr) {
        score += 30;
      }
      if (item.lang.toLowerCase() === prefBase) {
        score += 5;
      }
      if (score > bestScore) {
        bestScore = score;
        best = item;
      }
    }
    return best;
  }

  function isSubtitlesButtonPressed() {
    const btn = document.querySelector(".ytp-subtitles-button");
    return Boolean(btn && btn.getAttribute("aria-pressed") === "true");
  }

  function restorePlayerCcState(flashState) {
    if (!flashState) return;
    const applyRestore = () => {
      const player = getMoviePlayer();
      if (!player) return;
      try {
        if (flashState.wasCcOff) {
          if (typeof player.setOption === "function") {
            player.setOption("captions", "track", {});
          }
          if (isSubtitlesButtonPressed() && typeof player.toggleSubtitles === "function") {
            player.toggleSubtitles();
          }
        } else if (flashState.prevTrack && flashState.prevTrack.languageCode) {
          if (typeof player.setOption === "function") {
            player.setOption("captions", "track", flashState.prevTrack);
          }
        }
      } catch (_) {}
    };

    applyRestore();
    // Kiểm tra bồi sau 120ms để đảm bảo gạch đỏ nút CC trên YouTube chắc chắn tắt hẳn nếu trước đó CC đang tắt
    setTimeout(() => {
      if (flashState.wasCcOff && isSubtitlesButtonPressed()) {
        applyRestore();
      }
    }, 120);
  }

  function inspectTimedTextResponse(urlStr, bodyText) {
    if (!urlStr || !urlStr.includes("/api/timedtext")) return;
    let u;
    try {
      u = new URL(urlStr, location.origin);
    } catch (_) {
      return;
    }

    const videoId = u.searchParams.get("v") || "";
    const lang = u.searchParams.get("lang") || "";
    const tlang = u.searchParams.get("tlang") || "";
    const kind = u.searchParams.get("kind") || "";
    const fmt = u.searchParams.get("fmt") || "auto";

    if (!videoId || !lang) return;
    // Không lấy bản dịch máy tự động của Google (tlang=...) và không lấy phụ đề gốc đã là Tiếng Việt
    if (tlang) return;
    if (isVietnameseLang(lang)) return;
    if (!isNonEmptyTimedTextBody(bodyText)) return;

    const record = {
      videoId,
      lang,
      kind,
      isAsr: kind.toLowerCase() === "asr",
      fmt,
      raw: bodyText,
      capturedAt: Date.now()
    };
    saveCapturedTrack(record);

    if (pendingFlash && pendingFlash.videoId === videoId) {
      const flash = pendingFlash;
      pendingFlash = null;
      if (flash.timeoutId) clearTimeout(flash.timeoutId);
      restorePlayerCcState(flash);
      emitToContentScript({
        event: "acquire_result",
        reqId: flash.reqId,
        status: "ready",
        videoId,
        track: record
      });
      return;
    }

    emitToContentScript({
      event: "track_captured",
      videoId,
      track: record
    });
  }

  // 1. Hook window.fetch ngay từ document_start
  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (...args) {
      const req = args[0];
      const urlStr = typeof req === "string" ? req : (req && req.url) || "";
      const promise = origFetch.apply(this, args);
      if (urlStr && urlStr.includes("/api/timedtext")) {
        promise
          .then((resp) => {
            if (!resp || !resp.ok) return;
            try {
              resp
                .clone()
                .text()
                .then((txt) => inspectTimedTextResponse(urlStr, txt))
                .catch(() => {});
            } catch (_) {}
          })
          .catch(() => {});
      }
      return promise;
    };
  }

  // 2. Hook XMLHttpRequest (trình phát #movie_player của YouTube chủ yếu tải timedtext qua XHR)
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      this.__lat_timedtext_url = typeof url === "string" ? url : (url && String(url)) || "";
    } catch (_) {
      this.__lat_timedtext_url = "";
    }
    return origOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    if (this.__lat_timedtext_url && this.__lat_timedtext_url.includes("/api/timedtext")) {
      const urlStr = this.__lat_timedtext_url;
      this.addEventListener("load", function () {
        try {
          if (this.status < 200 || this.status >= 300) return;
          const rt = this.responseType || "";
          let body = "";
          if (rt === "" || rt === "text") {
            body = this.responseText || "";
          } else if (rt === "json" && this.response) {
            body = JSON.stringify(this.response);
          } else if (rt === "arraybuffer" && this.response) {
            body = new TextDecoder("utf-8").decode(this.response);
          }
          if (body) {
            inspectTimedTextResponse(urlStr, body);
          }
        } catch (_) {}
      });
    }
    return origSend.apply(this, args);
  };

  function getPlayerResponseForVideo(expectedVideoId) {
    const player = getMoviePlayer();
    if (player && typeof player.getPlayerResponse === "function") {
      try {
        const pr = player.getPlayerResponse();
        if (pr && pr.videoDetails && (!expectedVideoId || pr.videoDetails.videoId === expectedVideoId)) {
          return pr;
        }
      } catch (_) {}
    }
    const initPr = window.ytInitialPlayerResponse;
    if (initPr && initPr.videoDetails && (!expectedVideoId || initPr.videoDetails.videoId === expectedVideoId)) {
      return initPr;
    }
    return null;
  }

  function chooseCandidateTrackMeta(captionTracks, preferredLang = "en") {
    if (!Array.isArray(captionTracks) || captionTracks.length === 0) return null;
    const prefBase = normalizeBaseLang(preferredLang);

    let best = null;
    let bestScore = -1;
    for (const t of captionTracks) {
      if (!t || !t.languageCode) continue;
      if (isVietnameseLang(t.languageCode)) continue;
      if (t.isTranslatable === false && t.kind === "forced") continue;

      const tBase = normalizeBaseLang(t.languageCode);
      const isAsr = String(t.kind || "").toLowerCase() === "asr";
      let score = 10;
      if (prefBase === "auto" || !prefBase || tBase === prefBase) {
        score += 100;
      }
      if (!isAsr) {
        score += 30;
      }
      if (String(t.languageCode).toLowerCase() === prefBase) {
        score += 5;
      }
      if (score > bestScore) {
        bestScore = score;
        best = t;
      }
    }
    return best;
  }

  async function tryRecoverFromPerformanceEntries(videoId, preferredLang = "en") {
    if (!videoId || typeof origFetch !== "function" || typeof performance === "undefined") {
      return null;
    }
    let entries = [];
    try {
      entries = performance.getEntriesByType("resource") || [];
    } catch (_) {
      return null;
    }

    const candidates = [];
    for (let i = entries.length - 1; i >= 0; i--) {
      const name = entries[i] && entries[i].name;
      if (!name || !name.includes("/api/timedtext")) continue;
      try {
        const u = new URL(name, location.origin);
        if (u.origin !== location.origin) continue;
        const v = u.searchParams.get("v") || "";
        const lang = u.searchParams.get("lang") || "";
        const tlang = u.searchParams.get("tlang") || "";
        if (v !== videoId || !lang || tlang || isVietnameseLang(lang)) continue;
        candidates.push({
          url: u.toString(),
          lang,
          kind: u.searchParams.get("kind") || "",
          fmt: u.searchParams.get("fmt") || "auto"
        });
      } catch (_) {}
    }

    for (const cand of candidates.slice(0, 3)) {
      try {
        const resp = await origFetch(cand.url, { credentials: "same-origin", cache: "force-cache" });
        if (!resp || !resp.ok) continue;
        const txt = await resp.text();
        if (!isNonEmptyTimedTextBody(txt)) continue;
        const record = {
          videoId,
          lang: cand.lang,
          kind: cand.kind,
          isAsr: cand.kind.toLowerCase() === "asr",
          fmt: cand.fmt,
          raw: txt,
          capturedAt: Date.now()
        };
        saveCapturedTrack(record);
      } catch (_) {}
    }
    return pickBestCapturedTrack(videoId, preferredLang);
  }

  async function waitForMatchingPlayerResponse(videoId, maxWaitMs = 1200) {
    const deadline = Date.now() + maxWaitMs;
    while (Date.now() < deadline) {
      const pr = getPlayerResponseForVideo(videoId);
      if (pr) return pr;
      await new Promise((r) => setTimeout(r, 100));
    }
    return getPlayerResponseForVideo("");
  }

  async function acquireCaptionsForCurrentVideo(reqId, sourceLang = "en") {
    const videoId = getCurrentUrlVideoId();
    if (!videoId) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "unavailable",
        reason: "not_watch_page",
        videoId: ""
      });
      return;
    }

    // 1. Kiểm tra ngay trong RAM nếu YouTube đã tự tải gói timedtext từ lúc mở trang (0ms)
    const cached = pickBestCapturedTrack(videoId, sourceLang);
    if (cached) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "ready",
        videoId,
        track: cached
      });
      return;
    }

    // 1b. Khôi phục từ bộ đệm HTTP của trình duyệt nếu tab YouTube đã mở và bật sẵn CC từ trước khi nạp extension
    const perfRecovered = await tryRecoverFromPerformanceEntries(videoId, sourceLang);
    if (perfRecovered) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "ready",
        videoId,
        track: perfRecovered
      });
      return;
    }

    // 2. Đợi #movie_player đồng bộ metadata của đúng videoId hiện tại (quan trọng khi chuyển bài SPA)
    const pr = await waitForMatchingPlayerResponse(videoId, 1200);
    const recheckCached = pickBestCapturedTrack(videoId, sourceLang);
    if (recheckCached) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "ready",
        videoId,
        track: recheckCached
      });
      return;
    }

    if (pr && pr.videoDetails && pr.videoDetails.isLive) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "unavailable",
        reason: "live_stream",
        videoId
      });
      return;
    }

    const trackRenderer =
      pr &&
      pr.captions &&
      pr.captions.playerCaptionsTracklistRenderer &&
      pr.captions.playerCaptionsTracklistRenderer.captionTracks;

    const player = getMoviePlayer();
    let fallbackTracks = null;
    if (!trackRenderer && player && typeof player.getOption === "function") {
      try {
        fallbackTracks = player.getOption("captions", "tracklist");
      } catch (_) {}
    }

    const bestMeta = chooseCandidateTrackMeta(trackRenderer || fallbackTracks || [], sourceLang);
    if (!bestMeta || !player) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "unavailable",
        reason: "no_captions",
        videoId
      });
      return;
    }

    // 3. Cách A: Nháy nạp track trên #movie_player (~150-250ms) để lấy trọn file json3 rồi tự trả CC về trạng thái cũ
    if (pendingFlash && pendingFlash.timeoutId) {
      clearTimeout(pendingFlash.timeoutId);
      restorePlayerCcState(pendingFlash);
      pendingFlash = null;
    }

    let curTrack = null;
    try {
      curTrack = typeof player.getOption === "function" ? player.getOption("captions", "track") : null;
    } catch (_) {}
    const wasCcOff =
      (!curTrack || !curTrack.languageCode) && !isSubtitlesButtonPressed();

    const flashObj = {
      reqId,
      videoId,
      sourceLang,
      wasCcOff,
      prevTrack: curTrack && curTrack.languageCode ? { ...curTrack } : null,
      timeoutId: null
    };

    flashObj.timeoutId = setTimeout(() => {
      if (pendingFlash !== flashObj) return;
      pendingFlash = null;
      restorePlayerCcState(flashObj);
      const lateHit = pickBestCapturedTrack(videoId, sourceLang);
      if (lateHit) {
        emitToContentScript({
          event: "acquire_result",
          reqId,
          status: "ready",
          videoId,
          track: lateHit
        });
      } else {
        emitToContentScript({
          event: "acquire_result",
          reqId,
          status: "unavailable",
          reason: "flash_timeout",
          videoId
        });
      }
    }, 1800);

    pendingFlash = flashObj;

    try {
      if (typeof player.loadModule === "function") {
        player.loadModule("captions");
      }
      if (wasCcOff && typeof player.toggleSubtitles === "function" && !isSubtitlesButtonPressed()) {
        player.toggleSubtitles();
      }
      if (typeof player.setOption === "function") {
        if (!wasCcOff && curTrack && curTrack.languageCode === bestMeta.languageCode) {
          player.setOption("captions", "track", {});
        }
        const selector = { languageCode: bestMeta.languageCode };
        if (bestMeta.kind) selector.kind = bestMeta.kind;
        if (bestMeta.vssId) selector.vss_id = bestMeta.vssId;
        player.setOption("captions", "track", selector);
      }
    } catch (_) {
      clearTimeout(flashObj.timeoutId);
      if (pendingFlash === flashObj) pendingFlash = null;
      restorePlayerCcState(flashObj);
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "unavailable",
        reason: "player_api_error",
        videoId
      });
    }
  }

  document.addEventListener(EVENT_FROM_ISOLATED, (evt) => {
    if (!evt || !evt.detail) return;
    let msg;
    try {
      msg = typeof evt.detail === "string" ? JSON.parse(evt.detail) : evt.detail;
    } catch (_) {
      return;
    }
    if (!msg || !msg.cmd) return;
    if (msg.cmd === "acquire") {
      acquireCaptionsForCurrentVideo(msg.reqId || String(Date.now()), msg.sourceLang || "en");
    }
  });
})();
