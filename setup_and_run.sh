#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "[1/4] Khởi tạo môi trường Python venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

echo "[2/4] Kiểm tra và cài đặt thư viện tối ưu cho Apple Silicon (MLX + PyQt6)..."
if ! python3 -c "import torch, PyQt6, faster_whisper, ctranslate2" >/dev/null 2>&1; then
    pip install --upgrade pip
    pip install -r requirements_mac.txt
else
    echo "      Các thư viện Python đã được cài đặt đầy đủ trong venv."
fi

echo "[3/4] Kiểm tra và tự động cài đặt Ollama + Model LLM..."
if ! command -v ollama >/dev/null 2>&1; then
    echo "      Chưa tìm thấy Ollama. Đang tự động cài đặt Ollama..."
    if command -v brew >/dev/null 2>&1; then
        brew install ollama
    else
        curl -fsSL https://ollama.com/install.sh | sh || true
    fi
fi

if command -v ollama >/dev/null 2>&1; then
    if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        echo "      Đang khởi động dịch vụ ngầm Ollama server (127.0.0.1:11434)..."
        nohup ollama serve >/dev/null 2>&1 &
        for _ in {1..15}; do
            if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    fi
    REC_LLM=$(python3 -c "from hardware_profile import detect_hardware_profile; print(detect_hardware_profile().recommended_llm)" 2>/dev/null || echo "qwen2.5:7b")
    if ! ollama list 2>/dev/null | grep -q "$REC_LLM"; then
        echo "      Đang tải model dịch thuật $REC_LLM (chỉ tải 1 lần đầu)..."
        ollama pull "$REC_LLM" || true
    else
        echo "      [OK] Ollama server và model $REC_LLM đã sẵn sàng!"
    fi
else
    echo "      [Cảnh báo] Không thể tự động cài Ollama. Ứng dụng sẽ tự động dùng NLLB-200 INT8."
fi

echo "[4/4] Khởi chạy Local AI 2-Way Live Translator..."
python3 main.py
