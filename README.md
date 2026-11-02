# Local AI 2-Way Live Translator (Desktop Overlay + Virtual Mic)

Phần mềm Desktop dịch thuật giọng nói **2 chiều thời gian thực (Full-Duplex)** chạy trực tiếp trên máy cá nhân, tối ưu hóa chuyên biệt cho **PC Windows (NVIDIA GTX 1070 8GB VRAM)** và **macOS Apple Silicon (M1/M2/M3 Pro 32GB RAM)**.

---

## Tính năng Cốt lõi

### 1. Chiều Nghe (Incoming Audio: Ngoại ngữ $\rightarrow$ Phụ đề Tiếng Việt)
- **System Audio Loopback:** Bắt trực tiếp âm thanh phát ra từ Zoom, Google Meet, Microsoft Teams, trình duyệt (YouTube) hoặc phần mềm xem phim (VLC) qua `WASAPI Loopback` (Windows) hoặc `BlackHole 2ch` (macOS).
- **Cắt câu thông minh (Silero VAD):** Phát hiện nhịp nghỉ tự nhiên ($0.42\text{ s}$) để tách luồng âm thanh thành từng câu hoàn chỉnh.
- **STT + Dịch thuật Ngữ cảnh:** Sử dụng `faster-whisper` (`CUDA int8`) hoặc `mlx-whisper` kết hợp **Ollama LLM** (`qwen2.5:3b` / `qwen2.5:7b`) hoặc **CTranslate2 NLLB-200** (`int8`).
- **Overlay Xuyên thấu (Click-Through):** Hiển thị phụ đề song ngữ nổi trên mọi cửa sổ kèm đồng hồ đo độ trễ (`Latency HUD`).

### 2. Chiều Nói (Outgoing Voice: Nói Tiếng Việt [Giữ `F8`] $\rightarrow$ Duyệt 1.5s [`Esc` hủy] $\rightarrow$ Phát Tiếng Anh qua Virtual Mic)
- **Global Push-to-Talk (`F8`) & Hủy khẩn cấp (`Esc`):** Nhấn giữ phím `F8` ở bất kỳ đâu (ngay cả khi đang mở toàn màn hình Zoom/Google Meet) để nói tiếng Việt, thả phím `F8` để dịch sang tiếng Anh.
- **Tái sử dụng 100% VRAM ($0\text{ MB}$ VRAM phát sinh):** Dùng khóa đồng bộ `gpu_lock` để chia sẻ cùng một bộ model Whisper và Qwen/NLLB đang nằm trong VRAM giữa Chiều Nghe và Chiều Nói.
- **Local TTS & Định tuyến Cáp Ảo:** Chuyển câu tiếng Anh thành giọng đọc tự nhiên (`edge-tts` tự động fallback về `pyttsx3` khi offline, tốn $0\text{ MB}$ VRAM) và đẩy thẳng vào **`CABLE Input (VB-Audio Virtual Cable)`** (Windows) hoặc **`BlackHole 2ch`** (macOS).
- **Kiểm tra Chéo trên Overlay:** Hiển thị rõ câu tiếng Việt vừa nhận diện và câu tiếng Anh phát sang phòng họp:
  `🎙️ Bạn nói: "[Câu tiếng Việt]" ➔ 🔊 Phát sang Meet: "[Câu tiếng Anh]"`

### 3. Bộ Tính năng Bảo vệ & Trợ lý Họp Công việc (Meeting Safety Mode)
- **`🔒 Ẩn khi Share màn hình` (Screen-Share Stealth):** Mặc định bật sẵn trên Windows (Win32 `WDA_EXCLUDEFROMCAPTURE`). Giúp ẩn hoàn toàn cả khung phụ đề Overlay lẫn bảng điều khiển `Control Panel` khỏi mắt người xem khi bạn Share toàn màn hình trên Zoom, Google Meet, Microsoft Teams hoặc OBS, trong khi mắt bạn vẫn nhìn rõ 100%.
- **`🛡️ Duyệt 1.5s (Esc hủy)` (Safety Preview Gate):** Hiển thị bản dịch tiếng Anh kèm đếm ngược `1.5s` trước khi đọc vào Micro ảo. Nếu phát hiện AI dịch sai ý hoặc chưa ưng ý, bạn chỉ cần nhấn phím **`Esc`** (hoặc nút **`✖ Hủy phát (Esc)`** / nhấn lại **`F8`** để nói lại) để chặn ngay lập tức; nhấn **`Esc`** khi đang đọc dở cũng ngắt âm thanh tức thì.
- **`📚 Từ điển họp` (`custom_glossary.txt`):** Cho phép nhập tên dự án, tên khách hàng, từ viết tắt và thuật ngữ chuyên ngành (`VD: Kubernetes, SLA, OKR, P&L -> Báo cáo lãi lỗ`) để mồi trực tiếp vào cả Whisper STT (2 chiều) lẫn Ollama LLM, tự động lưu lại cho các buổi họp sau.
- **`📝 Lưu biên bản` (`meeting_logs/`):** Tự động ghi lại biên bản cuộc họp song ngữ theo thời gian thực có gắn mốc giờ (`HH:MM:SS`) cho cả chiều Đối tác nói lẫn chiều Bạn phát biểu ra file Markdown (`meeting_logs/meeting_YYYY-MM-DD_HH-MM-SS.md`) để dễ dàng tra cứu lại con số, deadline và cam kết sau buổi họp.

---

## Cấu trúc Thư mục & Chuẩn AI Harness

Dự án tuân thủ chuẩn tài liệu **Claude Harness** giúp mọi AI Coding Agent (Claude Code, Antigravity, Cursor, Windsurf, Codex) đọc hiểu tức thì toàn bộ kiến trúc và luồng dữ liệu:

```text
.
├── CLAUDE.md                    # Điểm vào nhận thức chính cho Claude Code & các AI Agent
├── AGENTS.md                    # Quy tắc & hướng dẫn chuẩn hóa cho mọi AI Coding Agent
├── ARCHITECTURE.md              # Sơ đồ Mermaid kiến trúc, Sequence Diagram 2 chiều & Mô hình 6 luồng
├── README.md                    # Hướng dẫn cài đặt, cấu hình Virtual Cable & sử dụng
├── docs/
│   ├── HARDWARE_AND_VRAM.md     # Phân tích phần cứng (GTX 1070 SM 6.1 INT8 vs M1 Pro) & VRAM
│   └── EXTENSION_GUIDE.md       # Hướng dẫn mở rộng ngôn ngữ, đổi phím tắt, tinh chỉnh VAD/TTS
├── main.py                      # Điều phối đa luồng (Incoming + Outgoing), Global Hotkey F8 & Control Panel
├── hardware_profile.py          # Tự động nhận diện OS, GPU (GTX 1070 vs M1 Pro) & chọn cấu hình tối ưu
├── audio_capture.py             # SystemAudioVADWorker (chiều nghe) & MicPushToTalkRecorder (chiều nói)
├── ai_engine.py                 # Quản lý STT + Dịch 2 chiều (EN<->VI) có khóa ưu tiên GPU (gpu_lock)
├── tts_virtual_mic.py           # Tổng hợp giọng nói Tiếng Anh & định tuyến thẳng vào Virtual Audio Cable
├── overlay_ui.py                # Giao diện PyQt6: Khung phụ đề xuyên thấu + Hiển thị kiểm tra chiều nói + HUD
├── requirements_win.txt         # Thư viện tối ưu cho Windows + CUDA (GTX 1070)
├── requirements_mac.txt         # Thư viện tối ưu cho macOS Apple Silicon (M1 Pro)
├── setup_and_run.bat            # Script tự động cài đặt và khởi chạy trên Windows
└── setup_and_run.sh             # Script tự động cài đặt và khởi chạy trên macOS
```

---

## Hướng dẫn Cài đặt & Khởi chạy

### Trên PC Windows (NVIDIA GTX 1070 / RTX)
Chỉ cần chạy file script tự động:
```bat
setup_and_run.bat
```
Script sẽ tự động:
1. Tạo môi trường ảo `venv`.
2. Cài đặt PyTorch với CUDA (`cu121`) và các thư viện trong `requirements_win.txt` (tự động bỏ qua ở các lần chạy sau khi `venv` đã đủ thư viện).
3. **Tự động kiểm tra & cài đặt ngầm Ollama** (ưu tiên qua `winget`, tự động fallback tải trực tiếp `OllamaSetup.exe /SILENT` từ `ollama.com`), tự khởi chạy dịch vụ `ollama serve` và tự tải model `qwen2.5:3b` (nếu mất mạng không cài được thì ứng dụng vẫn tự động chuyển sang **NLLB-200 INT8**).
4. Khởi chạy `python main.py`.

### Trên macOS (Apple Silicon M1/M2/M3 Pro)
Chấp quyền thực thi và chạy script:
```bash
chmod +x setup_and_run.sh
./setup_and_run.sh
```

---

## Hướng dẫn Cài Cáp Âm Thanh Ảo & Đấu nối vào Zoom / Google Meet

Để đối phương trong cuộc gọi nghe được giọng tiếng Anh của AI (mà không nghe thấy tiếng Việt gốc của bạn):

1. **Cài đặt Driver Cáp Ảo miễn phí (Chỉ cần làm 1 lần):**
   - **Trên Windows:** Tải và cài đặt **VB-Audio Virtual Cable** (`VBCABLE_Driver_Pack43.zip`). Sau khi cài, phần mềm sẽ tự động nhận diện thiết bị **`CABLE Input (VB-Audio Virtual Cable) [★ Cáp Ảo Khuyên Dùng]`**.
   - **Trên macOS:** Chạy lệnh `brew install --cask blackhole-2ch`. Phần mềm sẽ tự động chọn **`BlackHole 2ch`**.
2. **Thiết lập trong Zoom / Google Meet / Microsoft Teams:**
   - Ở mục **Microphone (Thiết bị thu âm)** của Zoom/Meet: Chọn **`CABLE Output (VB-Audio Virtual Cable)`** (trên Windows) hoặc **`BlackHole 2ch`** (trên Mac).
   - Ở mục **Speaker (Loa)** của Zoom/Meet: Giữ nguyên Tai nghe hoặc Loa ngoài của bạn như bình thường.
3. **Cách sử dụng an toàn & hiệu quả trong cuộc họp thực tế:**
   - **Trước khi họp:** Điền nhanh tên dự án, tên khách hàng hoặc từ viết tắt chuyên ngành vào ô **`📚 Từ điển họp`** (`VD: Kubernetes, SLA, Sprint, P&L -> Báo cáo lãi lỗ`). Chế độ **`🔒 Ẩn khi Share màn hình`** và **`📝 Lưu biên bản`** đã được bật sẵn.
   - **Khi đối tác nói tiếng Anh:** Đọc phụ đề tiếng Việt trực tiếp trên khung Overlay (ngay cả khi bạn đang Share toàn màn hình, đối tác cũng không nhìn thấy khung này).
   - **Khi đến lượt bạn phát biểu:** Nhấn giữ phím **`F8`** trên bàn phím, nói câu tiếng Việt vào mic thật rồi thả phím **`F8`**. Khung Overlay sẽ hiện trước câu tiếng Anh trong **`1.5 giây`** để bạn kiểm tra nhanh: nếu hài lòng thì cứ để máy tự phát giọng tiếng Anh vào phòng họp; nếu muốn hủy hoặc nói lại, bấm ngay phím **`Esc`**.
   - **Sau buổi họp:** Bấm nút **`📂 Mở biên bản`** để mở file biên bản song ngữ có mốc thời gian trong thư mục `meeting_logs/`.

