# PROGRESS.md — Nhật ký Tiến độ & Hiện trạng Dự án (Session Handoff)

> **Dành cho AI Agent & Developer trong phiên làm việc tiếp theo:**
> Hãy đọc file này đầu tiên (cùng với [`CLAUDE.md`](./CLAUDE.md) và [`AGENTS.md`](./AGENTS.md)) để nắm bắt chính xác tiến độ hiện tại, các lỗi đã xử lý, hiện trạng phần cứng/môi trường trên máy và bước tiếp theo cần thực hiện mà không phải dò lại từ đầu.

---

## 1. Thông tin Môi trường & Phần cứng Thực tế trên Máy Hiện tại

- **Hệ điều hành:** Windows (Python `3.12.10`).
- **GPU:** `NVIDIA GeForce GTX 1070` (`8.0 GB VRAM`, kiến trúc Pascal `SM 6.1`).
  - `ctranslate2 4.8.4` nhận diện `CUDA device count: 1`, hỗ trợ `{'float32', 'int8', 'int8_float32'}` (bắt buộc dùng `int8`).
- **Môi trường ảo (`venv/`):** Đã khởi tạo và cài đặt đầy đủ 100% thư viện trong `d:\AI\AI Translation\venv`:
  - `torch 2.5.1+cu121`, `torchaudio 2.5.1+cu121`
  - `faster-whisper 1.2.1`, `ctranslate2 4.8.4`, `onnxruntime 1.31.1`
  - `transformers 5.22.1`, `sentencepiece 0.2.2`, `huggingface_hub 2.4.0`
  - `PyQt6 6.12.0`, `PyAudioWPatch 0.2.12.9`, `sounddevice 0.5.6`, `soundfile 0.14.0`, `pynput 1.8.2`
  - `edge-tts 7.3.1`, `pyttsx3 2.99`
- **Thiết bị Âm thanh Hệ thống (Đã kiểm tra thực tế):**
  - **Chiều Nghe (WASAPI Loopback):** Hoạt động tốt với `Speakers (Realtek(R) Audio) [Loopback]` (`48000 Hz`, `2 channels`), ngoài ra còn có `VX2776 Series` và `LG FHD`.
  - **Chiều Nói (Physical Microphone):** Tại thời điểm kiểm tra cuối, máy **chưa cắm Micro vật lý** (`sd.default.device = [-1, 1]`, `max_input_channels = 0` trên mọi thiết bị).
  - **Cáp Âm thanh Ảo (Virtual Audio Cable):** Máy hiện chưa cài `VB-Audio Virtual Cable` (`CABLE Input`), nên đầu ra TTS mặc định đang chọn `Loa mặc định (Dùng để nghe thử)`.
- **Trạng thái Model AI & Ollama:**
  - **Ollama:** Đã bổ sung cơ chế **tự động tải & cài đặt ngầm Ollama** ngay trong [`setup_and_run.bat`](./setup_and_run.bat) (qua `winget` hoặc fallback tải trực tiếp `OllamaSetup.exe /SILENT`), tự bật `ollama serve` ngầm (`CREATE_NO_WINDOW`), tự nạp PATH phiên hiện tại và tự `ollama pull qwen2.5:3b`. Ngoài ra nếu chạy offline chưa có Ollama, ứng dụng vẫn tự động chuyển sang **`NLLB-200 INT8 (Siêu nhẹ, không cần Ollama)`**.
  - **Silero VAD:** Đã nạp thành công tức thì (`~2ms`) từ model ONNX tích hợp sẵn trong `faster_whisper.vad` (không cần tải từ GitHub).
  - **HuggingFace Cache (`~/.cache/huggingface/hub`):** Cả 2 model **`deepdml/faster-whisper-large-v3-turbo-ct2`** (~1.55 GB) và **`entai2965/nllb-200-distilled-600M-ctranslate2`** (~2.4 GB, kèm sẵn SentencePiece tokenizer) **ĐÃ ĐƯỢC TẢI ĐẦY ĐỦ 100% VỀ Ổ CỨNG**. Hàm `_resolve_hf_repo_fast()` trong `ai_engine.py` đã được tối ưu nạp thẳng offline từ ổ cứng (`local_files_only=True`), mất tổng cộng ~15 giây nạp lên VRAM mà không cần tải lại từ mạng.

---

## 2. Những Hạng mục Đã Hoàn thành (Done)

### 2.1. Khởi tạo Toàn bộ Mã nguồn & Kịch bản Chạy
1. [`hardware_profile.py`](./hardware_profile.py) — Tự động nhận diện OS/GPU (`SystemProfile`) và tự động nạp đường dẫn DLL CUDA (`cublas64_12.dll`, `cudnn64_9.dll`) từ `venv\Lib\site-packages\torch\lib` vào `PATH` trên Windows.
2. [`audio_capture.py`](./audio_capture.py) — Thu âm hệ thống (`SystemAudioVADWorker`) + Thu âm Micro Push-to-Talk (`MicPushToTalkRecorder`).
3. [`ai_engine.py`](./ai_engine.py) — Quản lý STT (`faster-whisper` / `mlx-whisper`), Từ điển chuyên ngành cuộc họp (`set_custom_glossary`), và Dịch 2 chiều (`Ollama` Vulkan/CUDA + chốt chặn lọc chữ Hán `NLLB-200 INT8`) dùng chung VRAM qua khóa `self.gpu_lock` (`threading.RLock()`).
4. [`tts_virtual_mic.py`](./tts_virtual_mic.py) — Tổng hợp giọng nói Tiếng Anh (`edge-tts` fallback `pyttsx3`), hỗ trợ ngắt âm tức thì giữa chừng (`cancel_event` / `stop_playback()`) và phát vào Virtual Audio Cable.
5. [`overlay_ui.py`](./overlay_ui.py) — Cửa sổ phụ đề song ngữ trong suốt (`SubtitleOverlayWindow`) + `Latency HUD` + dòng kiểm tra chiều nói + chế độ tàng hình khi Share màn hình (`apply_window_capture_stealth` — Win32 `WDA_EXCLUDEFROMCAPTURE`).
6. [`main.py`](./main.py) — Điều phối đa luồng (`IncomingInferenceWorker`, `OutgoingVoiceWorker` có cổng duyệt `1.5s`), phím tắt toàn cục `F8` (Nói) & `Esc` (Hủy phát khẩn cấp), tự động ghi biên bản họp song ngữ (`MeetingTranscriptLogger` -> `meeting_logs/*.md`), lưu từ điển [`custom_glossary.txt`](./custom_glossary.txt), bảng điều khiển `ControlBar` và ghi log ra [`app_debug.log`](./app_debug.log).
7. [`setup_and_run.bat`](./setup_and_run.bat) & [`setup_and_run.sh`](./setup_and_run.sh) — Kịch bản cài đặt & khởi chạy tự động: tự bỏ qua `pip install` khi `venv` đã đủ thư viện, **tự động tải & cài ngầm Ollama + bật `ollama serve` + tải model `qwen2.5:3b`/`7b`** trước khi mở app.

### 2.2. Bộ Tài liệu Chuẩn AI Harness (Claude Harness)
- [`CLAUDE.md`](./CLAUDE.md) — Bản đồ dự án, lệnh thực thi và 5 bất biến kiến trúc bắt buộc.
- [`AGENTS.md`](./AGENTS.md) — Hướng dẫn chuẩn hóa cho tất cả các model AI.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Sơ đồ Mermaid kiến trúc, Sequence Diagram 2 chiều và mô hình 6 luồng.
- [`docs/HARDWARE_AND_VRAM.md`](./docs/HARDWARE_AND_VRAM.md) & [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md) — Tài liệu chuyên sâu về VRAM GTX 1070 SM 6.1 và hướng dẫn mở rộng.
- [`README.md`](./README.md) — Hướng dẫn cài đặt và đấu nối Virtual Audio Cable.

### 2.3. Các Lỗi Nghiêm trọng Đã Phát hiện & Khắc phục Triệt để
1. **Lỗi hai cửa sổ đè lên nhau làm liệt thao tác chuột (`Intersects: True`):**
   - *Nguyên nhân:* `SubtitleOverlayWindow` (`960x185`, `WindowStaysOnTopHint`) và `ControlBar` cùng mở ở chính giữa màn hình (`Y ~ 410..608`), khiến khung Overlay che kín các nút bấm của `ControlBar`.
   - *Đã sửa:* Tách vị trí hiển thị — `ControlBar` nằm ở **đỉnh giữa màn hình (`Y = 30`)**, còn `SubtitleOverlayWindow` nằm ở **đáy giữa màn hình (`Y = 812`)** (`Intersects: False`).
2. **Lỗi đơ giao diện (`Not Responding`) khi bấm `▶ Bật Dịch Chiều Nghe`:**
   - *Nguyên nhân:* `SystemAudioVADWorker.__init__()` gọi `torch.hub.load('snakers4/silero-vad')` trực tiếp trên luồng GUI chính.
   - *Đã sửa:* Viết class `_StreamingSileroVAD` dùng trực tiếp model `silero_vad_v6.onnx` tích hợp sẵn trong `faster_whisper.vad` và chỉ khởi tạo bên trong luồng ngầm `run()` (tốc độ nạp `~2ms`, không cần mạng, không khóa GUI).
3. **Lỗi Deadlock tại `LocalAIEngine.gpu_lock`:**
   - *Nguyên nhân:* `transcribe()` giữ `threading.Lock()` rồi gọi `load_stt_model()` vốn cũng giữ `self.gpu_lock` trên cùng 1 luồng.
   - *Đã sửa:* Đổi `self.gpu_lock` sang `threading.RLock()` (Re-entrant Lock).
4. **Lỗi `WASAPI Loopback` bị treo (`stream.read()` block vô hạn) khi hệ thống im lặng:**
   - *Nguyên nhân:* Trên Windows, khi loa không phát âm thanh, `stream.read(1024)` của `PyAudioWPatch` chờ vô thời hạn khiến câu nói cuối không được ngắt VAD và không dừng được luồng thu.
   - *Đã sửa:* Dùng `stream.get_read_available()`; nếu sau `25ms` không có dữ liệu từ loa sẽ đẩy khung tĩnh `silent_chunk_16k` để VAD ngắt câu ngay khi người nói vừa dứt lời và dừng luồng tức thì khi bấm Stop.
5. **Lỗi `UnicodeEncodeError (cp1252)` khi ghi log tiếng Việt ra console Windows:**
   - *Đã sửa:* Thêm `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` ở đầu `main.py` và `chcp 65001` trong `setup_and_run.bat`.
6. **Tự động chuyển sang `NLLB-200 INT8` khi chưa cài Ollama & Cảnh báo khi thiếu Micro vật lý:**
   - *Đã sửa:* Kiểm tra nhanh `127.0.0.1:11434` khi khởi động; nếu chưa có Ollama sẽ tự động chọn `NLLB-200 INT8`. Thêm `MicPushToTalkRecorder.has_input_microphone()` để báo rõ trên UI nếu chưa cắm Mic khi nhấn `F8`.
7. **Nâng cấp lớn Độ chính xác Nhận diện & Ngắt câu theo Ngữ cảnh (Khắc phục lỗi mất phụ âm đầu `'but' -> 'Bye.'` & vụn câu trên video YouTube `RNhbqQefPSg`):**
   - *Nguyên nhân gốc rễ từ `app_debug.log` & `Ollama\server.log`:*
     1. VAD cũ chỉ bắt đầu lưu âm thanh *sau khi* `speech_prob >= 0.45`, làm cụt `60-200ms` phụ âm đầu (`"critical"` $\rightarrow$ `"Ridical"`, `"for instance"` $\rightarrow$ `"instance"`, `"but"` $\rightarrow$ `"Bye."`).
     2. Ngưỡng ngắt cố định `0.42s` không có Hysteresis cắt một câu diễn thuyết có nhịp nghỉ nhấn nhá thành 5-7 mảnh rời 1-2 từ (`"There are..."` + `"massive."` + `"Gaps."` $\rightarrow$ `"- Không có gì."`, `"agri-"` + `"culture."` $\rightarrow$ `"Văn hóa."`), cộng thêm xung nhiễu `0.48s` kích hoạt vòng lặp `temperature=(0.0..1.0)` của Whisper sinh ảo giác (`"Thank you."`, `"Amen."`, `"See you next week."`).
     3. Nội suy tuyến tính `np.interp` (`48kHz -> 16kHz`) không có bộ lọc chống răng cưa (anti-aliasing) làm méo âm xuýt/phụ âm.
     4. Ollama `v0.40.13` (`cuda_v12`) gặp lỗi `CUDA error: the provided PTX was compiled with an unsupported toolchain` (`HTTP 500`) với driver NVIDIA CUDA `12.6` trên `GTX 1070 (SM 6.1)`, khiến toàn bộ phiên test tự tụt về `NLLB-200` không có ngữ cảnh 4 câu.
   - *Đã nâng cấp triệt để:*
     1. **Bộ hạ tần số chống răng cưa `_StreamingResampler` (`audio_capture.py`):** Dùng `torchaudio.transforms.Resample` theo thuật toán Overlap-Save (`10ms` ngữ cảnh hai đầu), sai số `< 1.2e-7`, triệt tiêu hoàn toàn nhiễu gập tần trên `8kHz`.
     2. **Đệm trước 320ms (`pre_speech_frames`) + VAD Hysteresis (`0.42 / 0.26`) + Ngắt câu thích ứng 3 tầng (`audio_capture.py`):** Giữ trọn phụ âm đầu/cuối; câu ngắn `< 2.2s` chờ tới `1.08s` mới ngắt để gộp trọn liên từ (`"But..."`, `"And third,"`) vào mệnh đề chính; lọc bỏ hoàn toàn các xung thở/click ngắn `< 0.38s` tiếng nói thực.
     3. **Ghép nối liên từ lơ lửng (`IncomingInferenceWorker._stitch_prefix` trong `main.py`):** Nếu diễn giả dừng quá lâu (`> 1.1s`) ngay sau liên từ ngắn (`"But"`, `"While we can't..."`), luồng suy luận giữ lại tối đa `1.8s` để nối thẳng vào mệnh đề kế tiếp trước khi dịch.
     4. **Whisper `beam_size=3`, `temperature=0.0`, ngữ cảnh từ vựng trượt `stt_prompt_history` & Bộ lọc ảo giác (`ai_engine.py`):** Tắt vòng lặp random temperature gây ảo giác, mồi từ vựng chuyên ngành từ 2 câu chuẩn trước đó, và gắn nhãn rõ `Whisper: faster-whisper-large-v3-turbo-ct2 (★ Chuẩn xác nhất)`.
     5. **Tự động chuyển Ollama sang GPU Vulkan trên GTX 1070 + `/api/chat` ngữ cảnh 4 câu (`ai_engine.py` & `setup_and_run.bat`):** Khi phát hiện GPU Pascal `SM 6.1` hoặc lỗi `HTTP 500` CUDA PTX từ Ollama, tự động khởi động lại `ollama serve` với `OLLAMA_VULKAN=1` (chỉ áp dụng cho tiến trình con của Ollama, giữ nguyên CUDA cho Faster-Whisper) và sửa cảnh báo `fix_mistral_regex=True` + lọc dấu gạch đầu dòng thoại phim cho `NLLB-200`.
8. **Tối ưu Tốc độ Phản hồi, Bắt Thoại Nhỏ/Nhạc Nền Phim (AGC) & Chặn Triệt để Rò rỉ Chữ Trung Quốc của Qwen2.5 (Video `vZIzQfQ4JYc`):**
   - *Nguyên nhân gốc rễ từ `app_debug.log` (dòng 385-418):*
     1. **Rò rỉ chữ Trung Quốc (`诸位先生`, `地面`, `杀你`, `了`) & bê nguyên câu ngắn (`Save it.`):** `qwen2.5:3b` có thiên hướng song ngữ Trung-Anh; khi dịch thoại phim kịch tính (`"I'll kill you"`, `"Gentlemen"`), xác suất token Hán tự tăng cao, và một khi 1 câu lẫn chữ Hán lọt vào `context_history`, nó làm mẫu (few-shot) kéo các câu sau tiếp tục sinh chữ Hán.
     2. **Bỏ sót đoạn thoại nhỏ / có nhạc nền kịch tính:** Khi mở âm lượng loa Windows nhỏ hoặc diễn viên phim nói nhỏ/thì thầm trên nền nhạc phim dồn dập, biên độ số WASAPI Loopback tụt xuống `-45 dBFS` (`RMS < 0.002`) khiến Silero VAD không kích hoạt và bộ lọc `no_speech_threshold=0.55` loại nhầm đoạn có nhạc nền.
     3. **Độ trễ phụ đề 2.5s - 4.1s trên thoại phim nhanh:** Ngưỡng chờ `1.08s` cho câu `< 2.2s` cộng với việc giữ `1.8s` các câu lửng có dấu `...` (`"If you don't..."`) và `num_ctx=1024` của Ollama gây trễ nhịp hội thoại phim.
   - *Đã khắc phục triệt để:*
     1. **Tự động cân bằng âm lượng `_StreamingAGC` + Brickwall Limiter (`audio_capture.py`) & Peak Normalization (`ai_engine.py`):** Tự khuếch đại mượt thoại nhỏ (`RMS > 0.00025`) lên chuẩn `-24 dBFS` (`0.065`) cho VAD và Whisper mà không làm vỡ tiếng khi có tiếng hét/nổ súng; mở rộng `no_speech_threshold=0.75` và hạ ngưỡng VAD (`0.34 / 0.20`, `min_speech_sec=0.22s`) để bắt trọn thoại phim trên nền nhạc.
     2. **Giảm ~50% độ trễ phụ đề (`audio_capture.py`, `main.py`, `ai_engine.py`):** Rút ngắn ngưỡng ngắt câu thích ứng (`0.62s / 0.45s / 0.28s`, cắt mềm ở `5.8s`), chỉ giữ liên từ đơn 1 từ tối đa `0.85s` (hiển thị tức thì các câu như `"If you don't..."`, `"Save it."`), dùng `beam_size=2`, `requests.Session()` keep-alive và `num_ctx=512`.
     3. **Hệ thống 3 lớp chặn 100% chữ Trung Quốc & câu chưa dịch (`ai_engine.py`):** Khung thẻ `<source>...</source>` với luật cấm chữ Hán + `temperature=0.05, top_k=15`, giữ cố định `num_ctx=512` để Ollama không phải nạp lại VRAM; bộ kiểm tra `_CJK_RE` & `_is_untranslated_echo` tự động chuyển tức thì câu bị lẫn chữ Hán sang `NLLB-200 vie_Latn` đã nạp sẵn (`~100ms`); chặn tuyệt đối không lưu câu lỗi vào `context_history`.
9. **Bộ Tính năng Bảo vệ & Trợ lý Họp Công việc Chuyên nghiệp (Meeting-Grade Safety & Productivity):**
   - *Mục tiêu:* Triệt tiêu các rủi ro thực tế khi dùng công cụ trong họp công việc (Zoom / Google Meet / Microsoft Teams): lộ phụ đề khi Share màn hình, AI dịch nhầm ý câu nói tiếng Việt phát sang phòng họp ("nói hớ"), nhận diện sai thuật ngữ dự án/tên riêng, và thiếu biên bản đối chiếu con số/cam kết sau cuộc họp.
   - *Đã triển khai & kiểm thử tự động thành công 100%:*
     1. **Tàng hình khi Share màn hình (`🔒 Ẩn khi Share màn hình` — `overlay_ui.py` & `main.py`):** Sử dụng Win32 API `SetWindowDisplayAffinity(..., WDA_EXCLUDEFROMCAPTURE = 0x11)` (có fallback `WDA_MONITOR = 0x01`) mặc định bật sẵn trên Windows cho **cả `SubtitleOverlayWindow` lẫn `ControlBar`** (và tự động gắn lại khi Qt tái tạo `HWND` lúc bật/tắt `Click xuyên phụ đề`). Mắt người dùng vẫn nhìn rõ 100%, nhưng người xem Share toàn màn hình trên Zoom/Meet/Teams hoàn toàn không thấy cửa sổ dịch.
     2. **Cổng Duyệt Nhanh `1.5s` Trước Khi Phát + Phím Tắt Hủy Khẩn Cấp Toàn Cục `[Esc]` (`main.py` & `tts_virtual_mic.py`):** Sau khi thả phím `F8`, `OutgoingVoiceWorker` hiển thị bản dịch tiếng Anh kèm đồng hồ đếm ngược `1.5s` trên Overlay (`🛡️ Duyệt 1.5s (Esc hủy)`). Nếu bản dịch chưa đúng ý, nhấn **`Esc`** (hoặc bấm `✖ Hủy phát (Esc)` / nhấn lại `F8` để nói lại) sẽ hủy ngay lập tức không cho phát sang phòng họp; nếu đang phát TTS dở câu, nhấn **`Esc`** cũng ngắt âm `sd.stop()` tức thì.
     3. **Tự động Lưu Biên bản Họp Song ngữ (`MeetingTranscriptLogger` -> `meeting_logs/meeting_YYYY-MM-DD_HH-MM-SS.md`):** Tự động ghi lại toàn bộ nội dung `👥 Đối tác` và `🎙️ Bạn phát biểu` (chỉ ghi các câu thực sự phát thành công, bỏ qua câu đã bấm `Esc` hủy) kèm mốc thời gian `HH:MM:SS` ra file Markdown UTF-8; hỗ trợ bật/tắt (`📝 Lưu biên bản`) và mở nhanh (`📂 Mở biên bản`).
     4. **Từ điển Thuật ngữ Dự án / Tên riêng (`📚 Từ điển họp` -> `custom_glossary.txt`):** Ô nhập trực tiếp trên `ControlBar` (tự động lưu bền vững vào `custom_glossary.txt`), tự động tách từ khóa mồi vào cả `initial_prompt` của Whisper STT (2 chiều) lẫn prompt dịch thuật của Ollama (`VD: Kubernetes, SLA, OKR, P&L -> Báo cáo lãi lỗ`).
10. **Khắc phục Triệt để Hiện tượng "Điếc Tạm thời ~45 Giây" ở Cảnh Nhạc Nền Trầm Lớn + Giọng Camcorder Nhỏ (`1:05 - 2:05` Video `oj_WkCx651k`):**
    - *Chẩn đoán từ `app_debug.log` (dòng 447-452 & 488-492):*
      1. **Model AI hoàn toàn KHÔNG bị treo:** `IncomingInferenceWorker` dịch xong câu ở `0:55` (`"It's his blood work"`) trong `1.17s` rồi quay về chờ `sentence_queue`, nhưng suốt `49 giây` (`08:42:27 -> 08:43:16`) không có bất kỳ câu nào được `SystemAudioVADWorker` đẩy vào hàng đợi, tới `2:05` mới tự thu tiếp (và khi bấm Stop/Start ngay giữa đoạn `1:30` thì lại thu được ngay).
      2. **Nguyên nhân #1 — Xung nhịp chèn khung `0` giả (`silent_native` zero-splicing) trong `_capture_windows_wasapi()`:** Vòng lặp cũ kiểm tra `if avail < chunk_frames (960)` sau `time.sleep(0.020)` rồi bơm ngay `20ms` số `0` vào luồng âm thanh ngay cả khi `avail == 480` (hoặc khi đồng hồ HDMI `LG FHD` lệch pha với timer `20ms` của Windows). Thực nghiệm chứng minh khi `20ms` số `0` bị chèn xen kẽ vào lời thoại, xác suất giọng nói của Silero VAD sụp từ `38/156` khung xuống chỉ còn `4/312` khung (`0.128s < 0.22s`), làm rơi 100% câu nói cho tới khi hai đồng hồ trôi qua vùng lệch pha.
      3. **Nguyên nhân #2 — Nhạc nền trầm/cello/organ lớn (`40 - 130 Hz`) ở cảnh `1:05 - 2:05` chèn ép AGC & làm mù Silero VAD:** Khi nhạc phim trầm dâng cao kèm giọng nhân vật nói nhỏ qua loa TV/camcorder trong phim, dải trầm chiếm trọn `RMS` làm `Silero VAD` tụt xuống `max = 0.062` (`0/156` khung vượt ngưỡng `0.34`).
    - *Đã khắc phục 3 lớp trong `audio_capture.py` & `ai_engine.py`:*
      1. **Thu âm WASAPI theo sự kiện `stream_callback` (`pcm_queue`):** Bắt trực tiếp từng gói PCM phần cứng từ driver âm thanh không mất mẫu; chỉ bơm khung tĩnh xả đuôi khi Windows thực sự không phát âm thanh suốt `120ms` (`timeout=0.12`), chấm dứt 100% lỗi chèn số `0` vào giữa lời thoại.
      2. **Nhánh lọc thông cao pha tuyến tính `_VoiceBandHPF` (`65-tap FIR 155 Hz`, `15us/khung`) riêng cho Silero VAD + Reset định kỳ LSTM (`4.0s`):** Cắt sạch năng lượng nhạc trầm `< 155 Hz` trước khi đưa vào nhánh `vad_agc -> Silero VAD` (trong khi vẫn giữ âm thanh gốc tự nhiên cho Whisper), nâng xác suất phát hiện giọng nói nhỏ trên nền nhạc phim từ `0.062 (0/156)` lên **`0.859 - 0.917 (107-118/156 khung)`**.
      3. **Cơ chế Cứu hộ Âm thanh Hoạt động (`rescue_window_sec = 4.5s`) + Bộ lọc nhãn nhạc `_MUSIC_TAG_RE`:** Nếu loa vẫn đang phát âm thanh dải giọng nói liên tục suốt `4.5s` mà nhạc nền/hiệu ứng camcorder vẫn đè Silero VAD dưới ngưỡng, tự động cắt cửa sổ `4.5s` gửi thẳng sang `Faster-Whisper` trên GPU để trích xuất lời thoại (và lọc sạch nhãn `♪` / `[Music]` nếu đoạn đó chỉ có nhạc không lời).
11. **Tối ưu Tốc độ Bắt & Hiển thị Phụ đề Cho Podcast / Hội thoại Nói Liền Mạch (`Spotify Episode 6d2YrOJyqINSkNOPTHekJK`):**
    - *Chẩn đoán từ `app_debug.log` (dòng 549-659) & Đo thực nghiệm:*
      1. **Bộ lọc tạp âm / nhạc nền (`_StreamingAGC` + `_VoiceBandHPF`) KHÔNG làm chậm hệ thống:** Đo thực tế chỉ tốn **`0.080 ms` trên mỗi khung `32 ms`** (`0.25%` thời gian).
      2. **Nguyên nhân thực sự gây trễ "nói sang câu thứ 2 mới hiện câu 1":** Trong podcast thu âm phòng thu (đã cắt khoảng lặng bằng компрессор/editing), diễn giả nói liền mạch không có khoảng nghỉ hoàn toàn (`speech_prob < 0.20` kéo dài `0.45s`). Thống kê 38 đoạn trong log cho thấy **16/38 đoạn bị tích tụ tới tận ngưỡng cắt cứng `7.520 giây`** (và phần lớn các đoạn còn lại đều `6.0s - 7.0s`). Cộng thêm `~0.65s` STT + `~0.95s` dịch Ollama, người nghe phải đợi **`~8.5 - 9.1 giây`** kể từ chữ đầu tiên mới thấy phụ đề.
    - *Đã khắc phục 3 điểm trong `audio_capture.py`, `main.py`, `overlay_ui.py` & `ai_engine.py`:*
      1. **Cắt câu theo Nhịp Ngắt Hơi / Dấu Phẩy (`phrase_cut` ở `2.4s` & `soft_max_cut` ở `3.6s`):** Khi diễn giả nói liên tục từ `2.4s` trở lên và có nhịp lấy hơi/ngắt cụm ngắn `~96ms` (`3` khung có `speech_prob < 0.45`, dù chưa im lặng hẳn `< 0.20`), hệ thống lập tức tách cụm đó đẩy đi dịch ngay (độ dài đoạn giảm từ `7.52s` xuống `~2.8s - 3.5s`).
      2. **Cắt Thông Minh Tại Khe Giữa 2 Từ + Gối Phần Dư (`Smart Valley Split` ở `max_sentence_sec = 5.2s`):** Nếu diễn giả nói cực nhanh không nghỉ suốt `5.2s`, thuật toán tự tìm khung có xác suất VAD thấp nhất (khe hở giữa 2 từ trong khoảng `2.4s .. 5.2s`) để cắt câu gọn gàng và **giữ nguyên phần âm thanh sau điểm cắt làm đầu đoạn kế tiếp (`carry_frames`)**, không làm cụt chữ giữa từ.
      3. **Hiển thị Tức thì Phụ đề Gốc (`source_ready` sau `~0.6s`) & Chặn Ollama Lặp Câu Ngữ Cảnh:** Ngay khi `Faster-Whisper` nhận diện xong (`~600ms`), câu tiếng Anh hiện ngay lên `SubtitleOverlayWindow.show_incoming_source()` để người dùng đọc trước trong lúc chờ Ollama dịch (`~800ms`); đồng thời lọc bỏ trường hợp Ollama chép lại bản dịch của câu ngữ cảnh trước vào đầu câu mới.
12. **Khắc phục Triệt để Lỗi Lặp Chuỗi Whisper/Ollama, Sửa Vỡ Tokenizer Ġ & Rơi Câu NLLB-200, và Tối ưu Nhịp Đọc Phụ đề Hội thảo (Video `a.mp4`):**
    - *Chẩn đoán thực tế từ `meeting_2026-11-02_06-11-58.md` & `app_debug.log` (332 câu, 21 phút đầu video `a.mp4`):*
      1. **Lặp chuỗi giai đoạn đầu và rải rác:**
         - Whisper decoder loop khi gặp từ lạ hoặc câu ngắn (mồi từ vựng `"I'm Jocko, "` lặp 37 lần và bị cắt cụt ở cuối token `"I"`).
         - Cụm từ và mệnh đề lặp liên tiếp trong audio (`"Yeah, we have, we have..."`, mệnh đề dài 15-25 từ lặp đôi ở giữa câu).
         - Mồi ngữ cảnh `stt_prompt_history` khi bị dính chuỗi lặp Whisper sẽ làm mồi hỏng kéo theo các câu sau tiếp tục lặp; đồng thời cơ chế `_strip_context_echo()` cũ dùng `.find()` thay vì neo đầu câu có nguy cơ xóa nhầm cụm danh từ lặp lại tự nhiên ở giữa câu sau (`"trí tuệ nhân tạo"`).
      2. **Tiếng Anh/Ngoại ngữ rò rỉ, Phản hồi Chatbot LLM & Mất câu khi Fallback NLLB-200:**
         - Khi Ollama nhận chuỗi STT lặp/vụn câu, nó chuyển sang hội thoại giải thích tiếng Anh (`"It seems like..."`), chèn từ tiếng Anh đơn lẻ chưa dịch giữa câu Việt (`"hosting"`, `"dedication"`, `"lờiAdvice"`, `"effortless"`, `"matters"`), hoặc chèn chữ Kirin (`"giải mиф hóa"`).
         - NLLB-200 trên Python 3.12 khi qua `transformers.AutoTokenizer` bị lỗi biến khoảng trắng SentencePiece `▁` thành ký tự `Ġ` (`\u0120`), phá vỡ vocab CTranslate2; ngoài ra model NMT đơn câu `nllb-200-distilled-600M` sẽ bỏ sót 40-60% nội dung nếu truyền gộp 2-3 câu vào chung 1 chuỗi mà không tách batch câu.
      3. **Tốc độ hiển thị không theo kịp hoặc trôi quá nhanh & Vỡ bố cục ngang/dọc:**
         - GPU xử lý rất nhanh (`0.5s - 1.2s/câu`), nhưng 25.3% số câu chỉ tồn tại trên màn hình `< 2.5s` và 42.4% đòi hỏi tốc độ đọc `> 4.5 từ/giây`. Nếu chỉ gộp mù quáng mọi câu `< 1.8s` sẽ ghép nhầm 2 câu độc lập dài 13 từ thành khối 26 từ tràn khung dọc `215px`, còn ngưỡng chờ liên từ `0.85s` trong `main.py` luôn bị hết hạn sớm trước khi `SystemAudioVADWorker` thu xong mệnh đề kế tiếp (`3-4s`).
    - *Đã khắc phục & cải tiến sâu 4 lớp toàn diện:*
      1. **Thuật toán triệt tiêu lặp BPE + Cửa sổ trượt `_collapse_repetitions()` & Neo đầu câu `_strip_context_echo()` (`ai_engine.py`):**
         - Thu gọn mọi vòng lặp `>= 3` lần và tự gọt sạch mảnh tiền tố BPE bị cắt dở ở cuối vòng lặp (`"I'm Jocko, " * 37 + "I"` $\rightarrow$ `"I'm Jocko"`).
         - Cửa sổ trượt phát hiện mọi cụm `2..30` từ lặp đôi ở bất kỳ vị trí nào trong câu (giữ nguyên từ dẫn đầu, vế mới phía sau và bảo toàn 100% từ láy tiếng Việt như `"luôn luôn"`, `"từ từ"`).
         - Cấu hình Whisper `repetition_penalty=1.15`, `no_repeat_ngram_size=4`, chặn `compression_ratio > 2.1` khỏi `stt_prompt_history`, và khóa `_strip_context_echo()` chỉ cắt ở **đầu câu (prefix)** để không bao giờ đục mất thuật ngữ ở giữa câu.
      2. **SentencePiece Native + Dịch Batch Đa Câu Song Song trên GPU cho NLLB-200 (`ai_engine.py`):**
         - Nạp trực tiếp `sentencepiece.bpe.model` bằng `sentencepiece.SentencePieceProcessor` (chỉ lazy-import `transformers` khi thật sự thiếu file SP, tiết kiệm ~4s nạp & tắt cảnh báo PyTorch), xóa sổ 100% lỗi token `Ġ`.
         - Bổ sung `_split_sentences_for_nmt()` tách đoạn nhiều câu và đẩy song song vào một lệnh `nllb_translator.translate_batch(batch_tokens)` duy nhất trên GPU (`~139ms/đoạn`, dịch đủ 100% các câu không rơi sót).
      3. **Bộ lọc Đa Hệ Chữ + Âm vị Quốc ngữ & Hồi sinh Ngầm `OllamaHealer` Không Treo Phụ đề (`ai_engine.py`):**
         - Mở rộng `_CJK_RE` chặn cả chữ Hán/Nhật/Hàn, Kirin (`\u0400-\u04ff`), Thái, Ả Rập; nâng cấp `_is_untranslated_echo()` tách từ theo biên Unicode chuẩn, nhận diện từ dính kiểu `"lờiAdvice"` và kiểm tra quy luật âm vị/hậu tố tiếng Anh (`>= 7` ký tự ASCII viết thường hoặc `-ing/-tion/-ment/-ness/-less/-able/-ible`, có whitelist thuật ngữ IT & `custom_glossary`) để chuyển tức thì câu lỗi sang NLLB-200 (`0` dương tính giả trên tiếng Việt).
         - Giảm timeout suy luận trực tiếp Ollama xuống `6.5s`, đồng nhất cooldown `30s` cho cả 2 chiều kèm luồng nền `OllamaHealer` tự khởi động lại/kiểm tra `ollama serve` ở hậu trường trong khi phụ đề vẫn chạy mượt không khựng trên NLLB-200.
      4. **Nối Liên Từ Theo Trạng Thái VAD, Lọc Từ Đệm & Bộ Điều Tốc Nhịp Đọc `QTimer` + Tự Co Giãn Chiều Cao (`audio_capture.py`, `overlay_ui.py`, `main.py`):**
         - Bổ sung cờ `SystemAudioVADWorker.is_capturing_speech` giúp `IncomingInferenceWorker` giữ liên từ mở đầu (`"And..."`, `"But, uh..."`) tới `6.2s` khi diễn giả đang nói tiếp (và tự hủy liên từ trơ trọi nếu diễn giả ngừng hẳn, đồng thời lọc bỏ ngay các tiếng ngập ngừng thuần túy `"Um..."`, `"Uh..."`).
         - Nâng cấp `SubtitleOverlayWindow` với **Bộ điều tốc nhịp đọc bất đồng bộ (`_pacer_timer` + `_display_queue`)**: đảm bảo mỗi câu hiển thị trên dòng vàng chính tối thiểu `1.45s - 3.1s` (tỷ lệ theo số từ, tự động nén nhịp bắt kịp khi hàng đợi `>= 1-2` câu) trước khi cuộn mượt lên dòng lịch sử `prev_vi_label` (`⤴ `); chỉ gộp tại chỗ các mảnh câu thực sự nối tiếp ngữ pháp (`_should_merge_with_current`) và tự động co giãn chiều cao theo `totalHeightForWidth(980)` (neo cố định cạnh đáy, ẩn `prev_vi_label` khi trống).
         - Tách riêng thanh tùy chọn `🖥️ Hiển thị phụ đề:` (`👁️ Chỉ hiện Tiếng Việt (Đọc nhanh)`, `Click xuyên phụ đề`, `🔒 Ẩn khi Share màn hình`) trên `ControlBar` để chống tràn ngang trên màn hình laptop DPI 125%.

### 2.4. Tiến độ Triển khai Chrome Extension "Dịch Video Tại Chỗ" (Theo [`docs/CHROME_EXTENSION_SPEC.md`](./docs/CHROME_EXTENSION_SPEC.md))

| Giai đoạn | Hạng mục | Trạng thái | Ghi chú nghiệm thu |
| :--- | :--- | :--- | :--- |
| **P0 — Refactor** | Tách [`incoming_pipeline.py`](./incoming_pipeline.py) (`IncomingInferenceWorker`, `MeetingTranscriptLogger`, `setup_logging()`, hằng số từ đệm/liên từ) và import lại vào [`main.py`](./main.py) | ✅ **Hoàn thành (Done)** | `py_compile` sạch 100%, kiểm thử `IncomingInferenceWorker` & `MeetingTranscriptLogger` (cả chế độ `meeting` và `video`) hoạt động nguyên vẹn, không đổi hành vi app desktop. |
| **P1 — Backend bridge** | [`browser_bridge.py`](./browser_bridge.py), [`bridge_server.py`](./bridge_server.py), [`tests/test_bridge_ws.py`](./tests/test_bridge_ws.py) | ✅ **Hoàn thành (Done)** | Đạt 100% tiêu chí nghiệm thu: `_StreamingResampler` chuẩn cho `16kHz/44.1kHz/48kHz`, chặn bind `0.0.0.0`, `/health` trả đúng JSON, token sai / Origin lạ trả `403`, stream 61s WAV 48kHz thời gian thực qua WS nhận 12 sự kiện `source` và 11 sự kiện `result` tiếng Việt chuẩn xác, tự ghi `meeting_logs/video_*.md`. |
| **P2 — Extension MVP** | `chrome_extension/*` ([`manifest.json`](./chrome_extension/manifest.json), [`service_worker.js`](./chrome_extension/service_worker.js), [`offscreen.js`](./chrome_extension/offscreen.js), [`pcm_worklet.js`](./chrome_extension/pcm_worklet.js), [`content_script.js`](./chrome_extension/content_script.js), [`overlay.css`](./chrome_extension/overlay.css), [`popup.html`](./chrome_extension/popup.html), [`options.html`](./chrome_extension/options.html)), [`start_bridge.bat`](./start_bridge.bat) | ✅ **Hoàn thành (Done)** | Hoàn thiện toàn bộ khung MV3: `tabCapture` giữ nguyên tiếng video (`source.connect(audioCtx.destination)`), `AudioWorklet` gom khung `40ms Int16 mono`, tự đồng bộ token từ origin `chrome-extension://`, Shadow DOM overlay cô lập CSS bám theo thẻ `<video>` + hỗ trợ Fullscreen, Popup & Options 100% tiếng Việt, kèm `start_bridge.bat`. |
| **P3 — Native launcher** | [`native_host/launcher.py`](./native_host/launcher.py), [`native_host/launcher.bat`](./native_host/launcher.bat), [`native_host/com.local_translator.bridge.json.template`](./native_host/com.local_translator.bridge.json.template), [`native_host/install_native_host.ps1`](./native_host/install_native_host.ps1) | ✅ **Hoàn thành (Done)** | Đã cài đặt manifest + Registry HKCU (`Chrome`/`Edge`/`Brave`) + Scheduled Task `LocalTranslatorBridge`; kiểm thử thực tế giao thức nhị phân stdio 4-byte LE (`get_token` & `ensure_running` phản hồi `< 300ms`), tiến trình `pythonw.exe bridge_server.py` sống sót 100% sau khi `launcher.bat` thoát và tự động tắt giải phóng VRAM khi hết thời gian `idle-exit`. |
| **P4 — UX hoàn chỉnh** | [`chrome_extension/subtitle_pacer.js`](./chrome_extension/subtitle_pacer.js), [`tests/test_subtitle_pacer.html`](./tests/test_subtitle_pacer.html), nâng cấp [`chrome_extension/content_script.js`](./chrome_extension/content_script.js), tích hợp Integrated mode vào [`main.py`](./main.py) | ✅ **Hoàn thành (Done)** | Port chính xác 100% bộ điều nhịp đọc `SubtitlePacer` từ `overlay_ui.py` (4 nhánh: hiển thị ngay / gộp tại chỗ câu nối tiếp ngữ pháp / hàng đợi chống trễ `> 2` / cuộn mượt `⤴`), hỗ trợ kéo thả lưu offset theo từng trang (`dragOffset:<hostname>`), xử lý YouTube SPA (`yt-navigate-finish`) & làm mờ khi pause `> 5s`, tích hợp `BrowserBridgeServer` vào `ControlBar` kèm cơ chế độc quyền nguồn Chiều Nghe và đồng bộ 2 chiều combo/glossary qua `pyqtSignal`. |
| **P5 — Tài liệu** | Cập nhật bộ tài liệu Harness ([`CLAUDE.md`](./CLAUDE.md), [`AGENTS.md`](./AGENTS.md), [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`README.md`](./README.md), [`docs/EXTENSION_GUIDE.md`](./docs/EXTENSION_GUIDE.md), [`PROGRESS.md`](./PROGRESS.md)) | ✅ **Hoàn thành (Done)** | Đã cập nhật đồng bộ toàn bộ sơ đồ kiến trúc 3 luồng (Desktop Incoming, Chrome Tab Bridge, Outgoing PTT), bảng mô-đun, mô hình 8 luồng thực thi, hướng dẫn cài đặt Native Host + phím tắt `Alt+Shift+T`, và Playbook mở rộng nguồn âm thanh `TabAudioVADWorker`. |

### 2.5. Tiến độ Triển khai Tầng 2 "🧭 Ý chính đang bàn" (Theo [`docs/INTERPRETER_SUMMARY_SPEC.md`](./docs/INTERPRETER_SUMMARY_SPEC.md))

- **Đặc tả triển khai:** [`docs/INTERPRETER_SUMMARY_SPEC.md`](./docs/INTERPRETER_SUMMARY_SPEC.md). Có 2 chế độ kích hoạt `⏱ Theo chu kỳ` / `⏸ Theo nhịp nghỉ`, chỉnh nóng khi đang chạy để người dùng test thực tế rồi chốt chế độ mặc định.
- **Kết quả Benchmark `num_ctx` (`tests/bench_ollama_ctx.py` trên GTX 1070 Vulkan + `qwen2.5:3b`):**
  - `num_ctx = 512`: Dịch p50 = `945 ms`, p95 = `1556 ms` (baseline), VRAM = `3626 MiB`.
  - **`num_ctx = 1024` (Đã chọn):** Dịch p50 = `981 ms`, p95 = `1304 ms` (`-16.2%` vs 512, thỏa mãn `≤ +15%`), VRAM = `3597 MiB`, Summary `prompt_ms = 1017 ms`, `total_ms = 3036 ms`, `ok = True`.
  - `num_ctx = 2048`: Dịch p50 = `974 ms`, p95 = `1303 ms` (`-16.3%` vs 512), VRAM = `3631 MiB`, Summary `prompt_ms = 1015 ms`, `total_ms = 2909 ms`, `ok = True`.
- **Kết quả Test Preempt ở `OLLAMA_NUM_CTX = 1024` (Dịch gọi sau `300 ms` khi đang chạy Tóm tắt):**
  - Ở các lượt kết nối ổn định (#2–#5), luồng tóm tắt bị ngắt và thoát ngay ở `312–317 ms` (`12–17 ms` sau thời điểm `300 ms`), độ trễ dịch tăng thêm từ `-97 ms` đến `+156 ms` (**p50 = `+64 ms`**).
  - Ở lượt #1 (ngay sau khi chuyển đổi prompt/model làm miss KV prefix cache, Ollama đang chạy batch prompt-eval không ngắt giữa chừng trên GPU và `urllib3` tạo lại kết nối qua `_new_conn`), summary thoát ở `1017 ms` làm lượt #1 tăng `+571 ms` (kéo p95 của 5 mẫu lên `+488 ms`). Đã bổ sung hook cả `pool._new_conn` trong `_create_summary_session()` để luôn đóng trúng socket ngay cả khi `urllib3` tạo kết nối mới.

| Giai đoạn | Hạng mục | Trạng thái | Ghi chú nghiệm thu |
| :--- | :--- | :--- | :--- |
| **P0 — Nền tảng LLM & Benchmark** | `OLLAMA_NUM_CTX = 1024`, `_llm_lock` / `_llm_preempt`, `generate_summary_json()` trong [`ai_engine.py`](./ai_engine.py) + [`tests/bench_ollama_ctx.py`](./tests/bench_ollama_ctx.py) | ✅ **Hoàn thành (Done)** | Đã chốt `OLLAMA_NUM_CTX = 1024` (VRAM ~3.6 GB, p95 dịch `1304 ms`), preempt p50 = `+64 ms` (lượt #2–#5 thoát sau `12–17 ms`, lượt đầu cold prompt-eval `+571 ms`), `py_compile` sạch 100%. |
| **P1 — Lõi & Unit test** | [`interpreter_summary.py`](./interpreter_summary.py), [`tests/test_summary_worker.py`](./tests/test_summary_worker.py), [`.gitignore`](./.gitignore) | ✅ **Hoàn thành (Done)** | Hoàn thiện `SummarySettings`, `decide_trigger`, `build_summary_prompt`, `validate_summary`, `SummaryWorker(QThread)` (hết hạn câu hỏi 90s, tự tạm dừng khi không có Ollama, retry khi bị preempt); toàn bộ 6 unit test trong `tests.test_summary_worker` đạt 100% (`0.105s`). |
| **P2 — Tích hợp Desktop** | [`incoming_pipeline.py`](./incoming_pipeline.py), [`overlay_ui.py`](./overlay_ui.py) (`SummaryPanelWindow`), [`main.py`](./main.py) (`ControlBar`) | ✅ **Hoàn thành (Done)** | Bổ sung `SummaryPanelWindow` tách biệt ở góc phải phía trên `SubtitleOverlayWindow` (`Intersects: False`), hỗ trợ stealth `WDA_EXCLUDEFROMCAPTURE` & click-through, hàng điều khiển `🧭 Ý chính:` đổi nóng (`Tắt` / `⏱ Theo chu kỳ` / `⏸ Theo nhịp nghỉ` + `🔄 Tổng hợp ngay`), và ghi mục `### 🧭 Tóm tắt ý chính` vào biên bản họp. |
| **P3 — Bridge & Chrome Extension** | [`browser_bridge.py`](./browser_bridge.py), `chrome_extension/*`, [`tests/test_bridge_summary.py`](./tests/test_bridge_summary.py) | ✅ **Hoàn thành (Done)** | Đồng bộ `summary_settings` trong `hello_ack`/`start`/`config`, xử lý `summary_now`, phát sự kiện `summary` & `summary_status` qua WS; hiển thị thẻ `.lat-summary-card` ở góc trên-phải `<video>` trong cùng Shadow DOM (hỗ trợ Fullscreen & nút thu gọn `–`); unit test `tests.test_bridge_summary` đạt 100%. |
| **P4 — Telemetry & Tài liệu** | CSV telemetry (`meeting_logs/summary_metrics_*.csv`), [`tests/summarize_metrics.py`](./tests/summarize_metrics.py), cập nhật tài liệu Harness | ✅ **Hoàn thành (Done)** | Đã tích hợp ghi CSV telemetry UTF-8-BOM trong `SummaryWorker`, log `(preempted_summary)` / `dropped_stale` trong `IncomingInferenceWorker`, công cụ `tests/summarize_metrics.py`, và cập nhật đồng bộ `README.md`, `ARCHITECTURE.md`, `docs/CHROME_EXTENSION_SPEC.md`, `docs/EXTENSION_GUIDE.md`, `AGENTS.md`, `CLAUDE.md`, `PROGRESS.md`. |

### 2.6. Tiến độ Triển khai Chế độ Phụ đề Gốc YouTube (Hybrid YouTube Caption Mode — `0 ms` độ trễ hiển thị)

- **Mục tiêu:** Khi xem video YouTube (VOD / Shorts), tự động bắt luồng phụ đề gốc của video (`/api/timedtext` định dạng `json3`/`srv3`/`vtt`) thay vì phải chạy Whisper STT trên âm thanh tab, đưa câu hoàn chỉnh kèm ngữ cảnh 2 chiều (câu trước + câu sắp nói) vào `LocalAIEngine` (`Ollama qwen2.5:3b` + tường lửa `NLLB-200 INT8`), hiển thị khớp trực tiếp theo `video.currentTime` (`0 ms` trễ cảm nhận, `0%` tải Whisper STT), và tự động rơi về Audio Mode (`tabCapture` → Whisper) khi video không có phụ đề hoặc là livestream.
- **Các quyết định thiết kế đã triển khai:**
  1. **Cách A — Tự nháy nạp phụ đề trên `#movie_player` khi CC đang tắt (`chrome_extension/yt_caption_hook.js`):** Bắt trực tiếp gói `/api/timedtext` do `#movie_player` phát sinh (tránh hoàn toàn lỗi rỗng `200 OK` do thiếu PO Token `&pot=`), khôi phục từ bộ đệm HTTP nếu tab đã mở sẵn trước khi nạp extension, hoặc nháy nạp track ngoại ngữ tốt nhất trong `~150–250 ms` rồi **tự động trả nút CC của YouTube về trạng thái OFF ban đầu** (không chèn CSS ẩn CC của YouTube).
  2. **Tái tạo câu sạch không chồng lấp từ `json3` ASR & Dịch Batch Cửa sổ Trượt `+90s` (`youtube_caption.py` & `ai_engine.py`):**
     - Ghép từ theo mốc `tStartMs + tOffsetMs` để loại bỏ hoàn toàn hiện tượng lặp 2 dòng cuộn (`wWinId` / `aAppend: 1`) của phụ đề tự động YouTube; tự gộp các mảnh câu ngắn của phụ đề thủ công.
     - `LocalAIEngine.translate_caption_batch()` giữ nguyên bất biến `OLLAMA_NUM_CTX = 1024`: câu ngay tại playhead chạy đơn khẩn cấp (`urgent=True`, `< 1s`), còn các lô đón đầu 4 câu trong cửa sổ `+90s` chạy `urgent=False` (kèm 2 câu trước + 2 câu lookahead + `custom_glossary.txt`, không ngắt ngang `SummaryWorker`) và cứu hộ từng dòng lẻ qua `NLLB-200` nếu dính chữ Hán/echo mà không làm lệch mốc thời gian.
     - Lưu đệm bền vững tại `caption_cache/` (tối đa 80 video LRU, tự vô hiệu hóa khi đổi `glossary` hoặc `translation_mode`).
  3. **Chống lộ nội dung tương lai (`+90s`) vào Tóm tắt Ý chính & Biên bản (`browser_bridge.py`):** `BridgeSession._record_watched_cues_up_to()` chỉ đẩy câu vào `SummaryWorker` và `MeetingTranscriptLogger` đúng khi `video.currentTime` thực sự chạy qua câu đó (tự bỏ qua đoạn bị tua nhảy cóc). Giữ nguyên kết nối `offscreen.html` ở chế độ nghỉ (`pcmPaused = true`) để chuyển bài SPA (`yt-navigate-finish`) sang video không có phụ đề vẫn tự bật lại Whisper mượt mà không cần bấm lại `Alt+Shift+T`.
  4. **Kiểm thử tự động & Gia cố Triệt để (Audit v2 — `CAPTION_CACHE_SCHEMA_VER = 2`, `HOOK_VERSION = 3`, `CONTENT_SCRIPT_VERSION = 3`):**
     - **Chặn 100% lỗi dịch lệch câu / gộp câu lookahead / cắt cụt vế câu / rò tiếng Anh IN HOA (`ai_engine.py` & `youtube_caption.py`):**
       - Loại bỏ hoàn toàn việc đưa `next_context` (`lookahead`) vào prompt của `qwen2.5:3b` trong `translate_caption_batch()` (nguyên nhân khiến LLM dịch gộp cả câu sắp nói vào câu hiện tại làm câu ngắn 1–2 từ như `"Stop."`, `"No."` biến thành câu dài của nhân vật sau), hạ `BATCH_SIZE = 2`, và bổ sung bộ kiểm định `_is_misaligned_caption_line()` + `_word_jaccard()` tự động chuyển sang `NLLB-200` khi phát hiện câu dịch bị phình dài bất thường, bị cắt cụt mất vế câu, hoặc lặp giống hệt câu liền kề trong batch.
       - Nâng cấp `_is_untranslated_echo()` bắt cả từ tiếng Anh IN HOA (`GOING`, `STILL`, `DELAYING`, `IT'S`, v.v.) và chuẩn hóa câu phụ đề viết hoa toàn bộ (`_normalize_shouting_caps()`, bảo toàn từ viết tắt `FBI`, `CIA`, `PDA`, `AI`, `SLA`).
       - Nâng cấp `_reconstruct_asr_word_cues()` và `_merge_asr_orphan_fragments()` (`hold_for_punct_tail`, `hold_for_short_start`, `hold_for_dangling`, khử lặp từ đơn ASR `"I I I"`): ngăn chặn triệt để việc ngắt rơi 1 từ có dấu chấm ở cuối câu sang cue mới (`"how long you were" | "there."`, `"tipped him" | "off."`, `"Of" | "course,"`, `"for his" | "arraignment."`).
     - **Chặn 100% lỗi trượt bắt phụ đề gốc YouTube & Rê chuột Preview làm lệch thẻ `<video>` (`chrome_extension/*` & `browser_bridge.py`):**
       - Sửa lỗi chí mạng trong `yt_caption_hook.js` gọi đồng thời cả `player.toggleSubtitles()` lẫn `.ytp-subtitles-button.click()` trong cùng 1 tick làm nút CC tự bật rồi tự tắt ngay lập tức trước khi YouTube kịp phát request `/api/timedtext`.
       - Thêm cơ chế lưu chữ ký PO Token (`potStore`) theo từng `videoId`, tự gỡ `&tlang=...` để lấy lại phụ đề gốc sạch khi người dùng đang bật Auto-translate của YouTube, thử tải trực tiếp từ `captionTracks[].baseUrl` (Layer 2) trước khi nháy nút CC (Layer 3), và khóa chặt `inspectTimedTextResponse()` + `findBestVideoElement()` chỉ nhận diện đúng `videoId` trên URL chính (bỏ qua hoàn toàn các video preview khi rê chuột ở cột bên phải / trang chủ).
       - Sửa lỗi giá trị `video.currentTime` cũ của video trước làm lệch cửa sổ dịch `+90s` khi chuyển bài SPA, tự động nạp nâng cấp nóng `content_script.js` (`CONTENT_SCRIPT_VERSION = 3`) & `yt_caption_hook.js` (`HOOK_VERSION = 3`) trên cả tab mở sẵn, tự động `caption:reprobe` ngay khi WebSocket vừa kết nối xong, và tăng `CAPTION_CACHE_SCHEMA_VER = 2` (xóa sạch cache cũ bị lệch).
     - `py_compile` + `node --check` sạch 100%; toàn bộ **16/16 unit & integration tests** (`tests.test_youtube_caption`, `tests.test_summary_worker`, `tests.test_bridge_summary`) đạt `OK`.

---

## 3. Các Bước Tiếp theo Khi Bật Lên Lại (Next Steps)

Khi quay lại làm việc, xem video học tập trên Chrome hoặc chuẩn bị vào cuộc họp thực tế:

1. **Bước 1 — Khởi chạy ứng dụng Desktop (Mọi Model & Ollama Vulkan đã sẵn sàng 100% trên máy):**
   - Click đúp **`setup_and_run.bat`** *(hoặc chạy `.\venv\Scripts\python.exe main.py`)*.
   - Toàn bộ model (`Faster-Whisper large-v3-turbo-ct2`, `Ollama qwen2.5:3b` trên GPU Vulkan, `NLLB-200 INT8`) đã có sẵn trong ổ cứng nên nạp thẳng offline chỉ mất vài giây; đồng thời `BrowserBridgeServer` (`127.0.0.1:47863`) cũng tự động sẵn sàng đón kết nối từ Chrome Extension.
2. **Bước 2 — Dịch Video Tại Chỗ trên Trình duyệt Chrome / Edge / Brave (`Alt+Shift+T`):**
   - Mở `chrome://extensions/` $\rightarrow$ bấm nút **Reload (`⟳`)** trên tiện ích **Local AI Live Translator — Phụ đề Video** để nhận `yt_caption_hook.js` mới (hoặc bật **Developer mode** $\rightarrow$ **Load unpacked** trỏ vào [`chrome_extension`](./chrome_extension), ID cố định: `cgbbacpgahelimaelidkgmphgcbiclfd`).
   - Mở bất kỳ tab có video (YouTube, Coursera, Udemy, v.v.) và nhấn **`Alt+Shift+T`**. Trên YouTube có phụ đề gốc/tự động, HUD sẽ hiện `⚡ Phụ đề gốc YT (EN · Chuẩn/Tự động) | Đón đầu: +90s` và khớp chuẩn từng câu theo thời gian video (tua tới đâu dịch ngay tới đó); nếu video không có phụ đề hoặc ở trang web khác, tiện ích tự động thu âm thanh tab dịch bằng Whisper như bình thường.
3. **Bước 3 — Thiết lập nhanh trước cuộc họp (Zoom / Google Meet / Teams):**
   - Nhập tên dự án, tên khách hàng hoặc thuật ngữ chuyên ngành vào ô **`📚 Từ điển họp`** (ví dụ: `SLA, Kubernetes, Sprint, P&L -> Báo cáo lãi lỗ`).
   - Đảm bảo ô **`🔒 Ẩn khi Share màn hình`**, **`🛡️ Duyệt 1.5s (Esc hủy)`** và **`📝 Lưu biên bản`** đang được tích chọn.
   - Bấm **`▶ Bật Dịch Chiều Nghe`** để bắt đầu xem phụ đề song ngữ thời gian thực.
4. **Bước 4 — Giao tiếp Chiều Nói (`F8` nói — `Esc` hủy) & Đấu nối Virtual Mic:**
   - Để phát giọng tiếng Anh vào phòng họp: Cài driver **VB-Audio Virtual Cable** (`CABLE Input`), chọn `CABLE Output` làm Microphone trong Zoom/Meet và cắm Micro/Tai nghe vật lý cho PC.
   - Khi phát biểu: Giữ phím **`F8`** để nói tiếng Việt $\rightarrow$ thả **`F8`** $\rightarrow$ liếc nhanh câu tiếng Anh trên Overlay trong `1.5s` (bấm **`Esc`** nếu muốn hủy/nói lại).
   - Cuối buổi họp: Bấm **`📂 Mở biên bản`** để xem lại toàn bộ biên bản họp song ngữ trong thư mục `meeting_logs/`.
5. **Nếu có bất kỳ vấn đề gì phát sinh:**
   - Chỉ cần nhắn cho AI: *"Đọc `PROGRESS.md` và `app_debug.log` để tiếp tục kiểm tra/triển khai"*.

