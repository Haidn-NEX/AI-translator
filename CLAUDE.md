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

---

## 2. Repository Map & Module Responsibilities

| File | Responsibility | Key Classes / Functions |
| :--- | :--- | :--- |
| `main.py` | Application entrypoint, Qt Control Panel, worker orchestration, `1.5s` outgoing preview gate, bilingual meeting logger, global `F8`/`Esc` hotkeys | `ControlBar`, `IncomingInferenceWorker`, `OutgoingVoiceWorker`, `MeetingTranscriptLogger` |
| `hardware_profile.py` | OS & GPU auto-detection (GTX 1070 Pascal SM 6.1 vs Apple Silicon M-series vs CPU) + Windows CUDA DLL registration | `SystemProfile`, `detect_hardware_profile()` |
| `audio_capture.py` | System loopback capture + `_StreamingResampler` + `_StreamingAGC` + Silero VAD segmentation & Mic Push-to-Talk recorder | `SystemAudioVADWorker`, `MicPushToTalkRecorder`, `_StreamingResampler`, `_StreamingAGC` |
| `ai_engine.py` | Shared-VRAM STT + 2-way translation (`Ollama` + `NLLB-200 CT2` CJK firewall) + custom meeting glossary injection | `LocalAIEngine` (`self.gpu_lock`, `set_custom_glossary()`) |
| `tts_virtual_mic.py` | Interruptible English TTS synthesis (`edge-tts` / `pyttsx3`) & audio output routing into Virtual Audio Cable | `VirtualMicTTSRouter` (`speak_to_virtual_mic()`, `stop_playback()`) |
| `overlay_ui.py` | Frameless, always-on-top, click-through PyQt6 bilingual subtitle HUD + Win32 screen-share stealth (`WDA_EXCLUDEFROMCAPTURE`) | `SubtitleOverlayWindow`, `apply_window_capture_stealth()` |
| `requirements_win.txt` | Python dependencies for Windows + NVIDIA CUDA (GTX 1070) | PyQt6, PyAudioWPatch, faster-whisper, ctranslate2, etc. |
| `requirements_mac.txt` | Python dependencies for macOS Apple Silicon (M1 Pro) | PyQt6, soundcard, mlx-whisper, ctranslate2, etc. |
| `setup_and_run.bat` | One-click venv setup, CUDA PyTorch install, auto Ollama silent install + `ollama serve` + LLM pull, and app launch on Windows | Batch script |
| `setup_and_run.sh` | One-click venv setup, MLX install, auto Ollama install + `ollama serve` + LLM pull, and app launch on macOS | Bash script |

### Progressive Disclosure Documentation (Harness Docs)
- [`PROGRESS.md`](./PROGRESS.md) — **READ FIRST WHEN RESUMING:** Current project state, environment/hardware audit, resolved bugs, and exact next steps.
- [`AGENTS.md`](./AGENTS.md) — Universal agent instructions & workflow rules across AI coding tools.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Detailed Mermaid sequence/flow diagrams, 6-thread concurrency model, and queue contracts.
- [`docs/HARDWARE_AND_VRAM.md`](./docs/HARDWARE_AND_VRAM.md) — Hardware profiling logic, GTX 1070 SM 6.1 `int8` rules, M1 Pro Unified Memory tuning, and VRAM budgets.
- [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md) — Playbooks for adding new languages, swapping TTS/LLM backends, or modifying the UI safely.
- [`README.md`](./README.md) — End-user setup guide and Virtual Audio Cable routing for Zoom/Meet/Teams.

---

## 3. Common Commands

### Environment Setup & Execution
- **Windows (NVIDIA GTX 1070 / CUDA):**
  ```powershell
  .\setup_and_run.bat
  # Or manually:
  python -m venv venv
  .\venv\Scripts\activate
  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
  pip install -r requirements_win.txt
  python main.py
  ```
- **macOS (Apple Silicon M1 Pro):**
  ```bash
  chmod +x setup_and_run.sh
  ./setup_and_run.sh
  ```

### Verification & Smoke Checks (For AI Agents)
- **Syntax & Bytecode Compilation Check:**
  ```powershell
  python -m py_compile hardware_profile.py audio_capture.py ai_engine.py tts_virtual_mic.py overlay_ui.py main.py
  ```
- **Hardware Profile Detection Test:**
  ```powershell
  python -c "from hardware_profile import detect_hardware_profile; print(detect_hardware_profile())"
  ```

---

## 4. Critical Architectural Invariants (DO NOT VIOLATE)

When modifying or extending this codebase, every AI agent **MUST** preserve these 5 invariants:

1. **Single-Model Shared VRAM Lock (`self.gpu_lock`):**
   - Both `IncomingInferenceWorker` and `OutgoingVoiceWorker` call `LocalAIEngine.transcribe()` and NLLB translation methods concurrently from separate `QThread` instances.
   - All GPU/CTranslate2/MLX inference calls inside `LocalAIEngine` **must** acquire `with self.gpu_lock:` (`threading.RLock()`). Never instantiate a second Whisper or NLLB model for the outgoing channel.
2. **Pascal Architecture (GTX 1070 SM 6.1) `int8` Enforcement:**
   - NVIDIA Pascal GPUs (`major == 6`, e.g., GTX 1070 8GB) lack FP16 Tensor Cores. In `hardware_profile.py`, when `major == 6`, `stt_compute_type` **must** remain `"int8"` (never `"float16"` or `"int8_float16"`).
3. **PyQt6 Cross-Thread UI Safety:**
   - Background threads (`SystemAudioVADWorker` running in `threading.Thread`, `MicPushToTalkRecorder` triggered by `pynput.keyboard.Listener`, `IncomingInferenceWorker`, `OutgoingVoiceWorker`) **must never** call `QLabel.setText()` or mutate `QWidget` state directly.
   - Always emit a `pyqtSignal` (such as `ptt_state_signal`, `hud_state_signal`, `result_ready`, `outgoing_ready`, `status_update`) connected to slots on the Qt Main Thread.
4. **Incoming Queue Backpressure Drop Policy:**
   - In `IncomingInferenceWorker.run()`, if `self.sentence_queue.qsize() > 2`, older queued audio segments are drained/skipped so subtitles stay real-time and never lag behind live video/meetings. Do not remove this backpressure drain loop.
5. **Strict Audio Isolation (No Feedback Loops):**
   - **Incoming (`SystemAudioVADWorker`)** captures *System Output Loopback* (speakers/headphone loopback).
   - **Outgoing (`MicPushToTalkRecorder` -> `VirtualMicTTSRouter`)** captures the *Physical Microphone* only while `F8` (or UI PTT button) is held down, and outputs synthesized English audio strictly to the *Virtual Cable Input* (`CABLE Input` / `BlackHole 2ch`), preventing infinite translation loops.

