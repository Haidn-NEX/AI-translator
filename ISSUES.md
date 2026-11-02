# ISSUES & FEATURE REQUESTS

## Issue #1: Bổ sung Bối cảnh & Chỉ dẫn (Meeting Context / System Instruction) cho Model Local

### 1. Đặt vấn đề & Nhu cầu người dùng
- **Nhu cầu:** Người dùng muốn cung cấp một dạng nội dung chỉ dẫn (instruction / context) cho model local (Ollama) trước hoặc trong phiên họp để model nhận diện bối cảnh và tự điều chỉnh (adjust) cách dịch thuật:
  - Chọn đúng đại từ xưng hô theo vai vế (ví dụ: diễn giả chính, đồng nghiệp, khách hàng, MC).
  - Định hình văn phong dịch thuật (trang trọng, hội thảo kỹ thuật chuyên sâu, giao tiếp thân mật, v.v.).
  - Nắm bắt chủ đề cốt lõi của cuộc họp (ví dụ: điện toán đám mây, báo cáo tài chính P&L, thiết kế hệ thống, v.v.).
  - Hạn chế tối đa việc dịch thô ráp, dịch sai nghĩa các từ đa nghĩa (homonyms) hoặc rò rỉ cấu trúc tiếng Anh.

---

### 2. Phân tích Kỹ thuật & Ràng buộc Hệ thống

1. **Phạm vi tác động giữa các tầng model:**
   - **Ollama LLM (`qwen2.5:3b` / `qwen2.5:7b`):** Chịu tác động trực tiếp từ prompt. Model có thể tuân thủ chỉ dẫn nếu prompt được cấu trúc rõ ràng và cô đọng.
   - **NLLB-200 INT8:** Là mô hình NMT truyền thống (Sequence-to-Sequence), không có cơ chế nhận system instruction. Khi hệ thống rơi vào chế độ fallback NLLB-200 (khi Ollama lỗi hoặc cooldown), các instruction này sẽ không được áp dụng.
   - **Faster-Whisper / MLX-Whisper:** Là mô hình nhận dạng giọng nói (STT), không hiểu instruction ngữ nghĩa nhưng có thể tận dụng các từ khóa/tên riêng trích xuất từ instruction để đưa vào `initial_prompt` nhằm tăng độ chính xác nhận diện âm thanh.

2. **Ràng buộc cửa sổ ngữ cảnh (`num_ctx`) & VRAM (Quy tắc Bất biến số 2 trong `CLAUDE.md`):**
   - Trên GPU GTX 1070 8GB Pascal, hệ thống hiện giữ cố định `num_ctx = 512` để đảm bảo VRAM an toàn (~1.9GB cho Ollama) và tránh việc Ollama nạp lại runner gây trễ ~5.8s.
   - System message hiện tại cùng với 2 câu ngữ cảnh và câu gốc chiếm khoảng 220–280 tokens.
   - Nếu đưa vào instruction quá dài, Ollama sẽ vượt ngưỡng 512 tokens và tự động cắt xén (truncate) phần đầu của system prompt, làm vô hiệu hóa các luật dịch cốt lõi (chặn chữ Hán, loại bỏ từ đệm, định dạng phụ đề).
   - Trên Apple Silicon M1 Pro 32GB RAM (hoặc GPU hiện đại $\ge 12\text{GB}$ VRAM), `num_ctx` có thể nâng lên 1024–2048 tokens một cách an toàn mà không lo nghẽn bộ nhớ.

3. **Năng lực tuân thủ chỉ dẫn của Model nhỏ:**
   - Model `qwen2.5:3b` (GTX 1070) có năng lực hạn chế đối với các chỉ dẫn trừu tượng (như *"hãy dịch thật hay, tự nhiên"*). Nó chỉ hoạt động tốt với các chỉ dẫn ngắn gọn, định lượng rõ ràng hoặc đi kèm 1–2 ví dụ mẫu (Few-shot).
   - Model `qwen2.5:7b` (Mac M1 Pro) tuân thủ instruction phức tạp và giữ văn phong tốt hơn nhiều.

---

### 3. Đề xuất Phương án Kỹ thuật

#### A. Giao diện Người dùng (UI / UX)
- Mở rộng thanh điều khiển hoặc thêm khu vực nhập cấu hình:
  - Trường nhập: **`📋 Bối cảnh cuộc họp (Meeting Context / Instruction)`** trên `ControlBar` (cho phép nhập 3–5 dòng ngắn gọn, giới hạn tối đa ~400 ký tự).
  - Tự động lưu bền vững vào file `meeting_context.txt` (tương tự như `custom_glossary.txt`).

#### B. Cấu trúc Prompt & Tận dụng KV Cache
- Chèn nội dung bối cảnh vào **System Prompt** của Ollama thay vì User Message:
  - Giữ cố định tiền tố System Prompt để Ollama tận dụng tối đa cơ chế KV Cache (Prompt Caching), giúp tốc độ suy luận mỗi câu không bị chậm lại.
  - Cấu trúc mẫu chuẩn hóa:
    ```text
    [Meeting Context & Instruction]
    Topic: {topic}
    Speakers: {speakers_role}
    Tone: {tone_instruction}
    Key Mappings: {key_terms}
    ```

#### C. Điều chỉnh Cửa sổ Ngữ cảnh (`num_ctx`)
- Thử nghiệm và đánh giá việc nâng `num_ctx` từ `512` lên `768` (trên GTX 1070) hoặc `1024` (trên Mac M1 Pro):
  - Đo đạc thực tế mức tăng VRAM (ước tính tăng thêm ~180MB VRAM trên Pascal).
  - Đảm bảo giữ nguyên giá trị hằng số trong suốt phiên chạy để không kích hoạt nạp lại runner của Ollama.

#### D. Tích hợp Hai Chiều với Whisper STT
- Viết bộ parser tự động bóc tách các danh từ riêng, tên người, mã dự án từ ô `Meeting Context` để tự động bổ sung vào `self._glossary_stt_hint` của Whisper.
- Giúp Whisper nghe chuẩn ngay từ đầu, hạn chế việc Whisper nghe sai dẫn đến LLM dịch sai ngữ nghĩa.

---

### 4. Tiêu chí Nghiệm thu (Acceptance Criteria)
1. **Tính ổn định:** Độ trễ dịch thuật của Ollama không tăng quá 15% so với mức hiện tại (~0.6s – 1.0s/câu).
2. **Tuân thủ đại từ & văn phong:** Model chuyển đổi chính xác đại từ nhân xưng và thuật ngữ chỉ định theo bối cảnh đã nhập trong ít nhất 85% các câu thử nghiệm.
3. **Không phá vỡ Invariants:** Không gây tràn VRAM trên GTX 1070 8GB, không làm rơi vào vòng lặp fallback NLLB liên tục, không gây lỗi nạp lại Ollama runner giữa phiên họp.
