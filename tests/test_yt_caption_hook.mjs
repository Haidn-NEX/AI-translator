import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HOOK_PATH = path.resolve(__dirname, "..", "chrome_extension", "yt_caption_hook.js");
const HOOK_CODE = fs.readFileSync(HOOK_PATH, "utf-8");

const SAMPLE_JSON3_EN = JSON.stringify({
  events: [
    {
      tStartMs: 1000,
      dDurationMs: 2500,
      wWinId: 1,
      segs: [
        { utf8: "We" },
        { utf8: " need", tOffsetMs: 200 },
        { utf8: " Scylla.", tOffsetMs: 500 }
      ]
    }
  ]
});

const SAMPLE_JSON3_ES = JSON.stringify({
  events: [
    {
      tStartMs: 1000,
      dDurationMs: 2500,
      segs: [{ utf8: "Necesitamos a Scylla." }]
    }
  ]
});

function createBrowserEnv({
  videoId = "mQn5IEyJ_hs",
  pathname = "/watch",
  search = `?v=${videoId}`,
  captionTracks = [{ languageCode: "en", kind: "asr", vssId: "a.en" }],
  isLive = false,
  asyncModuleLoadMs = 0,
  requireButtonClick = false,
  flashWrongLangFirst = false
} = {}) {
  const listeners = new Map();
  let ccPressed = false;
  let moduleLoaded = asyncModuleLoadMs === 0;
  let currentTrack = null;
  let loadModuleCalls = 0;

  const env = {
    setTimeout,
    clearTimeout,
    Date,
    Math,
    URL,
    TextDecoder,
    JSON,
    Array,
    Map,
    Set,
    String,
    Number,
    Boolean,
    Object,
    Promise,
    Error,
    console
  };

  class FakeCustomEvent {
    constructor(type, init = {}) {
      this.type = type;
      this.detail = init.detail;
    }
  }

  class FakeXHR {
    constructor() {
      this._listeners = new Map();
      this.status = 0;
      this.responseType = "";
      this.responseText = "";
      this.response = null;
    }
    addEventListener(type, fn) {
      if (!this._listeners.has(type)) this._listeners.set(type, []);
      this._listeners.get(type).push(fn);
    }
    open(method, url) {
      this._url = url;
    }
    send() {
      if (this._mockBody !== undefined) {
        this.status = 200;
        this.responseText = this._mockBody;
        for (const fn of this._listeners.get("load") || []) {
          fn.call(this);
        }
      }
    }
  }

  function dispatchTimedTextXHR(lang, kind, body) {
    const xhr = new env.XMLHttpRequest();
    const url = `https://www.youtube.com/api/timedtext?v=${videoId}&lang=${lang}${
      kind ? `&kind=${kind}` : ""
    }&fmt=json3&pot=mock_pot_token`;
    xhr.open("GET", url);
    xhr._mockBody = body;
    xhr.send();
  }

  const subtitlesBtn = {
    getAttribute(attr) {
      if (attr === "aria-pressed") return ccPressed ? "true" : "false";
      return null;
    },
    click() {
      ccPressed = !ccPressed;
      if (ccPressed && flashWrongLangFirst) {
        setTimeout(() => dispatchTimedTextXHR("es", "", SAMPLE_JSON3_ES), 20);
      } else if (ccPressed && requireButtonClick) {
        setTimeout(() => dispatchTimedTextXHR("en", "asr", SAMPLE_JSON3_EN), 30);
      }
    }
  };

  const player = {
    getVideoData() {
      return { video_id: videoId };
    },
    getPlayerResponse() {
      return {
        videoDetails: { videoId, isLive },
        captions: captionTracks
          ? { playerCaptionsTracklistRenderer: { captionTracks } }
          : undefined
      };
    },
    getOptions() {
      return moduleLoaded ? ["captions", "cc"] : ["quality"];
    },
    loadModule(name) {
      if (name === "captions") {
        loadModuleCalls += 1;
        if (!moduleLoaded) {
          setTimeout(() => {
            moduleLoaded = true;
          }, asyncModuleLoadMs);
        }
      }
    },
    getOption(mod, key) {
      if (mod !== "captions" || !moduleLoaded) return undefined;
      if (key === "tracklist") {
        return (captionTracks || []).map((t) => ({
          languageCode: t.languageCode,
          kind: t.kind,
          vss_id: t.vssId
        }));
      }
      if (key === "track") {
        return currentTrack;
      }
      return undefined;
    },
    setOption(mod, key, val) {
      if (mod !== "captions" || key !== "track" || !moduleLoaded) return;
      if (!val || !val.languageCode) {
        currentTrack = null;
        ccPressed = false;
        return;
      }
      currentTrack = { ...val };
      if (requireButtonClick && !ccPressed) {
        return;
      }
      ccPressed = true;
      const delay = flashWrongLangFirst && val.languageCode === "en" ? 90 : 25;
      const body = val.languageCode === "es" ? SAMPLE_JSON3_ES : SAMPLE_JSON3_EN;
      setTimeout(() => dispatchTimedTextXHR(val.languageCode, val.kind || "", body), delay);
    },
    toggleSubtitles: requireButtonClick
      ? undefined
      : () => {
          ccPressed = !ccPressed;
        },
    querySelector(sel) {
      if (sel === ".ytp-subtitles-button") return subtitlesBtn;
      return null;
    }
  };

  const document = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    dispatchEvent(evt) {
      for (const fn of [...(listeners.get(evt.type) || [])]) {
        fn(evt);
      }
    },
    getElementById(id) {
      if (pathname.startsWith("/shorts/") && id === "shorts-player") return player;
      if (id === "movie_player" && !pathname.startsWith("/shorts/")) return player;
      return null;
    },
    querySelector(sel) {
      if (sel === ".ytp-subtitles-button") return subtitlesBtn;
      if (sel === ".html5-video-player") return player;
      return null;
    }
  };

  const location = {
    origin: "https://www.youtube.com",
    hostname: "www.youtube.com",
    pathname,
    href: `https://www.youtube.com${pathname}${search}`
  };

  const performance = {
    getEntriesByType() {
      return [];
    }
  };

  const nativeFetch = async () => ({ ok: false });

  env.window = env;
  env.document = document;
  env.location = location;
  env.performance = performance;
  env.CustomEvent = FakeCustomEvent;
  env.XMLHttpRequest = FakeXHR;
  env.fetch = nativeFetch;

  const context = vm.createContext(env);
  vm.runInContext(HOOK_CODE, context);

  function acquire(reqId, sourceLang = "en") {
    return new Promise((resolve) => {
      const onOut = (evt) => {
        const data = JSON.parse(evt.detail);
        if (data.event === "acquire_result" && data.reqId === reqId) {
          document.removeEventListener("__lat_yt_caption_out", onOut);
          resolve(data);
        }
      };
      document.addEventListener("__lat_yt_caption_out", onOut);
      document.dispatchEvent(
        new FakeCustomEvent("__lat_yt_caption_in", {
          detail: JSON.stringify({ cmd: "acquire", reqId, sourceLang })
        })
      );
    });
  }

  return {
    context,
    acquire,
    getCcPressed: () => ccPressed,
    getCurrentTrack: () => currentTrack,
    getLoadModuleCalls: () => loadModuleCalls
  };
}

async function runAllTests() {
  // Test 1: Async loadModule("captions") (160ms delay) when CC is OFF
  {
    const sim = createBrowserEnv({
      videoId: "mQn5IEyJ_hs",
      asyncModuleLoadMs: 160
    });
    const res = await sim.acquire("req-async-mod", "en");
    assert.equal(res.status, "ready", "Async loadModule('captions') must capture timedtext");
    assert.equal(res.track.lang, "en");
    assert.equal(res.track.isAsr, true);
    assert.equal(sim.getLoadModuleCalls() >= 1, true);
    assert.equal(sim.getCcPressed(), false, "CC button must be restored to OFF after flash");
    assert.equal(sim.getCurrentTrack(), null, "Caption track must be cleared back to OFF");
  }

  // Test 2: Fallback click on .ytp-subtitles-button when setOption requires CC button toggle
  {
    const sim = createBrowserEnv({
      videoId: "kSUK6-DOEts",
      requireButtonClick: true
    });
    const res = await sim.acquire("req-btn-fallback", "en");
    assert.equal(res.status, "ready", "Fallback .ytp-subtitles-button click must capture timedtext");
    assert.equal(res.track.lang, "en");
    assert.equal(sim.getCcPressed(), false, "CC button must be restored to OFF after fallback click");
  }

  // Test 3: Wrong default track ('es') arrives first during flash, preferred 'en' arrives 90ms later
  {
    const sim = createBrowserEnv({
      videoId: "prefLang123",
      captionTracks: [
        { languageCode: "es", kind: "", vssId: ".es" },
        { languageCode: "en", kind: "asr", vssId: "a.en" }
      ],
      flashWrongLangFirst: true
    });
    const res = await sim.acquire("req-pref-lang", "en");
    assert.equal(res.status, "ready");
    assert.equal(res.track.lang, "en", "Must prefer requested 'en' track over early 'es' flash");
    assert.equal(sim.getCcPressed(), false);
  }

  // Test 4: Live stream & genuine no_captions video must report unavailable immediately
  {
    const liveSim = createBrowserEnv({ videoId: "live001", isLive: true });
    const liveRes = await liveSim.acquire("req-live", "en");
    assert.equal(liveRes.status, "unavailable");
    assert.equal(liveRes.reason, "live_stream");

    const noCapSim = createBrowserEnv({ videoId: "nocap001", captionTracks: [] });
    const noCapRes = await noCapSim.acquire("req-nocap", "en");
    assert.equal(noCapRes.status, "unavailable");
    assert.equal(noCapRes.reason, "no_captions");
  }

  // Test 5: Re-injecting hook on same version does not duplicate events or double-wrap XHR/fetch
  {
    const sim = createBrowserEnv({ videoId: "shorts001", pathname: "/shorts/shorts001", search: "" });
    const fetchBefore = sim.context.fetch;
    const xhrSendBefore = sim.context.XMLHttpRequest.prototype.send;
    vm.runInContext(HOOK_CODE, sim.context);
    assert.equal(sim.context.fetch, fetchBefore, "window.fetch must not be double-wrapped");
    assert.equal(
      sim.context.XMLHttpRequest.prototype.send,
      xhrSendBefore,
      "XMLHttpRequest.prototype.send must not be double-wrapped"
    );
    const res = await sim.acquire("req-shorts", "en");
    assert.equal(res.status, "ready");
    assert.equal(res.videoId, "shorts001");
  }

  console.log("ALL yt_caption_hook.js tests PASSED (5/5)!");
}

runAllTests().catch((err) => {
  console.error(err);
  process.exit(1);
});

