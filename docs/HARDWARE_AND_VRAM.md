# Hardware Profiling & VRAM Budget Guide

> Reference documentation for `hardware_profile.py` and `ai_engine.py`.

---

## 1. Target Hardware Profiles

`detect_hardware_profile()` in `hardware_profile.py` automatically classifies the host system into one of three tiers:

| Profile Tier | Detection Condition | `stt_backend` | `stt_device` | `stt_compute_type` | `recommended_whisper` | `recommended_llm` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1a. NVIDIA Pascal (GTX 1070 8GB)** | `torch.cuda.is_available()` and `major == 6` | `faster_whisper` | `cuda` | **`int8`** (Mandatory) | `deepdml/faster-whisper-large-v3-turbo-ct2` (or `small` if $\le 6\text{ GB}$) | `qwen2.5:3b` |
| **1b. Modern NVIDIA (Turing/Ampere/Ada, SM $\ge 7$)** | `torch.cuda.is_available()` and `major != 6` | `faster_whisper` | `cuda` | `int8_float16` ($\le 8\text{ GB}$) or `float16` ($> 8\text{ GB}$) | `deepdml/faster-whisper-large-v3-turbo-ct2` | `qwen2.5:3b` ($\le 8\text{ GB}$) / `qwen2.5:7b` ($> 8\text{ GB}$) |
| **2. Apple Silicon (Mac M1/M2/M3 Pro)** | `os_name == "Darwin"` and `machine == "arm64"` | `mlx_whisper` | `cpu` (MLX uses Metal Unified Memory) | `int8` | `mlx-community/whisper-large-v3-turbo` | `qwen2.5:7b` |
| **3. CPU Fallback** | No CUDA & not macOS `arm64` | `faster_whisper` | `cpu` | `int8` | `small` | `qwen2.5:3b` |

---

## 2. Why GTX 1070 (SM 6.1) Must Use `int8`

The NVIDIA GeForce GTX 1070 uses the **Pascal** architecture (Compute Capability `6.1`).
- Pascal GPUs do **not** have dedicated FP16 Tensor Cores (which were introduced in Volta SM 7.0 / Turing SM 7.5), and FP16 throughput on GP104 is throttled ($1:64$ ratio vs FP32).
- However, Pascal GP104 natively supports **4-element vector INT8 dot-product instructions (`__dp4a`)** in CTranslate2 (`faster-whisper` and `ctranslate2.Translator`).
- Therefore, setting `compute_type="int8"` delivers maximum inference speed and reduces VRAM consumption by $\sim 50\%$.

---

## 3. VRAM Budget Breakdown (GTX 1070 — $8192\text{ MB}$ Total)

Because `LocalAIEngine` uses `self.gpu_lock = threading.RLock()` to share models between **Chiều Nghe** and **Chiều Nói**, the 2-way system uses the exact same VRAM as a 1-way system:

| Component | Mode A: Hybrid Whisper + Ollama Vulkan + NLLB Firewall | Mode B: Whisper + NLLB-200 CT2 Offline | Device / Notes |
| :--- | :--- | :--- | :--- |
| **Silero VAD (`ONNX`)** | $0\text{ MB}$ VRAM | $0\text{ MB}$ VRAM | Runs on CPU (`~2 ms` per $32\text{ ms}$ frame) |
| **Faster-Whisper (`large-v3-turbo` INT8)** | $\sim 1.1\text{ GB}$ VRAM | $\sim 1.1\text{ GB}$ VRAM | CUDA GPU 0 — Shared by Incoming & Outgoing (`vi`) |
| **Ollama LLM (`qwen2.5:3b` Q4_K_M)** | $\sim 1.9\text{ GB}$ VRAM (`OLLAMA_VULKAN=1`, `num_ctx=512`) | $0\text{ MB}$ VRAM | Context-aware 2-way business translation |
| **NLLB-200 (`distilled-600M` INT8)** | $\sim 0.65\text{ GB}$ VRAM (Pre-loaded CJK firewall) | $\sim 0.65\text{ GB}$ VRAM | CUDA GPU 0 — Instant `~100ms` zero-CJK & offline fallback |
| **TTS (`edge-tts` / `pyttsx3`)** | $0\text{ MB}$ VRAM | $0\text{ MB}$ VRAM | Network/CPU synthesis |
| **Windows DWM + Zoom/Meet + PyQt6** | $\sim 1.0\text{ GB}$ VRAM | $\sim 1.0\text{ GB}$ VRAM | OS & desktop overhead |
| **Total Peak VRAM** | **$\sim 4.65\text{ GB} / 8.0\text{ GB}$** | **$\sim 2.75\text{ GB} / 8.0\text{ GB}$** | Leaves $> 3.3\text{ GB}$ safe headroom |

---

## 4. GTX 1070 (SM 6.1) Ollama Vulkan Backend & Constant `num_ctx=512`

- **Vulkan Backend Isolation for Ollama `v0.40+`:** Newer Ollama CUDA builds (`cuda_v12`) compile PTX kernels requiring newer NVIDIA driver toolchains than CUDA 12.6 on Pascal `SM 6.1` (`HTTP 500: the provided PTX was compiled with an unsupported toolchain`). Both `setup_and_run.bat` and `LocalAIEngine._start_or_restart_ollama()` launch `ollama serve` with `OLLAMA_VULKAN=1` and `CUDA_VISIBLE_DEVICES=-1` **strictly in the `ollama serve` child environment**, so Ollama runs at full GPU speed via Vulkan while Python's `faster-whisper` and `ctranslate2` continue using CUDA GPU 0 (`int8`).
- **Constant `num_ctx=512` Invariant:** Never vary `options.num_ctx` between Ollama requests (e.g., between warmup probes and live chat calls), because changing `num_ctx` forces Ollama to unload and re-allocate the model runner in VRAM (`~5.8s` stall).

