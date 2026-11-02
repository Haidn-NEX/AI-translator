# ARCHITECTURE.md — System Architecture & Data Flow Specification

This document provides a deep-dive technical reference into the **Local AI 2-Way Live Translator** architecture, concurrency model, audio routing topology, and component interactions.

---

## 1. High-Level Component Architecture

```mermaid
flowchart TB
    subgraph OS_Audio["OS Audio Layer"]
        SysOut["System Audio Output<br/>(Zoom / Meet / YouTube / VLC)"]
        PhysMic["Physical Microphone<br/>(User Speaking Vietnamese)"]
        VirtCable["Virtual Audio Cable<br/>(VB-Cable Input / BlackHole 2ch)"]
        MeetMic["Zoom / Google Meet / Teams<br/>(Microphone Input = Virtual Cable Output)"]
        VirtCable --> MeetMic
    end

    subgraph Capture_Layer["Audio Capture Module (audio_capture.py)"]
        VADWorker["SystemAudioVADWorker<br/>(WASAPI Loopback / Soundcard + Silero VAD)"]
        PTTRec["MicPushToTalkRecorder<br/>(Global Hotkey F8 / UI Button)"]
    end

    subgraph Queues["Thread-Safe Audio Queues (16kHz float32 mono)"]
        InQ[("incoming_queue<br/>(queue.Queue)")]
        OutQ[("outgoing_queue<br/>(queue.Queue)")]
    end

    subgraph Workers["Qt Worker Threads (main.py)"]
        InWorker["IncomingInferenceWorker (QThread)"]
        OutWorker["OutgoingVoiceWorker (QThread)"]
    end

    subgraph Shared_AI["Shared AI Engine (ai_engine.py)"]
        GPULock{"self.gpu_lock<br/>(threading.RLock)"}
        Whisper["Faster-Whisper (CUDA INT8) /<br/>MLX-Whisper (Apple Silicon)"]
        Translator["Ollama LLM (qwen2.5:3b/7b) +<br/>CTranslate2 NLLB-200 (INT8 Firewall)"]
        CtxHist[("context_history (maxlen=4)<br/>+ custom_glossary.txt")]
        GPULock --> Whisper
        GPULock --> Translator
        Translator <--> CtxHist
        Whisper <--> CtxHist
    end

    subgraph TTS_Layer["TTS & Virtual Mic Router (tts_virtual_mic.py)"]
        TTSRouter["VirtualMicTTSRouter<br/>(edge-tts Neural / pyttsx3 Offline + Esc Stop)"]
    end

    subgraph UI_Layer["PyQt6 GUI & Meeting Safety Layer (main.py & overlay_ui.py)"]
        CtrlBar["ControlBar<br/>(WDA_EXCLUDEFROMCAPTURE Stealth)"]
        Overlay["SubtitleOverlayWindow<br/>(Translucent Click-Through + Stealth)"]
        MeetLog[("MeetingTranscriptLogger<br/>(meeting_logs/*.md)")]
    end

    SysOut -->|"Loopback PCM"| VADWorker
    VADWorker -->|"Sentence np.ndarray"| InQ
    InQ --> InWorker
    InWorker -->|"transcribe() & translate_to_vietnamese()"| GPULock
    InWorker -->|"result_ready signal"| Overlay
    InWorker -->|"log_incoming()"| MeetLog

    PhysMic -->|"While F8 Held"| PTTRec
    PTTRec -->|"On F8 Release (>=0.3s)"| OutQ
    OutQ --> OutWorker
    OutWorker -->|"transcribe(force_lang='vi') & translate_vi_to_english()"| GPULock
    OutWorker -->|"After 1.5s Preview (if not Esc-cancelled)"| TTSRouter
    TTSRouter -->|"sd.play(device=virtual_mic_index)"| VirtCable
    OutWorker -->|"outgoing_ready signal"| Overlay
    OutWorker -->|"log_outgoing()"| MeetLog
    CtrlBar -.->|"Controls, Glossary & Signals"| Overlay
```

---

## 2. Detailed Sequence Flows

### 2.1. Chiều Nghe (Incoming Audio $\rightarrow$ Vietnamese Subtitles)

```mermaid
sequenceDiagram
    autonumber
    participant Speaker as System Audio (WASAPI/BlackHole)
    participant VAD as SystemAudioVADWorker
    participant InQ as incoming_queue
    participant InW as IncomingInferenceWorker
    participant Engine as LocalAIEngine (gpu_lock)
    participant Overlay as SubtitleOverlayWindow

    Speaker->>VAD: Raw PCM stream (native sample rate, stereo/multi-ch)
    Note over VAD: Downmix to mono & resample to 16kHz float32
    loop Every 512 samples (32ms window)
        VAD->>VAD: Silero VAD speech_prob check (>= 0.45)
        alt Silence >= 0.42s OR Duration >= 7.5s (and Speech >= 0.35s)
            VAD->>InQ: put(full_sentence: np.ndarray)
            VAD->>VAD: Reset buffer & vad_model.reset_states()
        end
    end
    InW->>InQ: get(timeout=0.4)
    opt Backpressure: qsize() > 2
        InW->>InQ: get_nowait() (drop stale sentences to keep real-time)
    end
    InW->>Engine: transcribe(audio_np, force_lang=None)
    activate Engine
    Note over Engine: Acquire self.gpu_lock
    Engine-->>InW: (src_text, stt_ms)
    deactivate Engine
    InW->>Engine: translate_to_vietnamese(src_text)
    activate Engine
    Note over Engine: Include last 4 sentences from context_history
    Engine-->>InW: (vi_text, trans_ms)
    deactivate Engine
    InW->>Overlay: emit result_ready(src_text, vi_text, stt_ms, trans_ms)
    Overlay->>Overlay: Update hud_label, src_label, vi_label
```

### 2.2. Chiều Nói (Outgoing Push-to-Talk `F8` $\rightarrow$ English Virtual Mic)

```mermaid
sequenceDiagram
    autonumber
    participant User as User (Global F8 / UI Button)
    participant PTT as MicPushToTalkRecorder
    participant OutQ as outgoing_queue
    participant OutW as OutgoingVoiceWorker
    participant Engine as LocalAIEngine (gpu_lock)
    participant TTS as VirtualMicTTSRouter
    participant Overlay as SubtitleOverlayWindow

    User->>PTT: Press & Hold F8 (on_press)
    PTT->>PTT: Open sd.InputStream (16kHz, mono, float32)
    PTT->>Overlay: ptt_state_signal ("🎙️ ĐANG THU TIẾNG VIỆT...")
    User->>PTT: Release F8 (on_release)
    PTT->>PTT: Stop & close sd.InputStream, concat frames
    alt Duration >= 0.3s
        PTT->>OutQ: put(audio_np)
        PTT->>Overlay: ptt_state_signal ("⚡ Đang dịch câu nói của bạn...")
    else Duration < 0.3s
        PTT->>Overlay: ptt_state_signal ("Câu nói quá ngắn (< 0.3s), đã bỏ qua.")
    end
    OutW->>OutQ: get(timeout=0.3)
    OutW->>Engine: transcribe(audio_np, force_lang="vi")
    activate Engine
    Note over Engine: Acquire self.gpu_lock (reuses same Whisper model!)
    Engine-->>OutW: (vi_said, stt_ms)
    deactivate Engine
    OutW->>Engine: translate_vi_to_english(vi_said)
    activate Engine
    Engine-->>OutW: (en_text, trans_ms)
    deactivate Engine
    opt Safety Preview Gate (preview_before_speak == True, default 1.5s)
        OutW->>Overlay: status_update ("⏳ Sắp phát sau 1.5s (Nhấn [Esc] để HỦY)...")
        Note over User,OutW: Pressing Esc (or re-pressing F8 / Cancel button) sets cancel_event and aborts before TTS!
    end
    OutW->>TTS: speak_to_virtual_mic(en_text, cancel_event=self.cancel_event)
    activate TTS
    Note over TTS: Synthesize WAV via edge-tts (fallback: pyttsx3)<br/>Play via sd.play(..., blocking=False) polling cancel_event every 40ms
    TTS-->>OutW: tts_ms
    deactivate TTS
    OutW->>Overlay: emit outgoing_ready(vi_said, en_text, stt_ms, trans_ms, tts_ms)
    Overlay->>Overlay: Update outgoing_label + append to MeetingTranscriptLogger (meeting_logs/*.md)
```

---

## 3. Concurrency & Threading Model

The application runs **6 distinct execution threads**. Understanding their boundaries is critical for avoiding race conditions or Qt GUI freezes:

| # | Thread Name / Class | Base Type | Lifecycle | Communicates With | Synchronization Mechanism |
| :- | :--- | :--- | :--- | :--- | :--- |
| 1 | **Qt Main GUI Thread** | Event Loop (`QApplication`) | App start $\rightarrow$ exit | All UI widgets (`ControlBar`, `SubtitleOverlayWindow`), `MeetingTranscriptLogger` | Receives updates strictly via `pyqtSignal` |
| 2 | **`SystemAudioVADWorker`** | `threading.Thread(daemon=True)` | Started/Stopped via `toggle_capture()` | Pushes `np.ndarray` to `incoming_queue`; emits status via `hud_state_signal` | `queue.Queue`, `self.running` flag |
| 3 | **`IncomingInferenceWorker`** | `QThread` | Started/Stopped via `toggle_capture()` | Reads `incoming_queue`; calls `LocalAIEngine`; emits `result_ready` & `status_update` | `LocalAIEngine.gpu_lock`, `pyqtSignal` |
| 4 | **`OutgoingVoiceWorker`** | `QThread` | Runs continuously from launch until `closeEvent` | Reads `outgoing_queue`; runs `1.5s` safety preview; calls `LocalAIEngine` & `VirtualMicTTSRouter`; emits `outgoing_ready` | `LocalAIEngine.gpu_lock`, `self.cancel_event`, `pyqtSignal` |
| 5 | **`pynput.keyboard.Listener`** | `threading.Thread(daemon=True)` | Runs continuously from launch until `closeEvent` | Calls `ptt_recorder.start_recording()` / `stop_recording()` on `Key.f8`, and `outgoing_worker.cancel_current()` on `Key.esc` | `MicPushToTalkRecorder.is_recording`, `threading.Event` |
| 6 | **PortAudio Mic Callback** | C/PortAudio Thread (`sounddevice`) | Active only while `F8` is held | Appends mic chunks to `MicPushToTalkRecorder.frames` | Guarded by `self.is_recording` |

---

## 4. State & Queue Contracts

- **`incoming_queue` (`queue.Queue`):**
  - **Item Type:** `np.ndarray` (`dtype=np.float32`, 1D mono array at `16000 Hz`).
  - **Duration Range:** Typically `0.35s` to `7.5s` (`5,600` to `120,000` samples).
  - **Backpressure:** Drained down to the latest segment if `qsize() > 2`.
- **`outgoing_queue` (`queue.Queue`):**
  - **Item Type:** `np.ndarray` (`dtype=np.float32`, 1D mono array at `16000 Hz`).
  - **Duration Range:** `>= 0.3s` (`>= 4,800` samples), bounded by how long the user holds `F8`.
  - **Backpressure:** FIFO (every user utterance >= `0.3s` is processed in order).
- **`LocalAIEngine.context_history` (`collections.deque(maxlen=4)`):**
  - Stores tuples of `(source_text: str, vi_text: str)` for the last 4 incoming sentences.
  - Injected into the Ollama prompt (`_translate_ollama_to_vi`) so pronouns, domain terms, and conversational flow remain coherent across sentences.
  - Note: Outgoing utterances (`VI -> EN`) do **not** pollute `context_history`.

