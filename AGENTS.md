# AGENTS.md — Universal AI Agent Instructions

> **Compatible with:** Claude Code, Google Antigravity, Cursor, Windsurf, OpenAI Codex, GitHub Copilot, OpenCode.

## 1. Read First (Context Loading Order)

Before making any code changes, read the harness documentation in this order:
1. [`PROGRESS.md`](./PROGRESS.md) — **Session Handoff & Current State:** Exact progress, hardware/environment status, resolved issues, and next steps to resume.
2. [`CLAUDE.md`](./CLAUDE.md) — Core summary, repository map, commands, and the 5 mandatory architectural invariants.
3. [`ARCHITECTURE.md`](./ARCHITECTURE.md) — End-to-end data flow, threading model, and state/queue interactions.
4. [`docs/HARDWARE_AND_VRAM.md`](./docs/HARDWARE_AND_VRAM.md) — When touching `hardware_profile.py` or `ai_engine.py`.
5. [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md) — When adding features, UI controls, languages, or TTS/translation providers.

---

## 2. Execution Flow Summary (Mental Model for Agents)

### Startup Flow (`main.py`)
1. `QApplication` initializes `ControlBar` and `MeetingTranscriptLogger` (`meeting_logs/meeting_YYYY-MM-DD_HH-MM-SS.md`).
2. `detect_hardware_profile()` inspects OS and GPU (`torch.cuda` / `platform.machine()`) and returns an immutable `SystemProfile`.
3. `LocalAIEngine(profile)` is created (lazy-loads STT/NLLB models on first use to keep UI startup instant) and loads any persisted project glossary from `custom_glossary.txt` via `set_custom_glossary()`.
4. `VirtualMicTTSRouter()` scans PortAudio output devices (`sounddevice.query_devices()`) and auto-selects `CABLE Input` / `VB-Audio` (Windows) or `BlackHole` (macOS).
5. `SubtitleOverlayWindow` and `ControlBar` are displayed with Win32 screen-share stealth (`apply_window_capture_stealth` — `WDA_EXCLUDEFROMCAPTURE`) enabled by default on Windows.
6. `OutgoingVoiceWorker` (`QThread`) and `pynput.keyboard.Listener` (for global `F8` Push-to-Talk and global `Esc` emergency cancel) start immediately on launch.
7. When the user clicks **`▶ Bật Dịch Chiều Nghe`**, `IncomingInferenceWorker` (`QThread`) and `SystemAudioVADWorker` (`threading.Thread`) start capturing, translating, and logging system audio.

### Two Parallel Pipelines Sharing One Engine
```text
[Chiều Nghe / Incoming]
System Speaker Loopback ──► _StreamingResampler + _StreamingAGC + Silero VAD (16kHz, 512 samples)
  ──► incoming_queue ──► IncomingInferenceWorker ──► LocalAIEngine (gpu_lock + custom_glossary)
  ──► Whisper STT (Foreign) + Ollama/NLLB (to VI) ──► SubtitleOverlayWindow (src_label + vi_label)
                                                  ──► MeetingTranscriptLogger (meeting_logs/*.md)

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

- **Language & Localization:** Keep user-facing UI labels and status messages in Vietnamese (consistent with `overlay_ui.py` and `main.py`).
- **Sample Rate Contract:** Both `SystemAudioVADWorker` and `MicPushToTalkRecorder` normalize audio to **`16000 Hz`, mono (`1` channel), `np.float32` in range `[-1.0, 1.0]`** before pushing `np.ndarray` buffers into `incoming_queue` or `outgoing_queue`. Never push raw `int16` or stereo buffers into either queue.
- **No Blocking on Qt GUI Thread:** Never run model loading, HTTP calls (`requests.post` to Ollama), TTS synthesis, or `sd.play(..., blocking=True)` on the main Qt GUI thread. Keep all heavy I/O and inference inside `IncomingInferenceWorker` or `OutgoingVoiceWorker`.
- **Graceful Degradation:**
  - If `edge-tts` fails (e.g., offline / no internet), `VirtualMicTTSRouter.speak_to_virtual_mic()` automatically falls back to `_synthesize_offline_tts()` (`pyttsx3`). Preserve this try/except fallback.
  - If Ollama is unreachable, `_call_ollama()` returns an inline error string `[Lỗi Ollama ...]` rather than crashing the worker thread, and users can switch to `NLLB-200 INT8` mode in the UI combo box at any time.
- **Verification Requirement:** After any code edit, run `python -m py_compile hardware_profile.py audio_capture.py ai_engine.py tts_virtual_mic.py overlay_ui.py main.py` to verify zero syntax errors.

