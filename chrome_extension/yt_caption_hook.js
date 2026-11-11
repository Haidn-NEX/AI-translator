(() => {
  const HOOK_VERSION = 3;
  if (Number(window.__LAT_YT_HOOK_VERSION__ || 0) >= HOOK_VERSION) {
    return;
  }
  if (typeof window.__LAT_YT_HOOK_DISPOSE__ === "function") {
    try {
      window.__LAT_YT_HOOK_DISPOSE__();
    } catch (_) {}
  }
  window.__LAT_YT_CAPTION_HOOK_INSTALLED__ = true;
  window.__LAT_YT_HOOK_VERSION__ = HOOK_VERSION;

  const EVENT_TO_ISOLATED = "__lat_yt_caption_out";
  const EVENT_FROM_ISOLATED = "__lat_yt_caption_in";
  const MAX_VIDEOS_IN_RAM = 8;

  // Map<videoId, Array<{videoId, lang, kind, isAsr, fmt, raw, capturedAt}>>
  const trackStore = new Map();
  // Map<videoId, string> lưu token &pot= (Proof of Origin) bắt được từ gói timedtext của YouTube
  const potStore = new Map();
  const tlangStripInFlight = new Set();
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
    try {
      if (location.pathname && location.pathname.startsWith("/shorts/")) {
        const shortsPlayer = document.getElementById("shorts-player");
        if (shortsPlayer) return shortsPlayer;
      }
    } catch (_) {}
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

  function getSubtitlesButton() {
    const player = getMoviePlayer();
    if (player) {
      const btn = player.querySelector(".ytp-subtitles-button");
      if (btn) return btn;
    }
    return document.querySelector(".ytp-subtitles-button");
  }

  function isSubtitlesButtonPressed() {
    const btn = getSubtitlesButton();
    return Boolean(btn && btn.getAttribute("aria-pressed") === "true");
  }

  function isSubtitlesButtonAvailable() {
    const btn = getSubtitlesButton();
    if (!btn) return false;
    const ariaDisabled = String(btn.getAttribute("aria-disabled") || "").toLowerCase();
    if (ariaDisabled === "true") return false;
    const title = String(btn.getAttribute("title") || btn.getAttribute("aria-label") || "").toLowerCase();
    if (title.includes("unavailable") || title.includes("không khả dụng")) return false;
    return true;
  }

  function setSubtitlesEnabled(player, wantEnabled) {
    const target = Boolean(wantEnabled);
    const curPressed = isSubtitlesButtonPressed();
    if (curPressed === target) {
      if (!target && player && typeof player.setOption === "function") {
        try {
          player.setOption("captions", "track", {});
        } catch (_) {}
      }
      return;
    }

    // TUYỆT ĐỐI chỉ gọi MỘT thao tác bật/tắt duy nhất trong 1 nhịp JS (không gọi cả toggleSubtitles lẫn btn.click liền nhau)
    const btn = getSubtitlesButton();
    if (btn && typeof btn.click === "function") {
      try {
        btn.click();
        return;
      } catch (_) {}
    }
    if (player && typeof player.toggleSubtitles === "function") {
      try {
        player.toggleSubtitles();
      } catch (_) {}
    }
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
          if (isSubtitlesButtonPressed()) {
            setSubtitlesEnabled(player, false);
          }
        } else if (flashState.prevTrack && flashState.prevTrack.languageCode) {
          if (typeof player.setOption === "function") {
            player.setOption("captions", "track", flashState.prevTrack);
          }
        }
      } catch (_) {}
    };

    applyRestore();
    setTimeout(() => {
      if (flashState.wasCcOff && isSubtitlesButtonPressed()) {
        applyRestore();
      }
    }, 180);
    setTimeout(() => {
      if (flashState.wasCcOff && isSubtitlesButtonPressed()) {
        applyRestore();
      }
    }, 450);
  }

  function clearFlashTimers(flash) {
    if (!flash) return;
    if (flash.timeoutId) clearTimeout(flash.timeoutId);
    if (flash.stepTimerId) clearTimeout(flash.stepTimerId);
    if (flash.retryTimerId) clearTimeout(flash.retryTimerId);
    if (flash.langGraceTimerId) clearTimeout(flash.langGraceTimerId);
  }

  function resolvePendingFlash(flash, record) {
    if (!flash || pendingFlash !== flash) return;
    pendingFlash = null;
    clearFlashTimers(flash);
    restorePlayerCcState(flash);
    emitToContentScript({
      event: "acquire_result",
      reqId: flash.reqId,
      status: "ready",
      videoId: flash.videoId,
      track: record
    });
  }

  function triggerCleanFetchFromTlangUrl(u, videoId, lang) {
    if (typeof origFetch !== "function" || !videoId || !lang || isVietnameseLang(lang)) return;
    const key = `${videoId}:${lang}`;
    if (tlangStripInFlight.has(key)) return;
    tlangStripInFlight.add(key);
    try {
      const cleanU = new URL(u.toString());
      cleanU.searchParams.delete("tlang");
      cleanU.searchParams.set("fmt", "json3");
      const cleanUrl = cleanU.toString();
      origFetch(cleanUrl, { credentials: "same-origin" })
        .then((r) => (r && r.ok ? r.text() : ""))
        .then((txt) => {
          if (txt && typeof window.__LAT_YT_INSPECT_TIMEDTEXT__ === "function") {
            window.__LAT_YT_INSPECT_TIMEDTEXT__(cleanUrl, txt);
          }
        })
        .catch(() => {})
        .finally(() => {
          setTimeout(() => tlangStripInFlight.delete(key), 4000);
        });
    } catch (_) {
      tlangStripInFlight.delete(key);
    }
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
    const pot = u.searchParams.get("pot") || "";

    if (videoId && pot) {
      potStore.set(videoId, pot);
    }
    if (!videoId || !lang) return;

    // Nếu người dùng từng bật Auto-Translate trên YouTube (tlang=vi), tận dụng ngay URL đã ký (&pot=...)
    // nhưng gỡ bỏ tham số tlang để lấy phụ đề gốc chuẩn xác thay vì bỏ qua!
    if (tlang) {
      if (!isVietnameseLang(lang)) {
        triggerCleanFetchFromTlangUrl(u, videoId, lang);
      }
      return;
    }
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
      const targetBase = normalizeBaseLang(flash.targetLang || flash.sourceLang);
      const gotBase = normalizeBaseLang(lang);
      const isPreferredLang =
        !targetBase || targetBase === "auto" || gotBase === targetBase;

      if (isPreferredLang) {
        resolvePendingFlash(flash, record);
        return;
      }

      if (!flash.langGraceTimerId) {
        flash.langGraceTimerId = setTimeout(() => {
          if (pendingFlash !== flash) return;
          const best = pickBestCapturedTrack(videoId, flash.sourceLang) || record;
          resolvePendingFlash(flash, best);
        }, 550);
      }
      return;
    }

    // Chỉ phát sự kiện lên content_script nếu đúng là videoId đang mở trên URL chính (chống nhiễu khi rê chuột vào thumbnail sidebar)
    if (videoId === getCurrentUrlVideoId()) {
      emitToContentScript({
        event: "track_captured",
        videoId,
        track: record
      });
    }
  }

  window.__LAT_YT_INSPECT_TIMEDTEXT__ = inspectTimedTextResponse;

  // 1. Hook window.fetch ngay từ document_start (chỉ wrap 1 lần duy nhất)
  const origFetch = window.__LAT_YT_ORIG_FETCH__ || window.fetch;
  if (typeof origFetch === "function" && !window.__LAT_YT_ORIG_FETCH__) {
    window.__LAT_YT_ORIG_FETCH__ = origFetch;
  }
  if (typeof origFetch === "function" && !window.__LAT_YT_FETCH_HOOKED__) {
    window.__LAT_YT_FETCH_HOOKED__ = true;
    window.fetch = function (...args) {
      const req = args[0];
      let urlStr = "";
      if (typeof req === "string") {
        urlStr = req;
      } else if (req && typeof req === "object") {
        urlStr = req.url || req.href || "";
      }
      const promise = origFetch.apply(this, args);
      if (urlStr && urlStr.includes("/api/timedtext")) {
        promise
          .then((resp) => {
            if (!resp || !resp.ok) return;
            try {
              resp
                .clone()
                .text()
                .then((txt) => {
                  if (typeof window.__LAT_YT_INSPECT_TIMEDTEXT__ === "function") {
                    window.__LAT_YT_INSPECT_TIMEDTEXT__(urlStr, txt);
                  }
                })
                .catch(() => {});
            } catch (_) {}
          })
          .catch(() => {});
      }
      return promise;
    };
  }

  // 2. Hook XMLHttpRequest (trình phát #movie_player của YouTube chủ yếu tải timedtext qua XHR)
  if (!window.__LAT_YT_XHR_HOOKED__) {
    window.__LAT_YT_XHR_HOOKED__ = true;
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
            } else if (rt === "document" && this.responseXML && this.responseXML.documentElement) {
              body = this.responseXML.documentElement.outerHTML || "";
            } else if (rt === "blob" && this.response && typeof this.response.text === "function") {
              this.response
                .text()
                .then((txt) => {
                  if (txt && typeof window.__LAT_YT_INSPECT_TIMEDTEXT__ === "function") {
                    window.__LAT_YT_INSPECT_TIMEDTEXT__(urlStr, txt);
                  }
                })
                .catch(() => {});
              return;
            }
            if (body && typeof window.__LAT_YT_INSPECT_TIMEDTEXT__ === "function") {
              window.__LAT_YT_INSPECT_TIMEDTEXT__(urlStr, body);
            }
          } catch (_) {}
        });
      }
      return origSend.apply(this, args);
    };
  }

  function hasCaptionTracksInPr(pr) {
    const list =
      pr &&
      pr.captions &&
      pr.captions.playerCaptionsTracklistRenderer &&
      pr.captions.playerCaptionsTracklistRenderer.captionTracks;
    return Array.isArray(list) && list.length > 0;
  }

  function getPlayerResponseForVideo(expectedVideoId) {
    const candidates = [];
    const player = getMoviePlayer();
    if (player && typeof player.getPlayerResponse === "function") {
      try {
        const pr = player.getPlayerResponse();
        if (pr) candidates.push(pr);
      } catch (_) {}
    }

    try {
      const flexy = document.querySelector("ytd-watch-flexy");
      if (flexy) {
        if (flexy.playerData) candidates.push(flexy.playerData);
        if (flexy.__data && flexy.__data.playerData) candidates.push(flexy.__data.playerData);
      }
    } catch (_) {}

    try {
      const activeReel = document.querySelector("ytd-reel-video-renderer[is-active], ytd-shorts");
      if (activeReel && activeReel.__data && activeReel.__data.playerResponse) {
        candidates.push(activeReel.__data.playerResponse);
      }
    } catch (_) {}

    try {
      const appEl = document.querySelector("ytd-app");
      if (appEl && appEl.__data && appEl.__data.data && appEl.__data.data.playerResponse) {
        candidates.push(appEl.__data.data.playerResponse);
      }
    } catch (_) {}

    if (window.ytInitialPlayerResponse) {
      candidates.push(window.ytInitialPlayerResponse);
    }

    let firstMatching = null;
    for (const pr of candidates) {
      if (!pr || !pr.videoDetails) continue;
      if (expectedVideoId && pr.videoDetails.videoId !== expectedVideoId) continue;
      if (!firstMatching) firstMatching = pr;
      if (hasCaptionTracksInPr(pr)) {
        return pr;
      }
    }
    return firstMatching;
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
        const pot = u.searchParams.get("pot") || "";
        if (v && pot) {
          potStore.set(v, pot);
        }
        if (v !== videoId || !lang || isVietnameseLang(lang)) continue;
        if (tlang) {
          u.searchParams.delete("tlang");
          u.searchParams.set("fmt", "json3");
        }
        candidates.push({
          url: u.toString(),
          lang,
          kind: u.searchParams.get("kind") || "",
          fmt: u.searchParams.get("fmt") || "auto",
          wasTlang: Boolean(tlang)
        });
      } catch (_) {}
    }

    for (const cand of candidates.slice(0, 3)) {
      try {
        const resp = await origFetch(cand.url, {
          credentials: "same-origin",
          cache: cand.wasTlang ? "default" : "force-cache"
        });
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

  async function tryDirectFetchFromBaseUrl(videoId, candidateTracks, preferredLang = "en") {
    if (!videoId || typeof origFetch !== "function" || !Array.isArray(candidateTracks) || !candidateTracks.length) {
      return null;
    }
    const primary = chooseCandidateTrackMeta(candidateTracks, preferredLang);
    if (!primary) return null;

    const ordered = [primary];
    for (const t of candidateTracks) {
      if (t && t !== primary && t.baseUrl && !isVietnameseLang(t.languageCode) && ordered.length < 2) {
        ordered.push(t);
      }
    }

    const knownPot = potStore.get(videoId) || "";
    for (const meta of ordered) {
      if (!meta || !meta.baseUrl) continue;
      const urlsToTry = [];
      try {
        const uJson = new URL(meta.baseUrl, location.origin);
        uJson.searchParams.delete("tlang");
        uJson.searchParams.set("fmt", "json3");
        if (knownPot && !uJson.searchParams.get("pot")) {
          uJson.searchParams.set("pot", knownPot);
        }
        urlsToTry.push({ url: uJson.toString(), fmt: "json3" });

        const uRaw = new URL(meta.baseUrl, location.origin);
        uRaw.searchParams.delete("tlang");
        if (knownPot && !uRaw.searchParams.get("pot")) {
          uRaw.searchParams.set("pot", knownPot);
        }
        urlsToTry.push({ url: uRaw.toString(), fmt: uRaw.searchParams.get("fmt") || "auto" });
      } catch (_) {}

      for (const item of urlsToTry) {
        const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
        const timer = ctrl ? setTimeout(() => ctrl.abort(), 1100) : null;
        try {
          const resp = await origFetch(item.url, {
            credentials: "same-origin",
            signal: ctrl ? ctrl.signal : undefined
          });
          if (!resp || !resp.ok) continue;
          const txt = await resp.text();
          if (!isNonEmptyTimedTextBody(txt)) continue;
          const kind = String(meta.kind || "");
          const record = {
            videoId,
            lang: meta.languageCode || preferredLang || "en",
            kind,
            isAsr: kind.toLowerCase() === "asr",
            fmt: item.fmt,
            raw: txt,
            capturedAt: Date.now()
          };
          saveCapturedTrack(record);
          return record;
        } catch (_) {
        } finally {
          if (timer) clearTimeout(timer);
        }
      }
    }
    return null;
  }

  async function waitForMatchingPlayerResponse(videoId, maxWaitMs = 1200) {
    const deadline = Date.now() + maxWaitMs;
    let fallbackPr = null;
    while (Date.now() < deadline) {
      const pr = getPlayerResponseForVideo(videoId);
      if (pr) {
        if (hasCaptionTracksInPr(pr)) return pr;
        fallbackPr = pr;
      }
      const player = getMoviePlayer();
      try {
        const vd = player && typeof player.getVideoData === "function" ? player.getVideoData() : null;
        if (vd && vd.video_id === videoId && typeof player.getOption === "function") {
          const tl = player.getOption("captions", "tracklist");
          if (Array.isArray(tl) && tl.length > 0) {
            return {
              videoDetails: { videoId, isLive: false },
              captions: { playerCaptionsTracklistRenderer: { captionTracks: tl } }
            };
          }
        }
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 80));
    }
    return fallbackPr || (videoId ? null : getPlayerResponseForVideo(""));
  }

  function isCaptionsModuleReady(player) {
    if (!player) return false;
    try {
      if (typeof player.getOption === "function") {
        const tl = player.getOption("captions", "tracklist");
        if (Array.isArray(tl) && tl.length > 0) return true;
      }
      if (typeof player.getOptions === "function") {
        const opts = player.getOptions();
        if (Array.isArray(opts) && opts.includes("captions")) return true;
      }
    } catch (_) {}
    return false;
  }

  async function waitForCaptionsModuleReady(player, maxWaitMs = 1200) {
    if (!player) return false;
    if (isCaptionsModuleReady(player)) return true;
    try {
      if (typeof player.loadModule === "function") {
        player.loadModule("captions");
      }
    } catch (_) {}

    const hasGetOptions = typeof player.getOptions === "function";
    const effectiveWait = hasGetOptions ? maxWaitMs : Math.min(maxWaitMs, 150);
    const deadline = Date.now() + effectiveWait;
    while (Date.now() < deadline) {
      if (isCaptionsModuleReady(player)) return true;
      await new Promise((r) => setTimeout(r, 50));
    }
    return isCaptionsModuleReady(player);
  }

  function buildTrackSelector(meta, includeVssId = true) {
    if (!meta || !meta.languageCode) return null;
    const selector = { languageCode: meta.languageCode };
    if (meta.kind) selector.kind = meta.kind;
    if (meta.name) selector.name = meta.name;
    if (includeVssId) {
      const vss = meta.vss_id || meta.vssId;
      if (vss) selector.vss_id = vss;
    }
    return selector;
  }

  function selectDesiredTrackOnPlayer(player, fallbackTracks, sourceLang, fallbackMeta) {
    if (!player || typeof player.setOption !== "function") return;
    try {
      player.setOption("captions", "translationLanguage", {});
    } catch (_) {}
    try {
      let liveList = null;
      if (typeof player.getOption === "function") {
        liveList = player.getOption("captions", "tracklist");
      }
      const pool =
        Array.isArray(liveList) && liveList.length > 0
          ? liveList
          : Array.isArray(fallbackTracks) && fallbackTracks.length > 0
            ? fallbackTracks
            : [];
      const chosenFromLive = chooseCandidateTrackMeta(pool, sourceLang) || fallbackMeta;
      if (chosenFromLive) {
        const sel = buildTrackSelector(chosenFromLive, true);
        if (sel) {
          player.setOption("captions", "track", sel);
        }
      }
    } catch (_) {}
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

    // 2. Đợi #movie_player / Polymer đồng bộ metadata của đúng videoId hiện tại (quan trọng khi chuyển bài SPA)
    const pr = await waitForMatchingPlayerResponse(videoId, 1200);
    if (getCurrentUrlVideoId() !== videoId) return;

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

    // 2b. Tầng 2 (Direct baseUrl Fetch): Thử tải trực tiếp từ baseUrl trong captionTracks trước khi đụng vào nút CC
    if (Array.isArray(trackRenderer) && trackRenderer.length > 0) {
      const directHit = await tryDirectFetchFromBaseUrl(videoId, trackRenderer, sourceLang);
      if (getCurrentUrlVideoId() !== videoId) return;
      if (directHit) {
        emitToContentScript({
          event: "acquire_result",
          reqId,
          status: "ready",
          videoId,
          track: directHit
        });
        return;
      }
    }

    const player = getMoviePlayer();
    if (!player) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "unavailable",
        reason: "no_player",
        videoId
      });
      return;
    }

    // Đảm bảo module "captions" của #movie_player đã nạp trước khi đọc tracklist / gọi setOption
    await waitForCaptionsModuleReady(player, 900);
    if (getCurrentUrlVideoId() !== videoId) return;

    const postModuleCached = pickBestCapturedTrack(videoId, sourceLang);
    if (postModuleCached) {
      emitToContentScript({
        event: "acquire_result",
        reqId,
        status: "ready",
        videoId,
        track: postModuleCached
      });
      return;
    }

    let liveTracks = null;
    if (typeof player.getOption === "function") {
      try {
        liveTracks = player.getOption("captions", "tracklist");
      } catch (_) {}
    }

    const candidateTracks =
      Array.isArray(liveTracks) && liveTracks.length > 0
        ? liveTracks
        : Array.isArray(trackRenderer) && trackRenderer.length > 0
          ? trackRenderer
          : [];

    const bestMeta = chooseCandidateTrackMeta(candidateTracks, sourceLang);
    const canFlashButton = isSubtitlesButtonAvailable() || typeof player.toggleSubtitles === "function";
    const confirmedNoCaptions =
      Boolean(pr && pr.videoDetails && pr.videoDetails.videoId === videoId && !hasCaptionTracksInPr(pr)) &&
      (!Array.isArray(liveTracks) || liveTracks.length === 0);

    // Chỉ kết luận no_captions nếu cả metadata lẫn nút CC đều xác nhận không có phụ đề (hoặc chỉ toàn phụ đề Tiếng Việt)
    if (!bestMeta) {
      if (candidateTracks.length > 0 || confirmedNoCaptions || !canFlashButton) {
        emitToContentScript({
          event: "acquire_result",
          reqId,
          status: "unavailable",
          reason: "no_captions",
          videoId
        });
        return;
      }
    }

    // 3. Tầng 3 (Sequenced Flash-Fetch trên #movie_player): Bật CC đúng 1 nhịp để #movie_player sinh &pot= và bắn XHR timedtext
    if (pendingFlash) {
      clearFlashTimers(pendingFlash);
      restorePlayerCcState(pendingFlash);
      pendingFlash = null;
    }

    let curTrack = null;
    try {
      curTrack = typeof player.getOption === "function" ? player.getOption("captions", "track") : null;
    } catch (_) {}
    const wasCcOff =
      (!curTrack || !curTrack.languageCode) && !isSubtitlesButtonPressed();

    const prefBase = normalizeBaseLang(sourceLang);
    const hasTargetLangInList =
      prefBase === "auto" ||
      !prefBase ||
      !candidateTracks.length ||
      candidateTracks.some(
        (t) => t && t.languageCode && !isVietnameseLang(t.languageCode) && normalizeBaseLang(t.languageCode) === prefBase
      );

    const flashObj = {
      reqId,
      videoId,
      sourceLang,
      targetLang: bestMeta ? bestMeta.languageCode : sourceLang,
      hasTargetLangInList,
      wasCcOff,
      prevTrack: curTrack && curTrack.languageCode ? { ...curTrack } : null,
      timeoutId: null,
      stepTimerId: null,
      retryTimerId: null,
      langGraceTimerId: null
    };

    flashObj.retryTimerId = setTimeout(async () => {
      if (pendingFlash !== flashObj) return;
      try {
        setSubtitlesEnabled(player, true);
        selectDesiredTrackOnPlayer(player, candidateTracks, sourceLang, bestMeta);
        if (Array.isArray(trackRenderer) && trackRenderer.length > 0 && potStore.has(videoId)) {
          const potHit = await tryDirectFetchFromBaseUrl(videoId, trackRenderer, sourceLang);
          if (potHit && pendingFlash === flashObj) {
            resolvePendingFlash(flashObj, potHit);
          }
        }
      } catch (_) {}
    }, 850);

    flashObj.timeoutId = setTimeout(() => {
      if (pendingFlash !== flashObj) return;
      pendingFlash = null;
      clearFlashTimers(flashObj);
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
    }, 2600);

    pendingFlash = flashObj;

    try {
      if (wasCcOff) {
        // Nhịp 1 (0ms): Chỉ bật CC 1 lần duy nhất (không gọi setOption đè lên khi tracklist chưa khởi tạo!)
        setSubtitlesEnabled(player, true);
        // Nhịp 2 (180ms): Khi module captions đã thức dậy và nạp xong tracklist, chọn đúng ngôn ngữ gốc và xóa tlang
        flashObj.stepTimerId = setTimeout(() => {
          if (pendingFlash !== flashObj) return;
          selectDesiredTrackOnPlayer(player, candidateTracks, sourceLang, bestMeta);
        }, 180);
      } else {
        // Nếu CC đang bật sẵn nhưng chưa bắt được gói (hoặc đang kẹt ở ngôn ngữ khác/tlang), reset nhẹ track để ép tải lại
        try {
          if (typeof player.setOption === "function") {
            player.setOption("captions", "translationLanguage", {});
            player.setOption("captions", "track", {});
          }
        } catch (_) {}
        flashObj.stepTimerId = setTimeout(() => {
          if (pendingFlash !== flashObj) return;
          setSubtitlesEnabled(player, true);
          selectDesiredTrackOnPlayer(player, candidateTracks, sourceLang, bestMeta);
        }, 70);
      }
    } catch (_) {
      clearFlashTimers(flashObj);
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

  const onFromIsolated = (evt) => {
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
  };

  document.addEventListener(EVENT_FROM_ISOLATED, onFromIsolated);
  window.__LAT_YT_HOOK_DISPOSE__ = () => {
    document.removeEventListener(EVENT_FROM_ISOLATED, onFromIsolated);
    if (pendingFlash) {
      clearFlashTimers(pendingFlash);
      pendingFlash = null;
    }
  };
})();
