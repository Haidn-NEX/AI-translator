# AGENT BLUEPRINT: CROSS-PLATFORM LOCAL AI 2-WAY LIVE TRANSLATOR (DESKTOP OVERLAY + VIRTUAL MIC)

> **Instruction for AI Coding Agent (Antigravity):**
> Read this entire specification carefully. Your task is to scaffold, implement, and verify this complete **Bi-Directional (2-Way)** Python Desktop application based on the architecture and code modules detailed below. Create all files in the specified directory structure, set up the virtual environment according to the detected OS (`Windows` with NVIDIA GTX 1070 or `macOS` with Apple Silicon M1 Pro), and provide a one-command launcher.

## 1. Mục tiêu Dự án & Kiến trúc 2 Chiều (Full-Duplex)

Xây dựng phần mềm Desktop độc lập (chạy Offline trên từng máy, tối ưu hóa riêng cho **PC GTX 1070 $8\text{ GB}$ VRAM** và **Mac M1 Pro $32\text{ GB}$ RAM**) xử lý đồng thời 2 luồng dịch thời gian thực và **tái sử dụng chung 1 bộ model trong VRAM**:

### Chiều 1 — Chiều Nghe (Incoming Audio: Ngoại ngữ $\rightarrow$ Phụ đề Tiếng Việt)
1. **Bắt âm thanh hệ thống (System Audio Loopback):** Thu trực tiếp âm thanh phát ra từ Zoom, Google Meet, Teams, trình duyệt xem YouTube hoặc phần mềm xem phim (VLC).
2. **Cắt câu tự động (Silero VAD):** Phát hiện nhịp nghỉ tự nhiên ($0.42\text{ s}$) để cắt luồng âm thanh thành từng câu hoàn chỉnh.
3. **STT + Dịch sang Tiếng Việt:** Dùng `faster-whisper` (CUDA `int8` trên GTX 1070) hoặc `mlx-whisper` (trên Mac M1 Pro) kết hợp với **Ollama LLM** (`qwen2.5:3b` / `qwen2.5:7b`) hoặc **CTranslate2 NLLB-200** (`int8`).
4. **Hiển thị Overlay Xuyên thấu:** Hiện phụ đề song ngữ trên màn hình với tính năng bấm chuột xuyên qua chữ (`Click-through`) và đồng hồ đo độ trễ `Latency HUD`.

### Chiều 2 — Chiều Nói (Outgoing Voice: Bạn nói Tiếng Việt $\rightarrow$ Bên kia nghe Tiếng Anh)
1. **Cơ chế Push-to-Talk Toàn cục (Global Hotkey `F8` hoặc nút bấm trên UI):** Khi đang ở trong cửa sổ Zoom/Google Meet, người dùng chỉ cần nhấn giữ phím **`F8`** để nói tiếng Việt và thả phím ra khi nói xong.
2. **Cơ chế Ưu tiên GPU (GPU Priority Lock) & Tái sử dụng Model ($0\text{ MB}$ VRAM phát sinh):** Ngay khi người dùng thả phím `F8`, hệ thống lập tức dùng chính model Whisper và model Dịch (Qwen/NLLB) đang nằm sẵn trong VRAM để nhận diện câu tiếng Việt và dịch ngược sang tiếng Anh.
3. **Tổng hợp Giọng nói Tiếng Anh (Local TTS) & Đẩy vào Micro Ảo (Virtual Audio Cable):**
   * Chuyển văn bản tiếng Anh vừa dịch thành giọng nói tự nhiên (chạy trên CPU/ONNX tốn $0\text{ MB}$ VRAM).
   * Phát thẳng luồng âm thanh tiếng Anh đó vào thiết bị cáp ảo **`CABLE Input (VB-Audio Virtual Cable)`** trên Windows hoặc **`BlackHole 2ch`** trên macOS.
   * Ứng dụng Zoom / Google Meet (nhận đầu vào Microphone từ cáp ảo) sẽ truyền trực tiếp giọng nói tiếng Anh sang đầu dây bên kia.
4. **Hiển thị Kiểm tra Chéo trên Overlay:** Ngay trên khung phụ đề nổi sẽ hiện dòng trạng thái màu xanh lá: `🎙️ Bạn nói: [Câu tiếng Việt] -> 🔊 Phát sang Meet: [Câu tiếng Anh]` để bạn kiểm soát $100\%$ nội dung đối phương nghe được.

## 2. Cấu trúc Thư mục Dự án (Project Structure)

Agent cần khởi tạo cấu trúc thư mục gồm 6 module Python và 4 file cấu hình/khởi chạy:

```
local-live-translator/
├── main.py                  # Điều phối đa luồng (Incoming + Outgoing), Global Hotkey F8 & Control Panel
├── hardware_profile.py      # Tự động nhận diện OS, GPU (GTX 1070 vs M1 Pro) & chọn cấu hình tối ưu
├── audio_capture.py         # Gồm SystemAudioVADWorker (chiều nghe) & MicPushToTalkWorker (chiều nói)
├── ai_engine.py             # Quản lý STT + Dịch 2 chiều (EN<->VI) có khóa ưu tiên GPU (Thread Lock)
├── tts_virtual_mic.py       # Tổng hợp giọng nói Tiếng Anh & định tuyến thẳng vào Virtual Audio Cable
├── overlay_ui.py            # Giao diện PyQt6: Khung phụ đề xuyên thấu + Hiển thị kiểm tra chiều nói + HUD
├── requirements_win.txt     # Thư viện tối ưu cho Windows + CUDA (GTX 1070)
├── requirements_mac.txt     # Thư viện tối ưu cho macOS Apple Silicon (M1 Pro)
├── setup_and_run.bat        # Script tự động cài đặt và chạy trên Windows
└── setup_and_run.sh         # Script tự động cài đặt và chạy trên macOS
```

## 3. Chi tiết Các File Cấu hình & Thư viện

### 3.1. `requirements_win.txt` (Dành cho PC GTX 1070)

```
PyQt6>=6.6.0
numpy>=1.24.0,<2.0.0
PyAudioWPatch>=0.2.12.6
sounddevice>=0.4.6
soundfile>=0.12.1
pynput>=1.7.6
torch>=2.1.0
torchaudio>=2.1.0
faster-whisper>=1.0.3
ctranslate2>=4.0.0
transformers>=4.38.0
sentencepiece>=0.2.0
requests>=2.31.0
huggingface-hub>=0.21.0
edge-tts>=6.1.12
pyttsx3>=2.90
```

### 3.2. `requirements_mac.txt` (Dành cho Mac M1 Pro)

```
PyQt6>=6.6.0
numpy>=1.24.0,<2.0.0
soundcard>=0.4.3
sounddevice>=0.4.6
soundfile>=0.12.1
pynput>=1.7.6
torch>=2.1.0
torchaudio>=2.1.0
faster-whisper>=1.0.3
mlx-whisper>=0.4.0
ctranslate2>=4.0.0
transformers>=4.38.0
sentencepiece>=0.2.0
requests>=2.31.0
huggingface-hub>=0.21.0
edge-tts>=6.1.12
pyttsx3>=2.90
```

## 4. Chi tiết Mã nguồn Từng Module

### 4.1. `hardware_profile.py` (Tự động nhận diện M1 Pro vs GTX 1070)

```
import platform
import torch
from dataclasses import dataclass

@dataclass
class SystemProfile:
    os_name: str
    device_name: str
    stt_backend: str        # "faster_whisper" hoặc "mlx_whisper"
    stt_device: str         # "cuda" hoặc "cpu"
    stt_compute_type: str   # "int8", "float16", "int8_float16"
    recommended_whisper: str
    recommended_llm: str
    vram_gb: float

def detect_hardware_profile() -> SystemProfile:
    os_name = platform.system()
    
    # 1. Kiểm tra trên Windows / Linux có GPU NVIDIA
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)
        major, minor = torch.cuda.get_device_capability(0)
        
        # Kiến trúc Pascal (Compute Capability 6.x như GTX 1070)
        # Bắt buộc dùng int8 vì Pascal không có Tensor Cores FP16
        if major == 6:
            compute_type = "int8"
            rec_whisper = "small" if vram_gb <= 6 else "deepdml/faster-whisper-large-v3-turbo-ct2"
            rec_llm = "qwen2.5:3b"
        else:
            compute_type = "int8_float16" if vram_gb <= 8 else "float16"
            rec_whisper = "deepdml/faster-whisper-large-v3-turbo-ct2"
            rec_llm = "qwen2.5:3b" if vram_gb <= 8 else "qwen2.5:7b"
            
        return SystemProfile(
            os_name=os_name,
            device_name=f"{gpu_name} ({vram_gb}GB VRAM, SM {major}.{minor})",
            stt_backend="faster_whisper",
            stt_device="cuda",
            stt_compute_type=compute_type,
            recommended_whisper=rec_whisper,
            recommended_llm=rec_llm,
            vram_gb=vram_gb
        )
        
    # 2. Kiểm tra trên macOS Apple Silicon (M1/M2/M3)
    if os_name == "Darwin" and platform.machine() == "arm64":
        return SystemProfile(
            os_name="Darwin",
            device_name="Apple Silicon (M-Series Unified Memory)",
            stt_backend="mlx_whisper",
            stt_device="cpu",
            stt_compute_type="int8",
            recommended_whisper="mlx-community/whisper-large-v3-turbo",
            recommended_llm="qwen2.5:7b",
            vram_gb=32.0
        )
        
    # 3. Fallback CPU
    return SystemProfile(
        os_name=os_name,
        device_name="CPU Fallback",
        stt_backend="faster_whisper",
        stt_device="cpu",
        stt_compute_type="int8",
        recommended_whisper="small",
        recommended_llm="qwen2.5:3b",
        vram_gb=0.0
    )
```

### 4.2. `audio_capture.py` (Thu âm Hệ thống + Thu Microphone Push-To-Talk)

Module này gồm 2 class hoạt động độc lập:
1. `SystemAudioVADWorker`: Thu âm thanh phát ra từ hệ thống (chiều nghe) và tự động cắt câu bằng **Silero VAD**.
2. `MicPushToTalkRecorder`: Thu giọng nói tiếng Việt từ Microphone thật của bạn khi bạn nhấn giữ phím tắt **`F8`** (hoặc bấm nút trên giao diện) và đẩy ngay vào hàng đợi ưu tiên khi thả phím.

```
import platform
import queue
import threading
import numpy as np
import sounddevice as sd
import torch

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # Cửa sổ 32ms chuẩn cho Silero VAD ở 16kHz

class SystemAudioVADWorker(threading.Thread):
    def __init__(self, sentence_queue: queue.Queue, status_callback=None):
        super().__init__(daemon=True)
        self.sentence_queue = sentence_queue
        self.status_callback = status_callback
        self.running = False
        
        self.vad_model, _ = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=False
        )
        self.vad_model.eval()
        
        self.speech_threshold = 0.45
        self.min_silence_sec = 0.42
        self.max_sentence_sec = 7.5
        self.min_speech_sec = 0.35

    def log(self, msg: str):
        if self.status_callback:
            self.status_callback(msg)

    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        os_name = platform.system()
        if os_name == "Windows":
            self._capture_windows_wasapi()
        else:
            self._capture_macos_or_linux()

    def _process_audio_stream(self, float32_16k_generator):
        audio_buffer = []
        silence_samples = 0
        speech_detected = False
        remainder = np.array([], dtype=np.float32)

        for raw_chunk in float32_16k_generator:
            if not self.running:
                break
            combined = np.concatenate((remainder, raw_chunk))
            offset = 0
            while offset + CHUNK_SAMPLES <= len(combined):
                frame = combined[offset:offset + CHUNK_SAMPLES]
                offset += CHUNK_SAMPLES
                
                with torch.no_grad():
                    speech_prob = self.vad_model(torch.from_numpy(frame), SAMPLE_RATE).item()

                if speech_prob >= self.speech_threshold:
                    speech_detected = True
                    silence_samples = 0
                    audio_buffer.append(frame)
                else:
                    if speech_detected:
                        silence_samples += CHUNK_SAMPLES
                        audio_buffer.append(frame)

                total_samples = len(audio_buffer) * CHUNK_SAMPLES
                duration_sec = total_samples / SAMPLE_RATE
                silence_sec = silence_samples / SAMPLE_RATE

                if speech_detected and (silence_sec >= self.min_silence_sec or duration_sec >= self.max_sentence_sec):
                    if duration_sec >= self.min_speech_sec:
                        full_sentence = np.concatenate(audio_buffer)
                        self.sentence_queue.put(full_sentence)
                    audio_buffer.clear()
                    silence_samples = 0
                    speech_detected = False
                    self.vad_model.reset_states()

            remainder = combined[offset:]

    def _capture_windows_wasapi(self):
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            
            if not default_speakers["isLoopbackDevice"]:
                for loopback in p.get_loopback_device_info_generator():
                    if default_speakers["name"] in loopback["name"]:
                        default_speakers = loopback
                        break

            self.log(f"Đang bắt âm thanh hệ thống: {default_speakers['name']}")
            native_rate = int(default_speakers["defaultSampleRate"])
            channels = int(default_speakers["maxInputChannels"])

            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=native_rate,
                frames_per_buffer=1024,
                input=True,
                input_device_index=default_speakers["index"]
            )

            def generator():
                while self.running:
                    data = stream.read(1024, exception_on_overflow=False)
                    pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if channels > 1:
                        pcm = pcm.reshape(-1, channels).mean(axis=1)
                    if native_rate != SAMPLE_RATE:
                        target_len = int(len(pcm) * SAMPLE_RATE / native_rate)
                        pcm = np.interp(
                            np.linspace(0, len(pcm), target_len, endpoint=False),
                            np.arange(len(pcm)),
                            pcm
                        ).astype(np.float32)
                    yield pcm

            self._process_audio_stream(generator())
            stream.stop_stream()
            stream.close()
        except Exception as e:
            self.log(f"Lỗi thu âm WASAPI: {e}")
        finally:
            p.terminate()

    def _capture_macos_or_linux(self):
        import soundcard as sc
        try:
            mics = sc.all_microphones(include_loopback=True)
            selected_mic = None
            for m in mics:
                name_lower = m.name.lower()
                if "blackhole" in name_lower or "loopback" in name_lower or "monitor" in name_lower:
                    selected_mic = m
                    break
            if selected_mic is None:
                selected_mic = sc.default_microphone()
                self.log(f"Chú ý macOS: Đang thu từ '{selected_mic.name}'. Hãy cài BlackHole 2ch để thu riêng âm thanh hệ thống.")
            else:
                self.log(f"Đang bắt âm thanh hệ thống: {selected_mic.name}")

            with selected_mic.recorder(samplerate=SAMPLE_RATE, channels=1) as recorder:
                def generator():
                    while self.running:
                        data = recorder.record(numframes=1024)
                        yield data.reshape(-1).astype(np.float32)
                self._process_audio_stream(generator())
        except Exception as e:
            self.log(f"Lỗi thu âm macOS/Linux: {e}")


class MicPushToTalkRecorder:
    """Thu giọng nói tiếng Việt từ Micro thật khi người dùng giữ phím Push-to-Talk (F8)"""
    def __init__(self, outgoing_queue: queue.Queue, status_callback=None):
        self.outgoing_queue = outgoing_queue
        self.status_callback = status_callback
        self.is_recording = False
        self.frames = []
        self.stream = None
        self.mic_device_index = None  # None = Default Physical Mic

    def _audio_callback(self, indata, frames, time_info, status):
        if self.is_recording:
            self.frames.append(indata.copy().reshape(-1))

    def start_recording(self):
        if self.is_recording:
            return
        self.frames.clear()
        self.is_recording = True
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self.mic_device_index,
                callback=self._audio_callback
            )
            self.stream.start()
            if self.status_callback:
                self.status_callback("🎙️ ĐANG THU TIẾNG VIỆT... (Hãy nói vào Mic, thả F8 để dịch & phát tiếng Anh)")
        except Exception as e:
            self.is_recording = False
            if self.status_callback:
                self.status_callback(f"Lỗi mở Micro: {e}")

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass

        if len(self.frames) > 0:
            audio_np = np.concatenate(self.frames).astype(np.float32)
            duration = len(audio_np) / SAMPLE_RATE
            if duration >= 0.3:
                self.outgoing_queue.put(audio_np)
                if self.status_callback:
                    self.status_callback(f"⚡ Đang dịch câu nói của bạn ({duration:.1f}s) sang tiếng Anh...")
            else:
                if self.status_callback:
                    self.status_callback("Câu nói quá ngắn (< 0.3s), đã bỏ qua.")
```

### 4.3. `ai_engine.py` (Xử lý STT + Dịch thuật 2 Chiều Dùng chung VRAM)

Module này sử dụng một khóa `threading.Lock()` (`self.gpu_lock`) để điều phối GPU giữa Chiều Nghe và Chiều Nói. Nhờ vậy cả hai chiều dùng chung duy nhất 1 bản `Whisper` và 1 bản `Qwen / NLLB` trong bộ nhớ mà không tốn thêm bất kỳ $\text{MB}$ VRAM nào.

```
import threading
import time
from collections import deque
import numpy as np
import requests
from hardware_profile import SystemProfile

class LocalAIEngine:
    def __init__(self, profile: SystemProfile):
        self.profile = profile
        self.whisper_model = None
        self.stt_model_name = profile.recommended_whisper
        self.translation_mode = "ollama"  # "ollama" hoặc "nllb_ct2"
        self.ollama_model = profile.recommended_llm
        self.ollama_url = "http://127.0.0.1:11434/api/generate"
        self.source_lang = "en"           # Ngôn ngữ chiều nghe: "auto", "en", "ja", "zh", "ko"
        
        # Khóa đồng bộ GPU để chiều nghe và chiều nói dùng chung Model an toàn
        self.gpu_lock = threading.Lock()
        
        # Lưu 4 câu gần nhất cho chiều nghe
        self.context_history = deque(maxlen=4)
        
        # Biến cho NLLB-200 CTranslate2 INT8
        self.nllb_translator = None
        self.nllb_tokenizer_en = None
        self.nllb_tokenizer_vi = None

    def load_stt_model(self, model_name: str = None):
        with self.gpu_lock:
            if model_name:
                self.stt_model_name = model_name
            if self.profile.stt_backend == "faster_whisper" and self.whisper_model is None:
                from faster_whisper import WhisperModel
                self.whisper_model = WhisperModel(
                    self.stt_model_name,
                    device=self.profile.stt_device,
                    compute_type=self.profile.stt_compute_type
                )

    def transcribe(self, audio_np: np.ndarray, force_lang: str = None) -> tuple[str, float]:
        """Nhận diện giọng nói dùng chung cho cả Chiều Nghe và Chiều Nói (force_lang='vi')"""
        t0 = time.perf_counter()
        lang = force_lang if force_lang else (None if self.source_lang == "auto" else self.source_lang)

        with self.gpu_lock:
            if self.profile.stt_backend == "mlx_whisper":
                import mlx_whisper
                res = mlx_whisper.transcribe(
                    audio_np,
                    path_or_hf_repo=self.stt_model_name,
                    language=lang,
                    condition_on_previous_text=False
                )
                text = res.get("text", "").strip()
            else:
                if self.whisper_model is None:
                    self.load_stt_model()
                segments, _ = self.whisper_model.transcribe(
                    audio_np,
                    language=lang,
                    beam_size=1,
                    best_of=1,
                    vad_filter=False,
                    condition_on_previous_text=False
                )
                text = " ".join(seg.text.strip() for seg in segments).strip()

        stt_ms = (time.perf_counter() - t0) * 1000.0
        return text, stt_ms

    def translate_to_vietnamese(self, source_text: str) -> tuple[str, float]:
        """Chiều Nghe: Dịch từ Ngoại ngữ -> Tiếng Việt"""
        if not source_text:
            return "", 0.0
        t0 = time.perf_counter()
        if self.translation_mode == "ollama":
            vi_text = self._translate_ollama_to_vi(source_text)
        else:
            vi_text = self._translate_nllb(source_text, src_lang="eng_Latn", tgt_lang="vie_Latn")
        trans_ms = (time.perf_counter() - t0) * 1000.0
        self.context_history.append((source_text, vi_text))
        return vi_text, trans_ms

    def translate_vi_to_english(self, vi_text: str) -> tuple[str, float]:
        """Chiều Nói: Dịch từ Tiếng Việt -> Tiếng Anh chuyên nghiệp để phát qua Virtual Mic"""
        if not vi_text:
            return "", 0.0
        t0 = time.perf_counter()
        if self.translation_mode == "ollama":
            en_text = self._translate_ollama_vi_to_en(vi_text)
        else:
            en_text = self._translate_nllb(vi_text, src_lang="vie_Latn", tgt_lang="eng_Latn")
        trans_ms = (time.perf_counter() - t0) * 1000.0
        return en_text, trans_ms

    def _translate_ollama_to_vi(self, source_text: str) -> str:
        context_str = ""
        if self.context_history:
            lines = [f"- {src} => {vi}" for src, vi in self.context_history]
            context_str = "Ngữ cảnh các câu vừa nói trước đó:\n" + "\n".join(lines) + "\n\n"

        prompt = (
            f"{context_str}"
            f"Dịch câu thoại sau sang tiếng Việt tự nhiên, ngắn gọn, đúng ngữ cảnh hội thoại/phim ảnh. "
            f"CHỈ trả về đúng câu dịch tiếng Việt, tuyệt đối không giải thích:\n\"{source_text}\""
        )
        return self._call_ollama(prompt)

    def _translate_ollama_vi_to_en(self, vi_text: str) -> str:
        prompt = (
            f"Translate the following Vietnamese sentence into natural, conversational, professional spoken English "
            f"suitable for a live video call. Return ONLY the English translation, no quotes or explanation:\n\"{vi_text}\""
        )
        return self._call_ollama(prompt)

    def _call_ollama(self, prompt: str) -> str:
        try:
            resp = requests.post(
                self.ollama_url,
                json={
                    "model": self.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 120,
                        "num_ctx": 1024
                    }
                },
                timeout=8
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip().strip('"')
        except Exception as e:
            return f"[Lỗi Ollama ({self.ollama_model}): {e}]"

    def _ensure_nllb_loaded(self):
        if self.nllb_translator is None:
            import ctranslate2
            import transformers
            from huggingface_hub import snapshot_download
            
            model_dir = snapshot_download("entai2965/nllb-200-distilled-600M-ctranslate2")
            self.nllb_translator = ctranslate2.Translator(
                model_dir,
                device=self.profile.stt_device,
                compute_type="int8"
            )
            self.nllb_tokenizer_en = transformers.AutoTokenizer.from_pretrained(
                "facebook/nllb-200-distilled-600M", src_lang="eng_Latn"
            )
            self.nllb_tokenizer_vi = transformers.AutoTokenizer.from_pretrained(
                "facebook/nllb-200-distilled-600M", src_lang="vie_Latn"
            )

    def _translate_nllb(self, text: str, src_lang: str, tgt_lang: str) -> str:
        with self.gpu_lock:
            self._ensure_nllb_loaded()
            tokenizer = self.nllb_tokenizer_en if src_lang == "eng_Latn" else self.nllb_tokenizer_vi
            source_tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode(text))
            results = self.nllb_translator.translate_batch(
                [source_tokens],
                target_prefix=[[tgt_lang]],
                beam_size=1
            )
            target_tokens = results[0].hypotheses[0][1:]
            return tokenizer.decode(tokenizer.convert_tokens_to_ids(target_tokens))
```

### 4.4. `tts_virtual_mic.py` (Tổng hợp Giọng nói Tiếng Anh & Đẩy vào Micro Ảo)

Module này chịu trách nhiệm:
1. Tự động quét danh sách thiết bị âm thanh và tìm đầu vào của cáp ảo (**`CABLE Input`** của VB-Audio trên Windows hoặc **`BlackHole 2ch`** trên macOS).
2. Chuyển câu tiếng Anh vừa dịch thành âm thanh (hỗ trợ cả giọng đọc Neural tự nhiên qua `edge-tts` lẫn giọng đọc Offline `pyttsx3` $100\%$ không cần mạng, tốn $0\text{ MB}$ VRAM).
3. Phát luồng âm thanh đó thẳng vào Virtual Audio Cable để Zoom / Google Meet thu nhận như một Microphone thật.

```
import asyncio
import os
import tempfile
import time
import sounddevice as sd
import soundfile as sf

class VirtualMicTTSRouter:
    def __init__(self):
        self.virtual_mic_index = self.auto_detect_virtual_cable()
        self.tts_engine_mode = "neural_edge"  # "neural_edge" (giọng cực tự nhiên) hoặc "offline_native" (100% offline)
        self.voice_name = "en-US-AndrewNeural"
        self.monitor_to_speaker = False       # Nếu True, phát cho cả bạn nghe thấy giọng tiếng Anh của mình

    @staticmethod
    def list_output_devices() -> list[tuple[int, str]]:
        devices = sd.query_devices()
        out = []
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] > 0:
                out.append((idx, dev["name"]))
        return out

    @classmethod
    def auto_detect_virtual_cable(cls) -> int | None:
        """Tự động tìm VB-Audio Virtual Cable (Windows) hoặc BlackHole (macOS)"""
        for idx, name in cls.list_output_devices():
            low = name.lower()
            if "cable input" in low or "vb-audio" in low or "blackhole" in low:
                return idx
        return None

    def speak_to_virtual_mic(self, english_text: str) -> float:
        """Tổng hợp giọng nói tiếng Anh và đẩy vào Virtual Mic. Trả về thời gian xử lý TTS (ms)"""
        if not english_text:
            return 0.0

        t0 = time.perf_counter()
        tmp_file = tempfile.mktemp(suffix=".wav")

        try:
            if self.tts_engine_mode == "neural_edge":
                try:
                    self._synthesize_edge_tts(english_text, tmp_file)
                except Exception:
                    # Tự động fallback về Offline Native TTS nếu mất mạng
                    self._synthesize_offline_tts(english_text, tmp_file)
            else:
                self._synthesize_offline_tts(english_text, tmp_file)

            tts_ms = (time.perf_counter() - t0) * 1000.0

            # Đọc file âm thanh và đẩy thẳng vào thiết bị Virtual Cable
            data, sr = sf.read(tmp_file, dtype="float32")
            target_device = self.virtual_mic_index  # Nếu None sẽ phát ra loa mặc định để test

            sd.play(data, samplerate=sr, device=target_device, blocking=True)
            return tts_ms
        finally:
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass

    def _synthesize_edge_tts(self, text: str, out_path: str):
        import edge_tts
        async def _run():
            communicate = edge_tts.Communicate(text, self.voice_name, rate="+8%")
            await communicate.save(out_path)
        asyncio.run(_run())

    def _synthesize_offline_tts(self, text: str, out_path: str):
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        # Chọn giọng tiếng Anh nếu hệ điều hành có cài sẵn
        for v in engine.getProperty("voices"):
            if "en" in v.id.lower() or "english" in v.name.lower() or "david" in v.name.lower() or "zira" in v.name.lower():
                engine.setProperty("voice", v.id)
                break
        engine.save_to_file(text, out_path)
        engine.runAndWait()
```

### 4.5. `overlay_ui.py` (Giao diện Phụ đề 2 Chiều Xuyên thấu + Latency HUD)

Khung Overlay hiển thị đồng thời:
* **Dòng kiểm tra chiều nói (màu xanh ngọc):** Cho bạn biết câu tiếng Việt bạn vừa nói đã được dịch thành câu tiếng Anh gì khi truyền sang Zoom/Meet.
* **Khung phụ đề chiều nghe (màu vàng rực):** Phụ đề dịch trực tiếp lời của đối phương hoặc nội dung phim/YouTube.

```
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGraphicsDropShadowEffect
)

class SubtitleOverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI 2-Way Live Subtitle Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(960, 185)
        self._drag_pos = QPoint()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)

        self.container = QWidget(self)
        self.container.setStyleSheet("""
            QWidget {
                background-color: rgba(15, 18, 24, 190);
                border: 1px solid rgba(255, 255, 255, 45);
                border-radius: 12px;
            }
        """)
        box_layout = QVBoxLayout(self.container)

        # 1. Thanh Latency HUD & Trạng thái hệ thống
        self.hud_label = QLabel("Sẵn sàng | Giữ phím [F8] để nói Tiếng Việt -> Phát Tiếng Anh vào Meet/Zoom")
        self.hud_label.setStyleSheet("color: #7dd3fc; font-size: 11px; background: transparent; border: none;")
        box_layout.addWidget(self.hud_label)

        # 2. Thanh kiểm tra Chiều Nói (Outgoing Voice Feedback)
        self.outgoing_label = QLabel("🎙️ [Chiều nói VI -> EN]: Nhấn giữ phím F8 để phát biểu bằng tiếng Việt...")
        self.outgoing_label.setWordWrap(True)
        self.outgoing_label.setStyleSheet("color: #4ade80; font-size: 13px; font-weight: bold; background: transparent; border: none;")
        box_layout.addWidget(self.outgoing_label)

        # 3. Dòng phụ đề gốc Chiều Nghe (tiếng Anh/Nhật/Trung...)
        self.src_label = QLabel("Phụ đề gốc (Chiều nghe) sẽ hiển thị tại đây...")
        self.src_label.setWordWrap(True)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.src_label.setStyleSheet("color: #cbd5e1; font-size: 15px; background: transparent; border: none;")
        box_layout.addWidget(self.src_label)

        # 4. Dòng phụ đề tiếng Việt Chiều Nghe
        self.vi_label = QLabel("Bản dịch tiếng Việt thời gian thực")
        self.vi_label.setWordWrap(True)
        self.vi_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vi_label.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        self.vi_label.setStyleSheet("color: #fef08a; font-size: 22px; font-weight: bold; background: transparent; border: none;")
        
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(8)
        shadow.setColor(QColor(0, 0, 0, 220))
        shadow.setOffset(1, 1)
        self.vi_label.setGraphicsEffect(shadow)
        box_layout.addWidget(self.vi_label)

        layout.addWidget(self.container)

    def update_incoming_subtitle(self, src_text: str, vi_text: str, stt_ms: float, trans_ms: float, device_tag: str):
        total_ms = stt_ms + trans_ms
        self.hud_label.setText(
            f"[Chiều Nghe - {device_tag}] STT: {stt_ms:.0f} ms | Dịch: {trans_ms:.0f} ms | Tổng: {total_ms:.0f} ms  (Giữ F8 để nói)"
        )
        self.src_label.setText(src_text)
        self.vi_label.setText(vi_text)

    def update_outgoing_status(self, vi_said: str, en_spoken: str, stt_ms: float, trans_ms: float, tts_ms: float):
        total = stt_ms + trans_ms + tts_ms
        self.outgoing_label.setText(
            f"🎙️ Bạn nói: \"{vi_said}\"  ➔  🔊 Phát sang Meet: \"{en_spoken}\" "
            f"(STT: {stt_ms:.0f}ms | Dịch: {trans_ms:.0f}ms | TTS: {tts_ms:.0f}ms | Tổng: {total:.0f}ms)"
        )

    def set_click_through(self, enabled: bool):
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
```

### 4.6. `main.py` (Điều phối Đa luồng 2 Chiều + Global Hotkey `F8`)

Kết nối luồng Nghe (`IncomingWorker`), luồng Nói ưu tiên (`OutgoingVoiceWorker`), bộ lắng nghe phím tắt toàn cục `F8` (`pynput`) và giao diện chọn Micro ảo.

```
import sys
import queue
from pynput import keyboard
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QCheckBox, QLabel
)
from hardware_profile import detect_hardware_profile
from audio_capture import SystemAudioVADWorker, MicPushToTalkRecorder
from ai_engine import LocalAIEngine
from tts_virtual_mic import VirtualMicTTSRouter
from overlay_ui import SubtitleOverlayWindow

class IncomingInferenceWorker(QThread):
    result_ready = pyqtSignal(str, str, float, float)
    status_update = pyqtSignal(str)

    def __init__(self, sentence_queue: queue.Queue, engine: LocalAIEngine):
        super().__init__()
        self.sentence_queue = sentence_queue
        self.engine = engine
        self.running = True

    def run(self):
        self.status_update.emit(f"Đang nạp model STT ({self.engine.stt_model_name})...")
        try:
            self.engine.load_stt_model()
            self.status_update.emit("Sẵn sàng 2 Chiều! Bật video để xem phụ đề hoặc giữ phím [F8] để nói tiếng Việt.")
        except Exception as e:
            self.status_update.emit(f"Lỗi nạp model: {e}")
            return

        while self.running:
            try:
                audio_np = self.sentence_queue.get(timeout=0.4)
            except queue.Empty:
                continue

            while self.sentence_queue.qsize() > 2:
                try:
                    audio_np = self.sentence_queue.get_nowait()
                except queue.Empty:
                    break

            src_text, stt_ms = self.engine.transcribe(audio_np)
            if len(src_text.strip()) < 2:
                continue

            vi_text, trans_ms = self.engine.translate_to_vietnamese(src_text)
            self.result_ready.emit(src_text, vi_text, stt_ms, trans_ms)

    def stop(self):
        self.running = False


class OutgoingVoiceWorker(QThread):
    """Luồng ưu tiên xử lý Chiều Nói: Tiếng Việt (Mic) -> Tiếng Anh (Virtual Cable)"""
    outgoing_ready = pyqtSignal(str, str, float, float, float)
    status_update = pyqtSignal(str)

    def __init__(self, outgoing_queue: queue.Queue, engine: LocalAIEngine, tts_router: VirtualMicTTSRouter):
        super().__init__()
        self.outgoing_queue = outgoing_queue
        self.engine = engine
        self.tts_router = tts_router
        self.running = True

    def run(self):
        while self.running:
            try:
                audio_np = self.outgoing_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            # 1. Nhận diện tiếng Việt từ Mic
            vi_said, stt_ms = self.engine.transcribe(audio_np, force_lang="vi")
            if len(vi_said.strip()) < 2:
                self.status_update.emit("Không nghe rõ câu tiếng Việt, hãy thử nói lại.")
                continue

            # 2. Dịch sang tiếng Anh hội thoại
            en_text, trans_ms = self.engine.translate_vi_to_english(vi_said)

            # 3. Đọc câu tiếng Anh vào Virtual Microphone (VB-Cable / BlackHole)
            tts_ms = self.tts_router.speak_to_virtual_mic(en_text)

            self.outgoing_ready.emit(vi_said, en_text, stt_ms, trans_ms, tts_ms)

    def stop(self):
        self.running = False


class ControlBar(QWidget):
    ptt_state_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.profile = detect_hardware_profile()
        self.engine = LocalAIEngine(self.profile)
        self.tts_router = VirtualMicTTSRouter()

        self.incoming_queue = queue.Queue()
        self.outgoing_queue = queue.Queue()

        self.audio_worker = None
        self.incoming_ai_worker = None

        self.overlay = SubtitleOverlayWindow()
        self.overlay.show()

        self.ptt_state_signal.connect(self.overlay.outgoing_label.setText)

        # Khởi chạy sẵn luồng đợi Chiều Nói và bộ thu Push-To-Talk
        self.ptt_recorder = MicPushToTalkRecorder(
            self.outgoing_queue,
            status_callback=lambda msg: self.ptt_state_signal.emit(msg)
        )
        self.outgoing_worker = OutgoingVoiceWorker(self.outgoing_queue, self.engine, self.tts_router)
        self.outgoing_worker.outgoing_ready.connect(self.overlay.update_outgoing_status)
        self.outgoing_worker.status_update.connect(self.overlay.outgoing_label.setText)
        self.outgoing_worker.start()

        self._build_ui()
        self._start_global_hotkey_listener()

    def _build_ui(self):
        self.setWindowTitle("Local AI 2-Way Live Translator - Control Panel")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.resize(780, 150)

        main_layout = QVBoxLayout(self)
        info_label = QLabel(
            f"Phần cứng: {self.profile.device_name} | STT: {self.profile.stt_backend} ({self.profile.stt_compute_type})"
        )
        main_layout.addWidget(info_label)

        # Hàng 1: Điều khiển Chiều Nghe (Incoming)
        row1 = QHBoxLayout()
        self.btn_toggle = QPushButton("▶ Bật Dịch Chiều Nghe")
        self.btn_toggle.clicked.connect(self.toggle_capture)
        row1.addWidget(self.btn_toggle)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["en (Nghe Tiếng Anh)", "auto (Tự nhận diện)", "ja (Tiếng Nhật)", "zh (Tiếng Trung)", "ko (Tiếng Hàn)"])
        self.lang_combo.currentTextChanged.connect(self.on_lang_changed)
        row1.addWidget(self.lang_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            f"Ollama LLM ({self.profile.recommended_llm})",
            "NLLB-200 INT8 (Siêu nhẹ, không cần Ollama)"
        ])
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        row1.addWidget(self.mode_combo)

        self.chk_click = QCheckBox("Click xuyên qua phụ đề")
        self.chk_click.toggled.connect(self.overlay.set_click_through)
        row1.addWidget(self.chk_click)
        main_layout.addLayout(row1)

        # Hàng 2: Điều khiển Chiều Nói (Outgoing Virtual Mic & Push-to-Talk)
        row2 = QHBoxLayout()
        self.btn_ptt = QPushButton("🎙️ Giữ chuột (hoặc giữ phím F8) để Nói Tiếng Việt")
        self.btn_ptt.setStyleSheet("background-color: #166534; color: white; font-weight: bold; padding: 6px;")
        self.btn_ptt.pressed.connect(self.ptt_recorder.start_recording)
        self.btn_ptt.released.connect(self.ptt_recorder.stop_recording)
        row2.addWidget(self.btn_ptt)

        row2.addWidget(QLabel("Đầu ra Micro Ảo:"))
        self.vmic_combo = QComboBox()
        self._populate_virtual_mic_list()
        self.vmic_combo.currentIndexChanged.connect(self.on_vmic_changed)
        row2.addWidget(self.vmic_combo)

        self.tts_combo = QComboBox()
        self.tts_combo.addItems(["TTS: Neural Voice (Tự nhiên)", "TTS: Offline Native (0ms mạng)"])
        self.tts_combo.currentIndexChanged.connect(self.on_tts_mode_changed)
        row2.addWidget(self.tts_combo)
        main_layout.addLayout(row2)

    def _populate_virtual_mic_list(self):
        devices = self.tts_router.list_output_devices()
        detected_idx = self.tts_router.virtual_mic_index
        select_pos = 0
        self.vmic_combo.addItem("Loa mặc định (Dùng để nghe thử)", None)
        for pos, (dev_idx, name) in enumerate(devices, start=1):
            tag = " [★ Cáp Ảo Khuyên Dùng]" if dev_idx == detected_idx else ""
            self.vmic_combo.addItem(f"[{dev_idx}] {name}{tag}", dev_idx)
            if dev_idx == detected_idx:
                select_pos = pos
        self.vmic_combo.setCurrentIndex(select_pos)

    def _start_global_hotkey_listener(self):
        """Cho phép nhấn giữ phím F8 ngay cả khi đang mở toàn màn hình Zoom / Google Meet"""
        def on_press(key):
            if key == keyboard.Key.f8:
                self.ptt_recorder.start_recording()

        def on_release(key):
            if key == keyboard.Key.f8:
                self.ptt_recorder.stop_recording()

        self.kb_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.kb_listener.daemon = True
        self.kb_listener.start()

    def on_lang_changed(self, text: str):
        self.engine.source_lang = text.split(" ")[0]

    def on_mode_changed(self, idx: int):
        self.engine.translation_mode = "ollama" if idx == 0 else "nllb_ct2"

    def on_vmic_changed(self, idx: int):
        self.tts_router.virtual_mic_index = self.vmic_combo.currentData()

    def on_tts_mode_changed(self, idx: int):
        self.tts_router.tts_engine_mode = "neural_edge" if idx == 0 else "offline_native"

    def toggle_capture(self):
        if self.audio_worker and self.audio_worker.running:
            self.audio_worker.stop()
            if self.incoming_ai_worker:
                self.incoming_ai_worker.stop()
            self.btn_toggle.setText("▶ Bật Dịch Chiều Nghe")
            self.overlay.hud_label.setText("Đã tạm dừng chiều nghe. (Chiều nói F8 vẫn hoạt động)")
        else:
            self.btn_toggle.setText("⏹ Dừng Chiều Nghe")
            self.incoming_ai_worker = IncomingInferenceWorker(self.incoming_queue, self.engine)
            self.incoming_ai_worker.result_ready.connect(
                lambda s, v, t1, t2: self.overlay.update_incoming_subtitle(s, v, t1, t2, self.profile.stt_compute_type)
            )
            self.incoming_ai_worker.status_update.connect(self.overlay.hud_label.setText)
            self.incoming_ai_worker.start()

            self.audio_worker = SystemAudioVADWorker(
                self.incoming_queue,
                status_callback=lambda msg: self.overlay.hud_label.setText(msg)
            )
            self.audio_worker.start()

    def closeEvent(self, event):
        if self.audio_worker:
            self.audio_worker.stop()
        if self.incoming_ai_worker:
            self.incoming_ai_worker.stop()
        if self.outgoing_worker:
            self.outgoing_worker.stop()
        self.overlay.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = ControlBar()
    panel.show()
    sys.exit(app.exec())
```

## 5. Kịch bản Tự động Cài đặt & Khởi chạy cho Antigravity

### 5.1. Trên PC Windows (GTX 1070) — `setup_and_run.bat`

```
@echo off
echo [1/4] Khoi tao moi truong Python venv...
if not exist venv (
    python -m venv venv
)
call venv\Scripts\activate

echo [2/4] Cai dat PyTorch CUDA va cac thu vien cho GTX 1070...
pip install --upgrade pip
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements_win.txt

echo [3/4] Kiem tra Ollama model qwen2.5:3b...
where ollama >nul 2>nul
if %errorlevel%==0 (
    ollama pull qwen2.5:3b
) else (
    echo [Canh bao] Chua cai Ollama. Ban co the dung che do NLLB-200 INT8 co san trong App hoac tai Ollama tai https://ollama.com
)

echo [4/4] Khoi chay Local AI 2-Way Live Translator...
python main.py
pause
```

### 5.2. Trên macOS (Mac M1 Pro 32GB) — `setup_and_run.sh`

```
#!/bin/bash
set -e

echo "[1/4] Khởi tạo môi trường Python venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

echo "[2/4] Cài đặt thư viện tối ưu cho Apple Silicon (MLX + PyQt6)..."
pip install --upgrade pip
pip install -r requirements_mac.txt

echo "[3/4] Kiểm tra BlackHole 2ch (Virtual Audio Cable) và Ollama..."
if command -v ollama >/dev/null 2>&1; then
    ollama pull qwen2.5:7b
else
    echo "Gợi ý: Chạy 'brew install ollama && brew install --cask blackhole-2ch' để có đủ cáp ảo 2 chiều trên macOS."
fi

echo "[4/4] Khởi chạy Local AI 2-Way Live Translator..."
python3 main.py
```

## 6. Hướng dẫn Cài Cáp Âm Thanh Ảo & Đấu nối vào Zoom / Google Meet

Để đối phương trong cuộc gọi nghe được giọng tiếng Anh của AI (mà không nghe thấy tiếng Việt gốc của bạn):

1. **Cài đặt Driver Cáp Ảo miễn phí (Chỉ cần làm 1 lần):**
   * **Trên Windows (PC GTX 1070):** Tải và cài đặt **VB-Audio Virtual Cable** (`VBCABLE_Driver_Pack43.zip`). Sau khi cài, phần mềm của chúng ta sẽ tự động nhận diện thiết bị **`CABLE Input (VB-Audio Virtual Cable) [★ Cáp Ảo Khuyên Dùng]`**.
   * **Trên macOS (Mac M1 Pro):** Chạy lệnh `brew install --cask blackhole-2ch`. Phần mềm sẽ tự động chọn **`BlackHole 2ch`**.
2. **Thiết lập trong Zoom / Google Meet / Microsoft Teams:**
   * Ở mục **Microphone (Thiết bị thu âm)** của Zoom/Meet: Chọn **`CABLE Output (VB-Audio Virtual Cable)`** (trên Windows) hoặc **`BlackHole 2ch`** (trên Mac).
   * Ở mục **Speaker (Loa)** của Zoom/Meet: Giữ nguyên Tai nghe hoặc Loa ngoài của bạn như bình thường.
3. **Cách sử dụng trong cuộc họp thực tế:**
   * **Khi người khác nói tiếng Anh:** Bạn đọc phụ đề tiếng Việt trực tiếp trên khung Overlay.
   * **Khi đến lượt bạn phát biểu:** Bạn chỉ cần **nhấn giữ phím `F8`** trên bàn phím, nói câu tiếng Việt vào mic thật rồi **thả phím `F8`**. Ngay lập tức, dòng chữ màu xanh lá trên Overlay sẽ hiện câu tiếng Anh vừa dịch và phát giọng nói tiếng Anh truyền thẳng vào phòng họp Zoom/Meet.