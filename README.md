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
│   ├── INTERPRETER_SUMMARY_SPEC.md # Đặc tả kỹ thuật Tầng 2 "Thư ký Hợp nhất Ý chính" (Interval vs Pause)
│   ├── CHROME_EXTENSION_SPEC.md # Đặc tả kỹ thuật Chrome Extension "Dịch Video Tại Chỗ"
│   ├── HARDWARE_AND_VRAM.md     # Phân tích phần cứng (GTX 1070 SM 6.1 INT8 vs M1 Pro) & VRAM
│   └── EXTENSION_GUIDE.md       # Hướng dẫn mở rộng ngôn ngữ, nguồn âm thanh, tinh chỉnh VAD/TTS
├── chrome_extension/            # Chrome Extension MV3 "Dịch Video Tại Chỗ" (Alt+Shift+T)
│   ├── manifest.json            # Cấu hình MV3 (tabCapture, offscreen, nativeMessaging, cố định ID)
│   ├── service_worker.js        # Điều phối tabCapture, tự gọi Native Host bật backend & đồng bộ token
│   ├── offscreen.js             # AudioContext + AudioWorklet (pcm_worklet.js) + giữ nguyên tiếng tab
│   ├── content_script.js        # Shadow DOM phụ đề bám theo thẻ <video>, hỗ trợ Fullscreen & kéo thả
│   ├── subtitle_pacer.js        # Bộ điều tốc nhịp đọc phụ đề (port 1:1 từ overlay_ui.py)
│   ├── popup.html / popup.js    # Giao diện điều khiển nhanh trên thanh công cụ Chrome
│   └── options.html / options.js# Cài đặt cổng kết nối, cỡ chữ, độ mờ nền, từ điển chuyên ngành
├── native_host/                 # Chrome Native Messaging Host tự động bật bridge_server.py ngầm
│   ├── launcher.py              # Xử lý giao thức nhị phân stdio 4-byte LE (ensure_running, get_token, stop)
│   └── install_native_host.ps1  # Script đăng ký Registry HKCU + Windows Scheduled Task
├── main.py                      # Điều phối đa luồng (Desktop + Integrated Bridge), Global Hotkey F8 & Control Panel
├── incoming_pipeline.py         # Luồng suy luận Chiều Nghe dùng chung (IncomingInferenceWorker, MeetingTranscriptLogger)
├── interpreter_summary.py       # Luồng Thư ký Hợp nhất Ý chính (SummaryWorker, SummarySettings, Telemetry CSV)
├── browser_bridge.py            # Máy chủ HTTP/WebSocket nội bộ (127.0.0.1:47863) & TabAudioVADWorker
├── bridge_server.py             # Điểm vào chạy ngầm không cửa sổ (Headless) cho Chrome Extension
├── hardware_profile.py          # Tự động nhận diện OS, GPU (GTX 1070 vs M1 Pro) & chọn cấu hình tối ưu
├── audio_capture.py             # SystemAudioVADWorker (chiều nghe) & MicPushToTalkRecorder (chiều nói)
├── ai_engine.py                 # Quản lý STT + Dịch 2 chiều (EN<->VI) có khóa ưu tiên GPU (gpu_lock)
├── tts_virtual_mic.py           # Tổng hợp giọng nói Tiếng Anh & định tuyến thẳng vào Virtual Audio Cable
├── overlay_ui.py                # Giao diện PyQt6: Khung phụ đề xuyên thấu + Hiển thị kiểm tra chiều nói + HUD
├── requirements_win.txt         # Thư viện tối ưu cho Windows + CUDA (GTX 1070)
├── requirements_mac.txt         # Thư viện tối ưu cho macOS Apple Silicon (M1 Pro)
├── setup_and_run.bat            # Script tự động cài đặt và khởi chạy Desktop App trên Windows
├── start_bridge.bat             # Script bật nhanh Bridge Server ngầm cho Chrome Extension
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

---

## Hướng dẫn Sử dụng với Chrome Extension ("Dịch Video Tại Chỗ" — `Alt+Shift+T`)

Khi xem video học tập/hội thảo trên trình duyệt (YouTube, Coursera, Udemy, Vimeo, v.v.), bạn có thể dùng **Chrome Extension** để thu riêng âm thanh của đúng tab đang xem (không bị lẫn tiếng thông báo từ phần mềm khác) và hiển thị phụ đề song ngữ bám ngay bên trong khung hình `<video>` (kể cả khi phóng **Toàn màn hình / Fullscreen**):

1. **Bước 1 — Nạp tiện ích vào Chrome / Edge / Brave (Chỉ làm 1 lần):**
   - Mở địa chỉ `chrome://extensions/` (hoặc `edge://extensions/`).
   - Bật công tắc **Developer mode (Chế độ dành cho nhà phát triển)** ở góc trên bên phải.
   - Bấm nút **Load unpacked (Tải tiện ích đã giải nén)** và chọn thư mục `chrome_extension` trong dự án này.
   - Nhờ khóa công khai cố định trong `manifest.json`, tiện ích sẽ luôn nhận ID cố định: `cgbbacpgahelimaelidkgmphgcbiclfd`.
2. **Bước 2 — Đăng ký Native Host để Chrome tự động bật AI ngầm (Chỉ làm 1 lần):**
   - Mở PowerShell tại thư mục dự án và chạy lệnh:
     ```powershell
     powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ExtensionId cgbbacpgahelimaelidkgmphgcbiclfd
     ```
   - *(Tuỳ chọn)* Bạn cũng có thể bật sẵn ứng dụng Desktop (`setup_and_run.bat`) hoặc chạy `start_bridge.bat` thủ công bất kỳ lúc nào.
3. **Bước 3 — Bật phụ đề trực tiếp trên Video:**
   - Mở bất kỳ tab có video (ví dụ YouTube) và nhấn tổ hợp phím **`Alt+Shift+T`** (hoặc bấm vào biểu tượng tiện ích trên thanh công cụ Chrome rồi chọn **`▶ Bật dịch tab này`**).
   - Nếu ứng dụng Desktop chưa mở, Chrome sẽ tự động đánh thức tiến trình ngầm `bridge_server.py` (tự động tắt giải phóng VRAM sau 15 phút không dùng).
   - **Phím tắt tiện lợi trên trình duyệt:**
     - **`Alt+Shift+T`**: Bật / Tắt dịch cho tab hiện tại.
     - **`Alt+Shift+S`**: Chuyển đổi nhanh giữa chế độ *Song ngữ (Ngoại ngữ + Tiếng Việt)* và *Chỉ hiện Tiếng Việt*.
     - **`Alt+Shift+H`**: Ẩn / Hiện nhanh hộp phụ đề trên video.
   - Bạn có thể nắm thanh **`⠿ Dịch Tại Chỗ`** ở góc hộp phụ đề để kéo thả đến vị trí bất kỳ trong khung video (vị trí được tự động nhớ riêng cho từng trang web), và toàn bộ nội dung video được tự động lưu biên bản vào `meeting_logs/video_YYYY-MM-DD_HH-MM-SS.md`.

---

## 🧭 Thử nghiệm Ý chính (Thư ký Hợp nhất Ý chính — Tầng 2)

Bên cạnh phụ đề tức thời (Tầng 1), hệ thống cung cấp thẻ **`🧭 Ý chính đang bàn`** (Tầng 2) hoạt động song song với $0\text{ MB}$ VRAM phát sinh (chia sẻ cùng model `qwen2.5` ở `OLLAMA_NUM_CTX = 1024`, tự động nhường GPU tức thì khi có câu phụ đề mới cần dịch).

### Cách chọn nhanh chế độ và tham số theo tình huống
Bạn có thể đổi nóng ngay khi đang nghe (trên `ControlBar` của Desktop hoặc trong Popup của Chrome Extension) mà không cần khởi động lại:
- **Diễn thuyết nhanh / YouTube nói liên tục không nghỉ:** Chọn **`⏱ Theo chu kỳ`** (`interval`), đặt **`Mỗi (giây) = 30s`**, **`Tối thiểu câu = 3`**.
- **Họp đối thoại qua lại / Phỏng vấn / Hỏi đáp:** Chọn **`⏸ Theo nhịp nghỉ`** (`pause`), đặt **`Im lặng (giây) = 2.0s`**, **`Tối thiểu câu = 2`**.
- **Cần chốt ý ngay lập tức trước khi bật mic trả lời:** Bấm nút **`🔄 Tổng hợp ngay`**.

### Quy trình 4 kịch bản thực nghiệm để tự chọn chế độ mặc định phù hợp nhất
1. **Kịch bản 1 (`meeting_logs/a.mp4` hoặc video quen thuộc — 3 phút đầu):** Chạy ở chế độ **`⏱ Theo chu kỳ (30s)`**, `Tối thiểu câu = 3`.
2. **Kịch bản 2 (Cùng video đó — 3 phút tiếp theo):** Chuyển nóng sang **`⏸ Theo nhịp nghỉ (2.0s)`**, `Tối thiểu câu = 2`.
3. **Kịch bản 3 (Video nói cực nhanh / tranh luận trên YouTube qua Chrome Extension):** Thử cả hai chế độ (`⏱ Theo chu kỳ` vs `⏸ Theo nhịp nghỉ`) ngay trong Popup của tiện ích để cảm nhận độ nhịp nhàng của thẻ góc trên-phải video.
4. **Kịch bản 4 (Cuộc họp thực tế có dùng phím `F8` trả lời):** Kiểm tra xem dòng **`❓ [Câu hỏi của đối tác]`** trên bảng `🧭 Ý chính đang bàn` có giúp bạn nắm bắt câu hỏi và phản xạ bấm `F8` nhanh hơn không.

Sau khi thử nghiệm, chạy lệnh sau để in bảng thống kê đối chiếu khách quan từ dữ liệu telemetry (`meeting_logs/summary_metrics_*.csv` & `app_debug.log`):
```powershell
.\venv\Scripts\python.exe tests\summarize_metrics.py
```



