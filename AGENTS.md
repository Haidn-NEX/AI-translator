# AGENTS.md — Universal AI Agent Instructions

> **Compatible with:** Claude Code, Google Antigravity, Cursor, Windsurf, OpenAI Codex, GitHub Copilot, OpenCode.

## 1. Read First (Context Loading Order)

Before making any code changes, read the harness documentation in this order:
1. [`PROGRESS.md`](./PROGRESS.md) — **Session Handoff & Current State:** Exact progress, hardware/environment status, resolved issues, and next steps to resume.
2. [`CLAUDE.md`](./CLAUDE.md) — Core summary, repository map, commands, and the 6 mandatory architectural invariants.
3. [`ARCHITECTURE.md`](./ARCHITECTURE.md) — End-to-end data flow, threading model, and state/queue/WebSocket interactions.
4. [`docs/INTERPRETER_SUMMARY_SPEC.md`](./docs/INTERPRETER_SUMMARY_SPEC.md) — When touching `interpreter_summary.py`, `SummaryPanelWindow` in `overlay_ui.py`, `_llm_lock` / `OLLAMA_NUM_CTX` in `ai_engine.py`, or summary controls in `main.py` / `chrome_extension/`.
5. [`docs/CHROME_EXTENSION_SPEC.md`](./docs/CHROME_EXTENSION_SPEC.md) — When touching `incoming_pipeline.py`, `browser_bridge.py`, `bridge_server.py`, `native_host/`, or `chrome_extension/`.
6. [`docs/HARDWARE_AND_VRAM.md`](./docs/HARDWARE_AND_VRAM.md) — When touching `hardware_profile.py` or `ai_engine.py`.
7. [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md) — When adding features, UI controls, audio sources, languages, or TTS/translation providers.

---

## 2. Execution Flow Summary (Mental Model for Agents)

### Startup Flow (`main.py` & `bridge_server.py`)
1. `QApplication` (desktop `main.py`) or `QCoreApplication` (headless `bridge_server.py`) initializes the Qt event loop.
2. `detect_hardware_profile()` inspects OS and GPU (`torch.cuda` / `platform.machine()`) and returns an immutable `SystemProfile`.
3. `LocalAIEngine(profile)` is created (lazy-loads STT/NLLB models on first use in `main.py`, or warms up in background via `BridgePreloader` in `bridge_server.py`) and loads any persisted project glossary from `custom_glossary.txt` via `set_custom_glossary()`.
4. `BrowserBridgeServer(engine, port=47863)` starts an `aiohttp` HTTP/WebSocket server on `127.0.0.1:47863` (guarded by `bridge_token.txt` and `Origin: chrome-extension://...`) so the Chrome Extension can connect in either **Integrated mode** (`main.py`) or **Headless mode** (`bridge_server.py` / Native Host `com.local_translator.bridge`).
5. In desktop mode (`main.py`), `VirtualMicTTSRouter()`, `SubtitleOverlayWindow`, `ControlBar` (with `WDA_EXCLUDEFROMCAPTURE` stealth), `OutgoingVoiceWorker`, and `pynput.keyboard.Listener` (`F8` Push-to-Talk & `Esc` cancel) start immediately on launch.
6. When the user clicks **`▶ Bật Dịch Chiều Nghe`** (or presses **`Alt+Shift+T`** in Chrome), incoming audio is captured exclusively from either System Loopback (`SystemAudioVADWorker`) or Chrome Tab (`TabAudioVADWorker`).

### Three Pipelines Sharing One Engine (`0 MB` Extra VRAM)
```text
[Chiều Nghe Hệ Thống / Desktop Incoming]
System Speaker Loopback ──► _StreamingResampler + _StreamingAGC + Silero VAD (16kHz, 512 samples)
  ──► incoming_queue ──► IncomingInferenceWorker ──► LocalAIEngine (gpu_lock + custom_glossary)
  ──► Whisper STT (Foreign) + Ollama/NLLB (to VI) ──► SubtitleOverlayWindow (src_label + vi_label)
                                                  ──► MeetingTranscriptLogger (meeting_logs/meeting_*.md)

[Chiều Nghe Tab Chrome / Browser Bridge (Alt+Shift+T)]
├─ Fast-Path (YouTube Caption Mode): yt_caption_hook.js (/api/timedtext json3/srv3/vtt + Option A flash-fetch)
│    ──► ws://127.0.0.1:47863/ws (captions_load) ──► CaptionTranslationWorker (+90s sliding batch + caption_cache/)
│    ──► LocalAIEngine.translate_caption_batch() ──► Content Script (0ms video.currentTime sync) + SummaryWorker/Logger (watched cues only)
└─ Audio Fallback (tabCapture): Offscreen AudioWorklet (48kHz -> 40ms Int16 PCM + passthrough to speakers)
     ──► ws://127.0.0.1:47863/ws ──► TabAudioVADWorker (_StreamingResampler + _StreamingAGC + Silero VAD)
     ──► bridge_queue ──► IncomingInferenceWorker ──► LocalAIEngine (gpu_lock + custom_glossary)
     ──► JSON {source, result} over WS ──► Content Script (Shadow DOM + SubtitlePacer over <video>)
                                       ──► MeetingTranscriptLogger (meeting_logs/video_*.md)

[Chiều Nói / Outgoing (Global Hotkeys: F8 Speak | Esc Cancel)]
Physical Mic (while F8 held) ──► MicPushToTalkRecorder ──► outgoing_queue (on F8 release)
  ──► OutgoingVoiceWorker ──► LocalAIEngine (gpu_lock + custom_glossary)
  ──► Whisper STT (force_lang="vi") + Ollama/NLLB (VI to EN)
  ──► 1.5s Safety Preview Gate (Esc cancels before or during TTS)
  ──► VirtualMicTTSRouter (edge-tts / pyttsx3) ──► Virtual Audio Cable (Zoom/Meet Mic)
                                               ──► SubtitleOverlayWindow + MeetingTranscriptLogger
```

---

## 3. Agent Coding Guidelines & Guardrails

- **Language & Localization:** Keep user-facing UI labels and status messages in Vietnamese (consistent with `overlay_ui.py`, `main.py`, and `chrome_extension/`).
- **Sample Rate Contract:** `SystemAudioVADWorker`, `TabAudioVADWorker`, and `MicPushToTalkRecorder` all normalize audio to **`16000 Hz`, mono (`1` channel), `np.float32` in range `[-1.0, 1.0]`** before pushing `np.ndarray` buffers into `incoming_queue`, `bridge_queue`, or `outgoing_queue`. Never push raw `int16` or stereo buffers into inference queues.
- **No Blocking on Qt GUI Thread:** Never run model loading, HTTP calls (`requests.post` to Ollama), TTS synthesis, or `sd.play(..., blocking=True)` on the main Qt GUI thread. Keep all heavy I/O and inference inside `IncomingInferenceWorker` or `OutgoingVoiceWorker`.
- **Graceful Degradation:**
  - If `edge-tts` fails (e.g., offline / no internet), `VirtualMicTTSRouter.speak_to_virtual_mic()` automatically falls back to `_synthesize_offline_tts()` (`pyttsx3`). Preserve this try/except fallback.
  - If Ollama is unreachable, `_call_ollama()` returns an inline error string `[Lỗi Ollama ...]` rather than crashing the worker thread, and users can switch to `NLLB-200 INT8` mode in the UI combo box at any time.
- **Verification Requirement:** After any code edit, run `.\venv\Scripts\python.exe -m py_compile hardware_profile.py audio_capture.py ai_engine.py tts_virtual_mic.py overlay_ui.py main.py incoming_pipeline.py browser_bridge.py bridge_server.py native_host\launcher.py interpreter_summary.py youtube_caption.py` to verify zero syntax errors.

