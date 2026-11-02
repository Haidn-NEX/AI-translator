# Developer & AI Agent Extension Guide

> Step-by-step playbooks for safely extending or customizing the Local AI 2-Way Live Translator.

---

## 1. Adding a New Source Language (Chiều Nghe)

Currently supported incoming languages in `ControlBar.lang_combo` (`main.py`):
- `en` (English), `auto` (Auto-detect), `ja` (Japanese), `zh` (Chinese), `ko` (Korean).

To add a new language (e.g., French `fr` or German `de`):
1. **In `main.py` (`ControlBar._build_ui`):**
   Add `"fr (Tiếng Pháp)"` to `self.lang_combo.addItems([...])`. Because `on_lang_changed` extracts `text.split(" ")[0]`, `self.engine.source_lang` will automatically receive `"fr"`.
2. **In `ai_engine.py`:**
   - **Whisper + Ollama mode:** Works out-of-the-box with zero code changes! Whisper supports 99 languages via ISO-639-1 codes (`"fr"`), and `_translate_ollama_to_vi` translates any language into Vietnamese.
   - **NLLB-200 CT2 mode:** If supporting non-English source languages in NLLB mode, map `self.source_lang` to its FLORES-200 code (e.g., `"fra_Latn"`, `"jpn_Jpan"`, `"zho_Hans"`, `"kor_Hang"`) inside `translate_to_vietnamese()` and initialize the corresponding `AutoTokenizer`.

---

## 2. Changing Global Hotkeys (Default: `F8` Push-to-Talk & `Esc` Emergency Cancel)

To change the global Push-to-Talk hotkey (`F8`) or the Outgoing Cancel hotkey (`Esc`):
1. Open `main.py` and locate `ControlBar._start_global_hotkey_listener()`.
2. Update the key checks in `on_press(key)` and `on_release(key)`:
   ```python
   if key == keyboard.Key.f8:    # Push-to-Talk key (e.g. keyboard.Key.f9 or keyboard.Key.alt_r)
       ...
   elif key == keyboard.Key.esc: # Cancel preview / stop TTS playback key
       ...
   ```
3. **Thread-Safety Rule:** Never update Qt widgets directly inside `on_press` or `on_release`. Always delegate via thread-safe methods (`self.ptt_recorder.start_recording()`, `self.outgoing_worker.cancel_current()`) that communicate back to Qt via `pyqtSignal`.
4. Update the instructional labels in `overlay_ui.py` (`hud_label`, `outgoing_label`) and `main.py` (`btn_ptt`, `chk_preview`, `btn_cancel_tts`).

---

## 3. Customizing TTS Voices (`tts_virtual_mic.py`)

- **Neural Edge TTS (`edge-tts`):**
  - Default voice: `self.voice_name = "en-US-AndrewNeural"` at `rate="+8%"` in `VirtualMicTTSRouter.__init__`.
  - Other natural conversational voices:
    - Male: `"en-US-BrianNeural"`, `"en-US-ChristopherNeural"`, `"en-GB-RyanNeural"`
    - Female: `"en-US-AriaNeural"`, `"en-US-AvaNeural"`, `"en-US-EmmaNeural"`
- **Monitor to Own Speaker (`self.monitor_to_speaker`):**
  - If the user also wants to hear the synthesized English voice in their own headphones while it plays into the Virtual Cable, play `data` to `sd.default.device[1]` on a non-blocking stream or secondary thread in `VirtualMicTTSRouter.speak_to_virtual_mic()`.
  - **Warning:** Only enable speaker monitoring if the user is wearing headphones or if `SystemAudioVADWorker` is paused during outgoing TTS playback; otherwise `SystemAudioVADWorker` (which records system speaker loopback) will hear the user's own synthesized English voice and translate it back to Vietnamese on the overlay!

---

## 4. Tuning Silero VAD Sensitivity (`audio_capture.py`)

In `SystemAudioVADWorker.__init__` (paired with `_StreamingAGC` automatic gain normalization):
- `self.speech_threshold = 0.34` & `self.speech_end_threshold = 0.20`: Dual-threshold hysteresis tuned for quiet/movie dialogue with background music while `_StreamingAGC` boosts soft speech (`rms > 0.00025`) toward `-24 dBFS` (`0.065`) with a brickwall `0.95` peak limiter.
- `self.pre_speech_sec = 0.30` & `self.post_speech_sec = 0.20`: Pre-speech ring buffer (`~300ms`) and post-padding (`~200ms`) to preserve initial plosives/fricatives (`b-`, `c-`, `f-`, `th-`).
- Low-latency adaptive pause thresholds (`short_pause_silence_sec = 0.62`, `min_silence_sec = 0.45`, `long_silence_sec = 0.28`): Bridges short `< 1.2s` connectors (`"But..."`) while cutting normal movie/conversation lines quickly at `0.45s` and `>= 3.8s` clauses at `0.28s`.
- `self.phrase_cut_min_sec = 2.4` (`phrase_dip_prob = 0.45`, `phrase_dip_frames = 3`), `self.soft_max_sentence_sec = 3.6`, & `self.max_sentence_sec = 5.2`: For fast, tightly-edited podcasts/meetings where speakers never fully pause (`speech_prob >= 0.20` continuously), splits phrases at natural `~96ms` breath/comma dips starting at `2.4s`. If speech reaches `5.2s` with no dip, performs a **Smart Valley Split** at the lowest 3-frame VAD minimum in `2.4s..5.2s` and carries the remaining frames seamlessly into the next segment.
- `self.min_speech_sec = 0.22`: Minimum **voiced** duration (`>= 220ms` of active voiced frames) to catch short exclamations (`"Save it!"`, `"Fine."`) while rejecting single-frame clicks.
- `_VoiceBandHPF(taps=65, cutoff_hz=155.0)` & `self.rescue_window_sec = 4.5`: Side-chain linear-phase FIR high-pass filter strips `< 155 Hz` movie sub-bass swells before `vad_agc -> Silero VAD`, paired with an Active-Audio Rescue watchdog that dispatches `4.5s` windows directly to Whisper whenever voice-band energy stays active (`vb_rms >= 0.0008`) while music/camcorder filtering suppresses VAD.

---

## 5. Customizing Meeting Safety Features (Stealth, Preview Delay, Glossary & Transcripts)

- **Screen-Share Stealth (`apply_window_capture_stealth` in `overlay_ui.py`):**
  - Uses Win32 `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE = 0x11)` (Windows 10 2004+ / Windows 11) with automatic fallback to `WDA_MONITOR = 0x01`.
  - **Important PyQt6 Rule:** Calling `QWidget.setWindowFlag(...)` followed by `show()` (such as inside `SubtitleOverlayWindow.set_click_through()`) recreates the underlying Win32 `HWND`, resetting display affinity. Always re-apply `apply_window_capture_stealth(self, True)` inside `showEvent()` and after any `setWindowFlag()` call.
- **Outgoing Safety Preview Countdown (`OutgoingVoiceWorker` in `main.py`):**
  - Controlled by `self.preview_before_speak = True` and `self.preview_delay_sec = 1.5`. Increase `preview_delay_sec` (e.g. `2.5`) if users want more reading time before English TTS plays into Zoom/Meet.
- **Custom Project Glossary (`custom_glossary.txt` & `LocalAIEngine.set_custom_glossary`):**
  - Supports comma/semicolon-separated terms (`Kubernetes, SLA, OKR`) as well as explicit translation mappings (`->`, `=>`, `=`, e.g., `P&L -> Báo cáo lãi lỗ`), automatically feeding both Whisper `initial_prompt` (`_glossary_stt_hint`) and Ollama system/user prompts.
- **Bilingual Meeting Transcripts (`MeetingTranscriptLogger` in `main.py`):**
  - Writes timestamped UTF-8 Markdown logs lazily (only creating a file when the first utterance occurs) into `meeting_logs/meeting_YYYY-MM-DD_HH-MM-SS.md`. Cancelled outgoing utterances (`Esc`) are intentionally excluded from confirmed transcripts.

