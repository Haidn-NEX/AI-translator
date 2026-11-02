import os
import platform
import torch
from dataclasses import dataclass

# Đảm bảo CTranslate2 trên Windows luôn tìm thấy cublas64_12.dll và cudnn64_9.dll trong thư mục torch/lib
if platform.system() == "Windows":
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(_torch_lib):
        os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(_torch_lib)
            except OSError:
                pass


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
