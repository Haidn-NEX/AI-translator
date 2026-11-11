import datetime
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
from PyQt6.QtCore import QThread, pyqtSignal
from ai_engine import LocalAIEngine

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "app_debug.log")
GLOSSARY_FILE = os.path.join(APP_DIR, "custom_glossary.txt")
MEETING_LOGS_DIR = os.path.join(APP_DIR, "meeting_logs")

_LOGGING_INITIALIZED = False


def setup_logging() -> logging.Logger:
    """Thiết lập UTF-8 console và ghi log ra cả app_debug.log lẫn console (idempotent)."""
    global _LOGGING_INITIALIZED
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not _LOGGING_INITIALIZED:
        root_logger = logging.getLogger()
        if not root_logger.handlers:
            handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
            if sys.stdout is not None:
                handlers.append(logging.StreamHandler(sys.stdout))
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
                handlers=handlers,
            )
        _LOGGING_INITIALIZED = True
    return logging.getLogger("LiveTranslator")


logger = setup_logging()

# Từ đệm ngập ngừng vô nghĩa (không dịch thành "Em..." / "Ừm..." gây rác phụ đề)
_PURE_FILLERS = {"um", "uh", "erm", "er", "hmm", "hm", "uhh", "umm"}
# Liên từ nối câu cần chờ vế sau thay vì dịch trơ trọi một chữ "Và..." / "Nhưng..."
_BARE_CONJUNCTIONS = {"but", "and", "so", "or", "because", "while"}
_DANGLING_CONNECTORS = _BARE_CONJUNCTIONS | {
    "well", "and uh", "so uh", "but uh", "and um", "so um", "but um",
    "or uh", "or um", "because uh", "because um",
}


class IncomingInferenceWorker(QThread):
    result_ready = pyqtSignal(str, str, float, float)
    source_ready = pyqtSignal(str)
    status_update = pyqtSignal(str)
    mode_synced = pyqtSignal(str)

    def __init__(self, sentence_queue: queue.Queue, engine: LocalAIEngine, is_vad_speaking_fn=None):
        super().__init__()
        self.sentence_queue = sentence_queue
        self.engine = engine
        self.is_vad_speaking_fn = is_vad_speaking_fn
        self.running = True
        self.is_loading_models = True
        self.is_processing = False
        self.dropped_stale_count = 0

    def is_idle(self) -> bool:
        return (not self.is_processing) and self.sentence_queue.empty() and (not self.is_loading_models)

    @staticmethod
    def _is_pure_filler(text: str, source_lang: str) -> bool:
        if source_lang not in ("en", "auto"):
            return False
        tokens = re.findall(r"[a-zA-Z']+", text.lower())
        return bool(tokens) and len(tokens) <= 2 and all(t in _PURE_FILLERS for t in tokens)

    @staticmethod
    def _is_dangling_fragment(text: str, source_lang: str) -> bool:
        if source_lang not in ("en", "auto"):
            return False
        stripped = text.strip()
        if stripped.endswith("?"):
            return False
        words = stripped.split()
        if len(words) > 2:
            return False
        clean = " ".join(re.findall(r"[a-zA-Z']+", stripped.lower()))
        return clean in _DANGLING_CONNECTORS or (len(words) <= 2 and stripped.endswith("..."))

    @staticmethod
    def _stitch_prefix(prefix: str, current: str) -> str:
        p = re.sub(r"[\s,.…]+$", "", prefix.strip())
        p_parts = p.split()
        if len(p_parts) >= 2 and re.sub(r"[^\w]", "", p_parts[-1].lower()) in _PURE_FILLERS:
            p = " ".join(p_parts[:-1]).rstrip(" ,.")
        cur = current.lstrip()
        if cur.startswith("..."):
            cur = cur[3:].lstrip()
        return f"{p} {cur}".strip()

    def run(self):
        self.is_loading_models = True
        self.status_update.emit(
            f"⏳ Đang tải/nạp model STT ({self.engine.stt_model_name})... (Lần đầu tải ~1.5GB mất 1-2 phút)"
        )
        try:
            self.engine.load_stt_model()
            if self.engine.translation_mode == "ollama":
                self.status_update.emit(f"⏳ Đang nạp sẵn model dịch Ollama ({self.engine.ollama_model}) lên VRAM...")
                self.engine.warmup_ollama()
            # Nạp sẵn luôn NLLB-200 INT8 (~600MB) để làm chốt chặn 0ms trễ khi cần lọc chữ Hán hoặc dự phòng offline
            self.status_update.emit("⏳ Đang nạp bộ lọc từ vựng NLLB-200 INT8 (~600MB)...")
            self.engine._ensure_nllb_loaded("eng_Latn")
            self.engine._ensure_nllb_loaded("vie_Latn")
            self.mode_synced.emit(self.engine.translation_mode)
            self.is_loading_models = False
            active_trans = (
                f"Ollama {self.engine.ollama_model}"
                if self.engine.translation_mode == "ollama"
                else "NLLB-200 INT8"
            )
            self.status_update.emit(f"✅ Sẵn sàng 2 Chiều ({active_trans})! Đang lắng nghe âm thanh hệ thống & phím [F8]...")
        except Exception as e:
            self.is_loading_models = False
            logger.exception("Lỗi nạp model AI")
            self.status_update.emit(f"❌ Lỗi nạp model: {e}")
            return

        pending_prefix = ""
        pending_stt_ms = 0.0
        pending_since = 0.0

        while self.running:
            try:
                audio_np = self.sentence_queue.get(timeout=0.2)
            except queue.Empty:
                if pending_prefix:
                    vad_active = bool(self.is_vad_speaking_fn and self.is_vad_speaking_fn())
                    wait_limit = 6.2 if vad_active else 1.5
                    if (time.perf_counter() - pending_since) >= wait_limit:
                        clean_prefix = " ".join(re.findall(r"[a-zA-Z']+", pending_prefix.lower()))
                        # Nếu người nói đã ngừng hẳn mà mảnh chờ chỉ là liên từ trơ trọi ("And...", "But..."),
                        # hủy mảnh cụt thay vì dịch chữ "Và..." đứng một mình gây rác màn hình
                        if clean_prefix and all(
                            t in (_BARE_CONJUNCTIONS | _PURE_FILLERS) for t in clean_prefix.split()
                        ):
                            logger.info(f"[Chiều Nghe] Bỏ qua liên từ bỏ lửng quá hạn: {pending_prefix}")
                            self.status_update.emit("✅ Đang lắng nghe âm thanh hệ thống...")
                            pending_prefix = ""
                            pending_stt_ms = 0.0
                            continue
                        self.is_processing = True
                        try:
                            self.source_ready.emit(pending_prefix)
                            self.engine.last_translate_preempted_summary = False
                            vi_text, trans_ms = self.engine.translate_to_vietnamese(pending_prefix)
                            preempt_tag = (
                                " (preempted_summary)"
                                if getattr(self.engine, "last_translate_preempted_summary", False)
                                else ""
                            )
                            self.mode_synced.emit(self.engine.translation_mode)
                            logger.info(
                                f"[Chiều Nghe] STT ({pending_stt_ms:.0f}ms): {pending_prefix} => "
                                f"VI (trans_ms={trans_ms:.0f}ms){preempt_tag}: {vi_text}"
                            )
                            self.result_ready.emit(pending_prefix, vi_text, pending_stt_ms, trans_ms)
                        except Exception:
                            logger.exception("Lỗi dịch đoạn chờ")
                        finally:
                            self.is_processing = False
                            pending_prefix = ""
                            pending_stt_ms = 0.0
                continue

            dropped_in_turn = 0
            while self.sentence_queue.qsize() > 2:
                try:
                    audio_np = self.sentence_queue.get_nowait()
                    dropped_in_turn += 1
                except queue.Empty:
                    break
            if dropped_in_turn > 0:
                self.dropped_stale_count += dropped_in_turn
                pending_prefix = ""
                pending_stt_ms = 0.0
                logger.info(
                    f"[Chiều Nghe] Bỏ đoạn cũ do tồn hàng đợi (dropped_stale={dropped_in_turn}, total={self.dropped_stale_count})"
                )

            self.is_processing = True
            try:
                dur = len(audio_np) / 16000.0
                self.status_update.emit(f"⚡ Đang nhận diện & dịch câu vừa nghe ({dur:.1f}s)...")
                src_text, stt_ms = self.engine.transcribe(audio_np)
                if len(src_text.strip()) < 2:
                    self.status_update.emit("✅ Đang lắng nghe âm thanh hệ thống... (Đã lọc bỏ tạp âm/khoảng lặng)")
                    continue
                if self._is_pure_filler(src_text, self.engine.source_lang):
                    self.status_update.emit("✅ Đang lắng nghe âm thanh hệ thống... (Đã lọc bỏ từ đệm ngập ngừng)")
                    continue

                if pending_prefix:
                    src_text = self._stitch_prefix(pending_prefix, src_text)
                    stt_ms += pending_stt_ms
                    pending_prefix = ""
                    pending_stt_ms = 0.0
                elif self._is_dangling_fragment(src_text, self.engine.source_lang):
                    pending_prefix = src_text.strip()
                    pending_stt_ms = stt_ms
                    pending_since = time.perf_counter()
                    self.status_update.emit(f"⏳ Đang nối vế mở đầu (\"{pending_prefix}\") với câu tiếp theo...")
                    continue

                self.source_ready.emit(src_text)
                self.engine.last_translate_preempted_summary = False
                vi_text, trans_ms = self.engine.translate_to_vietnamese(src_text)
                preempt_tag = (
                    " (preempted_summary)"
                    if getattr(self.engine, "last_translate_preempted_summary", False)
                    else ""
                )
                self.mode_synced.emit(self.engine.translation_mode)
                logger.info(
                    f"[Chiều Nghe] STT ({stt_ms:.0f}ms): {src_text} => VI (trans_ms={trans_ms:.0f}ms){preempt_tag}: {vi_text}"
                )
                self.result_ready.emit(src_text, vi_text, stt_ms, trans_ms)
            except Exception as e:
                logger.exception("Lỗi suy luận chiều nghe")
                self.status_update.emit(f"⚠️ Lỗi xử lý chiều nghe: {e}")
            finally:
                self.is_processing = False

    def stop(self):
        self.running = False


class MeetingTranscriptLogger:
    """
    Tự động lưu biên bản cuộc họp / video song ngữ theo thời gian thực (meeting_logs/<prefix>_YYYY-MM-DD_HH-MM-SS.md)
    để người dùng rà soát lại mọi con số, cam kết và nội dung sau buổi họp / video.
    """
    def __init__(
        self,
        logs_dir: str = MEETING_LOGS_DIR,
        file_prefix: str = "meeting",
        title: str | None = None,
        url: str | None = None,
    ):
        self.logs_dir = logs_dir
        self.file_prefix = file_prefix
        self.title = title
        self.url = url
        self.enabled = True
        self._lock = threading.Lock()
        self._session_file: str | None = None

    def _ensure_file(self) -> str:
        if self._session_file is None:
            os.makedirs(self.logs_dir, exist_ok=True)
            now = datetime.datetime.now()
            ts = now.strftime("%Y-%m-%d_%H-%M-%S")
            self._session_file = os.path.join(self.logs_dir, f"{self.file_prefix}_{ts}.md")
            if self.file_prefix == "meeting" and not self.title and not self.url:
                header = (
                    f"# Biên bản Họp Song ngữ — {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"> Tự động ghi bởi **Local AI 2-Way Live Translator** (100% Local STT/LLM).\n\n---\n\n"
                )
            else:
                doc_title = self.title or "Video / Trang Web"
                url_line = f"- **URL:** {self.url}\n" if self.url else ""
                header = (
                    f"# Bản dịch Video Song ngữ — {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"- **Tiêu đề:** {doc_title}\n"
                    f"{url_line}\n"
                    f"> Tự động ghi bởi **Local AI 2-Way Live Translator (Chrome Extension)**.\n\n---\n\n"
                )
            with open(self._session_file, "w", encoding="utf-8") as f:
                f.write(header)
        return self._session_file

    def log_incoming(self, src_text: str, vi_text: str, lang: str = "en"):
        if not self.enabled or not src_text:
            return
        with self._lock:
            try:
                path = self._ensure_file()
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                entry = (
                    f"- **[{now_str}] 👥 Đối tác (`{lang}`):** {src_text}  \n"
                    f"  ➔ **VI:** {vi_text}\n\n"
                )
                with open(path, "a", encoding="utf-8") as f:
                    f.write(entry)
            except Exception as e:
                logger.warning(f"Không thể ghi biên bản chiều nghe: {e}")

    def log_outgoing(self, vi_said: str, en_spoken: str):
        if not self.enabled or not vi_said:
            return
        with self._lock:
            try:
                path = self._ensure_file()
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                entry = (
                    f"- **[{now_str}] 🎙️ Bạn phát biểu (`vi`):** {vi_said}  \n"
                    f"  ➔ **🔊 Phát sang Meet (`en`):** {en_spoken}\n\n"
                )
                with open(path, "a", encoding="utf-8") as f:
                    f.write(entry)
            except Exception as e:
                logger.warning(f"Không thể ghi biên bản chiều nói: {e}")

    def log_summary(self, payload: dict):
        if not self.enabled or not isinstance(payload, dict):
            return
        topic = str(payload.get("topic") or "").strip()
        points = list(payload.get("points") or [])
        new_count = int(payload.get("new_count", len(points)) or 0)
        new_points = [str(p).strip() for p in points[: max(0, new_count)] if str(p).strip()]
        question = str(payload.get("question") or "").strip()
        if not topic and not new_points and not question:
            return
        trigger = str(payload.get("trigger") or "interval")
        latency_sec = float(payload.get("latency_ms", 0) or 0) / 1000.0
        with self._lock:
            try:
                path = self._ensure_file()
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                lines = [
                    f"> **[{now_str}] 🧭 Ý chính** (`{trigger}`, {latency_sec:.1f}s) — 📌 {topic or '(chưa có)'}"
                ]
                for pt in new_points:
                    lines.append(f"> • {pt}")
                if question:
                    lines.append(f"> ❓ {question}")
                entry = "\n".join(lines) + "\n\n"
                with open(path, "a", encoding="utf-8") as f:
                    f.write(entry)
            except Exception as e:
                logger.warning(f"Không thể ghi ý chính vào biên bản: {e}")

    def open_in_explorer(self):
        os.makedirs(self.logs_dir, exist_ok=True)
        target = self._session_file if (self._session_file and os.path.isfile(self._session_file)) else self.logs_dir
        try:
            if platform.system() == "Windows":
                os.startfile(target)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as e:
            logger.warning(f"Không thể mở thư mục biên bản họp: {e}")

