# CLAUDE.md — AI Agent Harness & Project Specification

> **Purpose:** This file is the primary cognitive entry point (Claude Harness standard) for all AI coding agents (Claude Code, Antigravity, Cursor, Windsurf, Codex, Copilot). Read this file first before modifying any code in this repository.

---

## 1. Project Overview

**Local AI 2-Way Live Translator (`local-live-translator`)** is a cross-platform, offline-capable desktop application that performs **Full-Duplex (Bi-Directional) Real-Time Speech Translation** while sharing a **single set of AI models in VRAM ($0\text{ MB}$ extra VRAM overhead for 2-way operation)**:

1. **Direction 1 — Incoming Audio (`Foreign Language -> Vietnamese Subtitle Overlay`):**
   - Captures system audio loopback (`WASAPI Loopback` on Windows / `BlackHole 2ch` via `soundcard` on macOS).
   - Applies anti-aliased resampling (`_StreamingResampler`), automatic gain normalization (`_StreamingAGC`), and adaptive **Silero VAD** ($16\text{ kHz}$, $512$-sample windows, $300\text{ ms}$ pre-speech ring buffer, dual-threshold hysteresis).
   - Transcribes speech via `faster-whisper` (CUDA `int8` on GTX 1070) or `mlx-whisper` (Apple Silicon M1 Pro) with custom meeting glossary hints (`custom_glossary.txt`) and translates to Vietnamese via **Ollama** (`qwen2.5:3b` / `qwen2.5:7b`, auto-Vulkan on GTX 1070 SM 6.1) + **CTranslate2 NLLB-200** (`int8`, zero-CJK firewall & offline fallback).
   - Renders bilingual subtitles + real-time latency telemetry (`Latency HUD`) on a translucent, click-through PyQt6 overlay window protected from Zoom/Meet screen sharing via Win32 `WDA_EXCLUDEFROMCAPTURE`, while streaming bilingual transcripts to `meeting_logs/*.md`.
2. **Direction 2 — Outgoing Voice (`Vietnamese Mic Push-to-Talk [F8] -> 1.5s Safety Preview [Esc Cancel] -> English Virtual Microphone`):**
   - Listens globally for the **`F8`** hotkey (record speech) and **`Esc`** hotkey (emergency cancel/stop playback) via `pynput` even when Zoom/Google Meet/Teams is focused in fullscreen.
   - Records physical microphone audio (`16 kHz` mono `float32`), then reuses the **exact same loaded Whisper + Translation models** protected by `LocalAIEngine.gpu_lock` (`threading.RLock()`).
   - Displays a `1.5s` live safety preview countdown on the overlay before speaking so users can cancel mis-translations instantly with **`Esc`**; if confirmed, synthesizes natural English speech (`edge-tts` with automatic offline fallback to `pyttsx3`, $0\text{ MB}$ VRAM) and routes interruptible audio directly into **`CABLE Input (VB-Audio Virtual Cable)`** (Windows) or **`BlackHole 2ch`** (macOS).
   - Displays a green verification banner on the overlay (`🎙️ Bạn nói: "[VI]" ➔ 🔊 Phát sang Meet: "[EN]"`) and appends confirmed utterances to `meeting_logs/*.md`.
3. **Direction 3 — Chrome Extension "Dịch Video Tại Chỗ" (`Tab Audio / Hybrid YouTube Caption Mode [Alt+Shift+T] -> Local WebSocket Bridge -> In-Player Shadow DOM Subtitle Overlay`):**
   - **YouTube Caption Mode (Fast-Path on YouTube VOD/Shorts):** Captures native `/api/timedtext` tracks (`json3`/`srv3`/`vtt`) via `yt_caption_hook.js` (`MAIN` world, including automatic ~200ms CC flash-fetch + immediate CC-OFF restore when native CC is off), reconstructs clean non-overlapping sentences from ASR word-level `tOffsetMs`, translates a `+90s` sliding window in 4-sentence batches (`CaptionTranslationWorker` + `LocalAIEngine.translate_caption_batch()` + `caption_cache/`), and synchronizes subtitles directly to `video.currentTime` (`0 ms` perceived delay, `0%` Whisper STT load).
   - **Audio Fallback (`tabCapture`):** Automatically used on non-YouTube sites, YouTube Live streams, or videos without captions. Captures audio strictly from the active browser tab via `chrome.tabCapture` + MV3 Offscreen `AudioWorklet` (`48 kHz` $\rightarrow$ `40ms` Int16 mono PCM frames) while keeping tab audio audible (`source.connect(audioCtx.destination)`), streaming PCM over `ws://127.0.0.1:47863/ws?token=...` to `BrowserBridgeServer` $\rightarrow$ `TabAudioVADWorker` $\rightarrow$ `IncomingInferenceWorker` $\rightarrow$ `LocalAIEngine` (`0 MB` extra VRAM) and logging watched utterances to `meeting_logs/video_*.md`.
   - Renders bilingual subtitles inside an isolated Shadow DOM (`chrome_extension/content_script.js`) anchored directly over the active `<video>` element (supporting normal, theater, fullscreen, and drag-and-drop positioning).

---

## 2. Repository Map & Module Responsibilities

| File | Responsibility | Key Classes / Functions |
| :--- | :--- | :--- |
| `main.py` | Application entrypoint, Qt Control Panel, worker orchestration, integrated `BrowserBridgeServer`, `1.5s` outgoing preview gate, global `F8`/`Esc` hotkeys | `ControlBar`, `OutgoingVoiceWorker` |
| `incoming_pipeline.py` | Shared incoming inference pipeline, conjunction stitcher, filler filter, and bilingual Markdown transcript logger (used by both `main.py` and `bridge_server.py`) | `IncomingInferenceWorker`, `MeetingTranscriptLogger`, `setup_logging()` |
| `interpreter_summary.py` | Tier-2 "Thư ký Hợp nhất Ý chính" rolling summary worker (`interval`, `pause`, `manual`, `cap`), settings persistence (`interpreter_settings.json`), and CSV telemetry writer | `SummaryWorker`, `SummarySettings`, `decide_trigger()`, `build_summary_prompt()`, `validate_summary()` |
| `youtube_caption.py` | YouTube `timedtext` parser (`json3` manual/ASR word-level reconstruction, XML `srv3`, `WebVTT`), local LRU cache (`caption_cache/`), and `+90s` sliding-window batch translator | `CaptionCue`, `parse_youtube_timedtext()`, `CaptionTranslationCache`, `CaptionTranslationWorker` |
| `browser_bridge.py` | Localhost HTTP/WebSocket bridge (`127.0.0.1:47863`), Origin/Token security guard, hybrid YouTube Caption / Tab Audio mode switcher, and per-tab VAD audio stream adapter | `BrowserBridgeServer`, `BridgeSession`, `TabAudioVADWorker`, `load_or_create_bridge_token()` |
| `bridge_server.py` | Headless `QCoreApplication` entrypoint for Chrome Extension (singleton `/health` check, background model preloader, `--idle-exit 900`) | `BridgePreloader`, `main()` |
| `native_host/launcher.py` | Chrome Native Messaging Host (`com.local_translator.bridge`) binary stdio handler (`ensure_running`, `get_token`, `stop`) + `install_native_host.ps1` | `main()`, `_spawn_bridge_detached()` |
| `chrome_extension/*` | MV3 Chrome Extension "Dịch Video Tại Chỗ" (`Alt+Shift+T`): `yt_caption_hook.js` (`MAIN` world timedtext hook), `tabCapture`, `AudioWorklet`, Shadow DOM `<video>` overlay + top-right summary card, `SubtitlePacer`, Vietnamese Popup/Options | `yt_caption_hook.js`, `service_worker.js`, `offscreen.js`, `pcm_worklet.js`, `content_script.js`, `subtitle_pacer.js` |
| `hardware_profile.py` | OS & GPU auto-detection (GTX 1070 Pascal SM 6.1 vs Apple Silicon M-series vs CPU) + Windows CUDA DLL registration | `SystemProfile`, `detect_hardware_profile()` |
| `audio_capture.py` | System loopback capture + `_StreamingResampler` + `_StreamingAGC` + Silero VAD segmentation & Mic Push-to-Talk recorder | `SystemAudioVADWorker`, `MicPushToTalkRecorder`, `_StreamingResampler`, `_StreamingAGC` |
| `ai_engine.py` | Shared-VRAM STT + 2-way translation (`Ollama` + `NLLB-200 CT2` CJK firewall) + bidirectional-context `translate_caption_batch()` + preemptible `generate_summary_json()` (`OLLAMA_NUM_CTX=1024`) + custom meeting glossary injection | `LocalAIEngine` (`self.gpu_lock`, `self._llm_lock`, `translate_caption_batch()`, `generate_summary_json()`, `set_custom_glossary()`) |
| `tts_virtual_mic.py` | Interruptible English TTS synthesis (`edge-tts` / `pyttsx3`) & audio output routing into Virtual Audio Cable | `VirtualMicTTSRouter` (`speak_to_virtual_mic()`, `stop_playback()`) |
| `overlay_ui.py` | Frameless, always-on-top, click-through PyQt6 bilingual subtitle HUD + `SummaryPanelWindow` + Win32 screen-share stealth (`WDA_EXCLUDEFROMCAPTURE`) | `SubtitleOverlayWindow`, `SummaryPanelWindow`, `apply_window_capture_stealth()` |
| `tests/test_bridge_ws.py` | Automated end-to-end WebSocket + security + 48kHz WAV real-time streaming test for `BrowserBridgeServer` | `run_tests()` |
| `tests/test_summary_worker.py` | Unit tests for `SummarySettings`, `decide_trigger`, `build_summary_prompt`, `validate_summary`, and `SummaryWorker` preemption/hot-switch | `TestSummaryWorker` |
| `tests/test_bridge_summary.py` | Automated unit test for `BrowserBridgeServer` summary config sync (`hello_ack.summary_settings`) and `summary_now` WebSocket event emission | `TestBridgeSummary` |
| `tests/test_youtube_caption.py` | Unit & integration tests for `parse_youtube_timedtext` (manual/ASR `json3`, `srv3`, `vtt`), `translate_caption_batch` rescue, `CaptionTranslationCache`, and `BridgeSession` watched-cue/seek gating | `TestYouTubeCaption` |
| `tests/summarize_metrics.py` | CLI aggregator for `meeting_logs/summary_metrics_*.csv` & `app_debug.log` (`interval` vs `pause` Markdown comparison table) | `main()` |
| `requirements_win.txt` | Python dependencies for Windows + NVIDIA CUDA (GTX 1070) | PyQt6, PyAudioWPatch, faster-whisper, ctranslate2, aiohttp, etc. |
| `requirements_mac.txt` | Python dependencies for macOS Apple Silicon (M1 Pro) | PyQt6, soundcard, mlx-whisper, ctranslate2, aiohttp, etc. |
| `setup_and_run.bat` | One-click venv setup, CUDA PyTorch install, auto Ollama silent install + `ollama serve` + LLM pull, and app launch on Windows | Batch script |
| `start_bridge.bat` | Quick launcher for headless `bridge_server.py` (when using Chrome Extension without opening the Desktop UI) | Batch script |
| `setup_and_run.sh` | One-click venv setup, MLX install, auto Ollama install + `ollama serve` + LLM pull, and app launch on macOS | Bash script |

### Progressive Disclosure Documentation (Harness Docs)
- [`PROGRESS.md`](./PROGRESS.md) — **READ FIRST WHEN RESUMING:** Current project state, environment/hardware audit, resolved bugs, and exact next steps.
- [`AGENTS.md`](./AGENTS.md) — Universal agent instructions & workflow rules across AI coding tools.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Detailed Mermaid sequence/flow diagrams, concurrency model, and queue/WebSocket contracts.
- [`docs/INTERPRETER_SUMMARY_SPEC.md`](./docs/INTERPRETER_SUMMARY_SPEC.md) — Complete specification for Tier-2 "Thư ký Hợp nhất Ý chính" (`SummaryWorker`, `OLLAMA_NUM_CTX`, preemption, and `interval` vs `pause` evaluation).
- [`docs/CHROME_EXTENSION_SPEC.md`](./docs/CHROME_EXTENSION_SPEC.md) — Complete specification for the Chrome Extension "Dịch Video Tại Chỗ", WebSocket protocol, and Native Messaging Host.
- [`docs/HARDWARE_AND_VRAM.md`](./docs/HARDWARE_AND_VRAM.md) — Hardware profiling logic, GTX 1070 SM 6.1 `int8` rules, M1 Pro Unified Memory tuning, and VRAM budgets.
- [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md) — Playbooks for adding new languages, custom audio sources (`TabAudioVADWorker`), swapping TTS/LLM backends, or modifying the UI safely.
- [`README.md`](./README.md) — End-user setup guide, Chrome Extension (`Alt+Shift+T`) setup, and Virtual Audio Cable routing for Zoom/Meet/Teams.

---

## 3. Common Commands

### Environment Setup & Execution
- **Windows (NVIDIA GTX 1070 / CUDA — Full Desktop App + Integrated Chrome Bridge):**
  ```powershell
  .\setup_and_run.bat
  # Or manually:
  python -m venv venv
  .\venv\Scripts\activate
  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
  pip install -r requirements_win.txt
  python main.py
  ```
- **Windows (Headless Bridge Server & Chrome Native Messaging Host Registration):**
  ```powershell
  # One-time registration of Chrome Native Host + Windows Scheduled Task:
  powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ExtensionId cgbbacpgahelimaelidkgmphgcbiclfd
  # Or manual headless startup:
  .\start_bridge.bat
  # Or directly:
  .\venv\Scripts\python.exe bridge_server.py --idle-exit 900
  ```
- **macOS (Apple Silicon M1 Pro):**
  ```bash
  chmod +x setup_and_run.sh
  ./setup_and_run.sh
  ```

### Verification & Smoke Checks (For AI Agents)
- **Syntax & Bytecode Compilation Check:**
  ```powershell
  .\venv\Scripts\python.exe -m py_compile hardware_profile.py audio_capture.py ai_engine.py tts_virtual_mic.py overlay_ui.py main.py incoming_pipeline.py browser_bridge.py bridge_server.py native_host\launcher.py interpreter_summary.py youtube_caption.py tests\test_bridge_ws.py tests\test_youtube_caption.py
  ```
- **Hardware Profile Detection Test:**
  ```powershell
  .\venv\Scripts\python.exe -c "from hardware_profile import detect_hardware_profile; print(detect_hardware_profile())"
  ```

---

## 4. Critical Architectural Invariants (DO NOT VIOLATE)

When modifying or extending this codebase, every AI agent **MUST** preserve these 6 invariants:

1. **Single-Model Shared VRAM Lock (`self.gpu_lock`):**
   - Both `IncomingInferenceWorker` and `OutgoingVoiceWorker` call `LocalAIEngine.transcribe()` and NLLB translation methods concurrently from separate `QThread` instances.
   - All GPU/CTranslate2/MLX inference calls inside `LocalAIEngine` **must** acquire `with self.gpu_lock:` (`threading.RLock()`). Never instantiate a second Whisper or NLLB model for the outgoing channel.
2. **Pascal Architecture (GTX 1070 SM 6.1) `int8` Enforcement:**
   - NVIDIA Pascal GPUs (`major == 6`, e.g., GTX 1070 8GB) lack FP16 Tensor Cores. In `hardware_profile.py`, when `major == 6`, `stt_compute_type` **must** remain `"int8"` (never `"float16"` or `"int8_float16"`).
3. **PyQt6 Cross-Thread UI Safety:**
   - Background threads (`SystemAudioVADWorker` running in `threading.Thread`, `MicPushToTalkRecorder` triggered by `pynput.keyboard.Listener`, `IncomingInferenceWorker`, `OutgoingVoiceWorker`, `SummaryWorker`) **must never** call `QLabel.setText()` or mutate `QWidget` state directly.
   - Always emit a `pyqtSignal` (such as `ptt_state_signal`, `hud_state_signal`, `result_ready`, `outgoing_ready`, `summary_ready`, `status_update`) connected to slots on the Qt Main Thread.
4. **Incoming Queue Backpressure Drop Policy:**
   - In `IncomingInferenceWorker.run()`, if `self.sentence_queue.qsize() > 2`, older queued audio segments are drained/skipped so subtitles stay real-time and never lag behind live video/meetings. Do not remove this backpressure drain loop.
5. **Strict Audio Isolation (No Feedback Loops):**
   - **Incoming (`SystemAudioVADWorker`)** captures *System Output Loopback* (speakers/headphone loopback).
   - **Outgoing (`MicPushToTalkRecorder` -> `VirtualMicTTSRouter`)** captures the *Physical Microphone* only while `F8` (or UI PTT button) is held down, and outputs synthesized English audio strictly to the *Virtual Cable Input* (`CABLE Input` / `BlackHole 2ch`), preventing infinite translation loops.
6. **Uniform `OLLAMA_NUM_CTX` & Live Subtitle Preemption Over Summary:**
   - Every Ollama request across `LocalAIEngine` (probe, warmup, live subtitle translation, and Tier-2 summary) **must** use the single constant `OLLAMA_NUM_CTX = 1024` (never mixing `512` and `1024`/`2048`), preventing Ollama from unloading/reloading the model in VRAM (`2–5s` stall).
   - Live subtitle translation (`_call_ollama_chat`) **must** always preempt Tier-2 summary generation (`generate_summary_json`) via `self._llm_preempt.set()` + `self._abort_active_summary_stream()` before acquiring `self._llm_lock`, and summary calls must never acquire `self.gpu_lock` (so Whisper STT runs in parallel with summary LLM generation).

