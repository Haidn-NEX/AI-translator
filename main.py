import datetime
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import queue
from pynput import keyboard
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QCheckBox, QLabel, QLineEdit
)
from hardware_profile import detect_hardware_profile
from audio_capture import SystemAudioVADWorker, MicPushToTalkRecorder
from ai_engine import LocalAIEngine
from tts_virtual_mic import VirtualMicTTSRouter
from overlay_ui import SubtitleOverlayWindow, apply_window_capture_stealth

# Đảm bảo console Windows hỗ trợ in tiếng Việt UTF-8 không bị lỗi charmap cp1252
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Thiết lập ghi log ra cả file app_debug.log và console để dễ dàng kiểm tra lỗi
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "app_debug.log")
GLOSSARY_FILE = os.path.join(APP_DIR, "custom_glossary.txt")
MEETING_LOGS_DIR = os.path.join(APP_DIR, "meeting_logs")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("LiveTranslator")


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
                        try:
                            self.source_ready.emit(pending_prefix)
                            vi_text, trans_ms = self.engine.translate_to_vietnamese(pending_prefix)
                            self.mode_synced.emit(self.engine.translation_mode)
                            logger.info(
                                f"[Chiều Nghe] STT ({pending_stt_ms:.0f}ms): {pending_prefix} => VI ({trans_ms:.0f}ms): {vi_text}"
                            )
                            self.result_ready.emit(pending_prefix, vi_text, pending_stt_ms, trans_ms)
                        except Exception:
                            logger.exception("Lỗi dịch đoạn chờ")
                        finally:
                            pending_prefix = ""
                            pending_stt_ms = 0.0
                continue

            dropped_stale = False
            while self.sentence_queue.qsize() > 2:
                try:
                    audio_np = self.sentence_queue.get_nowait()
                    dropped_stale = True
                except queue.Empty:
                    break
            if dropped_stale:
                pending_prefix = ""
                pending_stt_ms = 0.0

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
                vi_text, trans_ms = self.engine.translate_to_vietnamese(src_text)
                self.mode_synced.emit(self.engine.translation_mode)
                logger.info(f"[Chiều Nghe] STT ({stt_ms:.0f}ms): {src_text} => VI ({trans_ms:.0f}ms): {vi_text}")
                self.result_ready.emit(src_text, vi_text, stt_ms, trans_ms)
            except Exception as e:
                logger.exception("Lỗi suy luận chiều nghe")
                self.status_update.emit(f"⚠️ Lỗi xử lý chiều nghe: {e}")

    def stop(self):
        self.running = False


class MeetingTranscriptLogger:
    """
    Tự động lưu biên bản cuộc họp song ngữ theo thời gian thực (meeting_logs/meeting_YYYY-MM-DD_HH-MM-SS.md)
    để người dùng rà soát lại mọi con số, cam kết và nội dung hai chiều sau buổi họp.
    """
    def __init__(self, logs_dir: str = MEETING_LOGS_DIR):
        self.logs_dir = logs_dir
        self.enabled = True
        self._lock = threading.Lock()
        self._session_file: str | None = None

    def _ensure_file(self) -> str:
        if self._session_file is None:
            os.makedirs(self.logs_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._session_file = os.path.join(self.logs_dir, f"meeting_{ts}.md")
            header = (
                f"# Biên bản Họp Song ngữ — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"> Tự động ghi bởi **Local AI 2-Way Live Translator** (100% Local STT/LLM).\n\n---\n\n"
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


class OutgoingVoiceWorker(QThread):
    """Luồng ưu tiên xử lý Chiều Nói: Tiếng Việt (Mic) -> Duyệt nhanh (Esc hủy) -> Tiếng Anh (Virtual Cable)"""
    outgoing_ready = pyqtSignal(str, str, float, float, float)
    status_update = pyqtSignal(str)
    mode_synced = pyqtSignal(str)

    def __init__(self, outgoing_queue: queue.Queue, engine: LocalAIEngine, tts_router: VirtualMicTTSRouter):
        super().__init__()
        self.outgoing_queue = outgoing_queue
        self.engine = engine
        self.tts_router = tts_router
        self.running = True
        self.preview_before_speak = True
        self.preview_delay_sec = 1.5
        self.cancel_event = threading.Event()
        self.is_busy = False

    def cancel_current(self) -> bool:
        """Hủy câu tiếng Anh đang đếm ngược duyệt hoặc dừng ngay âm thanh TTS đang phát sang Meet"""
        if self.is_busy:
            self.cancel_event.set()
            self.tts_router.stop_playback()
            return True
        return False

    def run(self):
        while self.running:
            try:
                audio_np = self.outgoing_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            self.cancel_event.clear()
            self.is_busy = True
            try:
                if self.engine.whisper_model is None and self.engine.profile.stt_backend == "faster_whisper":
                    self.status_update.emit("⏳ Đang nạp model Whisper lần đầu cho chiều nói...")
                # 1. Nhận diện tiếng Việt từ Mic
                vi_said, stt_ms = self.engine.transcribe(audio_np, force_lang="vi")
                if len(vi_said.strip()) < 2:
                    self.status_update.emit("Không nghe rõ câu tiếng Việt, hãy thử nói lại.")
                    continue

                # 2. Dịch sang tiếng Anh hội thoại công việc
                en_text, trans_ms = self.engine.translate_vi_to_english(vi_said)
                self.mode_synced.emit(self.engine.translation_mode)
                if self.cancel_event.is_set():
                    self.status_update.emit(f"🚫 Đã hủy phát câu vừa nói: \"{en_text}\" (Giữ F8 để nói lại)")
                    continue

                # 3. Chế độ duyệt nhanh trước khi phát (1.5s) để tránh rủi ro "nói hớ" trong cuộc họp
                if self.preview_before_speak and self.preview_delay_sec > 0:
                    steps = max(1, int(round(self.preview_delay_sec / 0.1)))
                    cancelled = False
                    for idx in range(steps):
                        if not self.running or self.cancel_event.is_set():
                            cancelled = True
                            break
                        rem = max(0.1, (steps - idx) * 0.1)
                        if idx % 5 == 0:
                            self.status_update.emit(
                                f"⏳ Sắp phát sau {rem:.1f}s (Nhấn [Esc] để HỦY): \"{vi_said}\" ➔ EN: \"{en_text}\""
                            )
                        time.sleep(0.1)
                    if cancelled or self.cancel_event.is_set():
                        logger.info(f"[Chiều Nói - ĐÃ HỦY PHÁT] VI: {vi_said} => EN: {en_text}")
                        self.status_update.emit(f"🚫 Đã hủy phát sang Meet: \"{en_text}\" (Giữ F8 để nói lại)")
                        continue

                self.status_update.emit(f"🔊 Đang phát sang Meet (Nhấn [Esc] để dừng): \"{en_text}\"")
                # 4. Đọc câu tiếng Anh vào Virtual Microphone (VB-Cable / BlackHole)
                tts_ms = self.tts_router.speak_to_virtual_mic(en_text, cancel_event=self.cancel_event)
                if self.cancel_event.is_set():
                    logger.info(f"[Chiều Nói - ĐÃ DỪNG GIỮA CHỪNG] VI: {vi_said} => EN: {en_text}")
                    self.status_update.emit(f"🚫 Đã dừng phát giữa chừng: \"{en_text}\"")
                    continue

                logger.info(f"[Chiều Nói] VI ({stt_ms:.0f}ms): {vi_said} => EN ({trans_ms:.0f}ms | TTS {tts_ms:.0f}ms): {en_text}")
                self.outgoing_ready.emit(vi_said, en_text, stt_ms, trans_ms, tts_ms)
            except Exception as e:
                logger.exception("Lỗi xử lý chiều nói")
                self.status_update.emit(f"⚠️ Lỗi chiều nói: {e}")
            finally:
                self.is_busy = False

    def stop(self):
        self.running = False
        self.cancel_current()


class ControlBar(QWidget):
    ptt_state_signal = pyqtSignal(str)
    hud_state_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        logger.info("Khởi động ứng dụng Local AI 2-Way Live Translator...")
        self.profile = detect_hardware_profile()
        logger.info(f"Cấu hình phần cứng: {self.profile}")
        self.engine = LocalAIEngine(self.profile)
        self.tts_router = VirtualMicTTSRouter()
        self.transcript_logger = MeetingTranscriptLogger()
        self._hide_from_capture = (platform.system() == "Windows")

        self._saved_glossary = ""
        if os.path.isfile(GLOSSARY_FILE):
            try:
                with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                    self._saved_glossary = f.read().strip()
            except Exception as e:
                logger.warning(f"Không đọc được custom_glossary.txt: {e}")
        if self._saved_glossary:
            self.engine.set_custom_glossary(self._saved_glossary)

        self.incoming_queue = queue.Queue()
        self.outgoing_queue = queue.Queue()

        self.audio_worker = None
        self.incoming_ai_worker = None

        self.overlay = SubtitleOverlayWindow()
        self.overlay.show()

        self.ptt_state_signal.connect(self._on_ptt_status)
        self.hud_state_signal.connect(self._on_hud_status)

        # Khởi chạy sẵn luồng đợi Chiều Nói và bộ thu Push-To-Talk
        self.ptt_recorder = MicPushToTalkRecorder(
            self.outgoing_queue,
            status_callback=lambda msg: self.ptt_state_signal.emit(msg)
        )
        self.outgoing_worker = OutgoingVoiceWorker(self.outgoing_queue, self.engine, self.tts_router)
        self.outgoing_worker.outgoing_ready.connect(self._on_outgoing_completed)
        self.outgoing_worker.status_update.connect(self._on_ptt_status)
        self.outgoing_worker.mode_synced.connect(self._on_worker_mode_synced)
        self.outgoing_worker.start()

        self._positioned_once = False
        self._build_ui()
        self.position_at_top_center()
        self._start_global_hotkey_listener()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._positioned_once:
            self._positioned_once = True
            self.position_at_top_center()
        if self._hide_from_capture:
            apply_window_capture_stealth(self, True)

    def position_at_top_center(self):
        """Đặt bảng điều khiển ở phía trên màn hình để không bao giờ bị khung phụ đề đè lên"""
        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            x = avail.x() + (avail.width() - self.width()) // 2
            y = avail.y() + 30
            self.move(max(0, x), max(0, y))

    def _on_hud_status(self, msg: str):
        if (
            self.incoming_ai_worker
            and getattr(self.incoming_ai_worker, "is_loading_models", False)
            and not msg.startswith(("⏳", "✅", "❌"))
        ):
            msg = f"{msg} | ⏳ Đang tải/nạp model AI (vui lòng đợi)..."
        self.overlay.hud_label.setText(msg)
        if hasattr(self, "panel_status_label"):
            self.panel_status_label.setText(f"Trạng thái: {msg}")

    def _on_ptt_status(self, msg: str):
        self.overlay.outgoing_label.setText(msg)
        if hasattr(self, "panel_status_label"):
            self.panel_status_label.setText(f"Chiều nói: {msg}")

    def _on_incoming_completed(self, src_text: str, vi_text: str, stt_ms: float, trans_ms: float):
        self.overlay.update_incoming_subtitle(
            src_text,
            vi_text,
            stt_ms,
            trans_ms,
            f"{self.profile.stt_compute_type} | {'Ollama' if self.engine.translation_mode == 'ollama' else 'NLLB'}",
        )
        self.transcript_logger.log_incoming(src_text, vi_text, self.engine.source_lang)

    def _on_outgoing_completed(self, vi_said: str, en_text: str, stt_ms: float, trans_ms: float, tts_ms: float):
        self.overlay.update_outgoing_status(vi_said, en_text, stt_ms, trans_ms, tts_ms)
        self.transcript_logger.log_outgoing(vi_said, en_text)

    def _on_ptt_button_pressed(self):
        if not self.ptt_recorder.is_recording and self.outgoing_worker.is_busy:
            self.outgoing_worker.cancel_current()
        self.ptt_recorder.start_recording()

    def _build_ui(self):
        self.setWindowTitle("Local AI 2-Way Live Translator - Control Panel")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.resize(1060, 255)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 8)
        main_layout.setSpacing(5)
        ollama_tag = (
            f"Đã sẵn sàng Ollama ({self.profile.recommended_llm})"
            if self.engine.ollama_available
            else "Đang dùng NLLB-200 INT8 Offline (Chạy setup_and_run.bat nếu muốn cài Ollama)"
        )
        self.info_label = QLabel(
            f"Phần cứng: {self.profile.device_name} | STT: {self.profile.stt_backend} ({self.profile.stt_compute_type}) | {ollama_tag}"
        )
        self.info_label.setStyleSheet("font-weight: bold; color: #0284c7;")
        main_layout.addWidget(self.info_label)

        # Hàng 1: Điều khiển Chiều Nghe (Incoming) — giữ rộng thoáng cho 4 ComboBox trên màn hình 125% DPI
        row1 = QHBoxLayout()
        self.btn_toggle = QPushButton("▶ Bật Dịch Chiều Nghe")
        self.btn_toggle.setStyleSheet("font-weight: bold; padding: 6px 12px;")
        self.btn_toggle.clicked.connect(self.toggle_capture)
        row1.addWidget(self.btn_toggle)

        self.loopback_combo = QComboBox()
        self._populate_loopback_list()
        self.loopback_combo.setMaximumWidth(245)
        self.loopback_combo.view().setMinimumWidth(380)
        self.loopback_combo.currentIndexChanged.connect(self.on_loopback_changed)
        row1.addWidget(self.loopback_combo)

        self.stt_combo = QComboBox()
        rec_short = self.profile.recommended_whisper.split("/")[-1]
        self.stt_combo.addItem(f"Whisper: {rec_short} (★ Chuẩn xác nhất)", self.profile.recommended_whisper)
        if self.profile.recommended_whisper != "small":
            self.stt_combo.addItem("Whisper: small (Nhẹ ~460MB - Kém chuẩn hơn)", "small")
        self.stt_combo.setMaximumWidth(230)
        self.stt_combo.view().setMinimumWidth(360)
        self.stt_combo.currentIndexChanged.connect(self.on_stt_model_changed)
        row1.addWidget(self.stt_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["en (Nghe Tiếng Anh)", "auto (Tự nhận diện)", "ja (Tiếng Nhật)", "zh (Tiếng Trung)", "ko (Tiếng Hàn)"])
        self.lang_combo.setMaximumWidth(160)
        self.lang_combo.currentTextChanged.connect(self.on_lang_changed)
        row1.addWidget(self.lang_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            f"Ollama LLM ({self.profile.recommended_llm})",
            "NLLB-200 INT8 (Siêu nhẹ, không cần Ollama)"
        ])
        self.mode_combo.setCurrentIndex(0 if self.engine.translation_mode == "ollama" else 1)
        self.mode_combo.setMaximumWidth(220)
        self.mode_combo.view().setMinimumWidth(320)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        row1.addWidget(self.mode_combo)
        main_layout.addLayout(row1)

        # Hàng 1b: Tùy chọn hiển thị phụ đề & Chế độ tàng hình khi Share màn hình (tách dòng riêng chống tràn ngang)
        row_display = QHBoxLayout()
        row_display.setSpacing(14)
        display_lbl = QLabel("🖥️ Hiển thị phụ đề:")
        display_lbl.setStyleSheet("color: #475569; font-weight: bold;")
        row_display.addWidget(display_lbl)

        self.chk_compact_vi = QCheckBox("👁️ Chỉ hiện Tiếng Việt (Đọc nhanh)")
        self.chk_compact_vi.setToolTip(
            "Ẩn dòng tiếng Anh gốc, hiển thị 2 dòng Tiếng Việt cuộn để mắt theo dõi hội thảo nhanh dễ dàng"
        )
        self.chk_compact_vi.toggled.connect(lambda checked: self.overlay.set_show_source_subtitle(not checked))
        row_display.addWidget(self.chk_compact_vi)

        self.chk_click = QCheckBox("Click xuyên phụ đề")
        self.chk_click.toggled.connect(self.overlay.set_click_through)
        row_display.addWidget(self.chk_click)

        self.chk_stealth = QCheckBox("🔒 Ẩn khi Share màn hình")
        self.chk_stealth.setToolTip(
            "Ẩn hoàn toàn khung phụ đề và bảng điều khiển khỏi Zoom / Google Meet / Teams khi bạn Share toàn màn hình"
        )
        if platform.system() == "Windows":
            self.chk_stealth.setChecked(True)
        else:
            self.chk_stealth.setEnabled(False)
        self.chk_stealth.toggled.connect(self.on_stealth_toggled)
        row_display.addWidget(self.chk_stealth)
        row_display.addStretch(1)
        main_layout.addLayout(row_display)

        # Hàng 2: Điều khiển Chiều Nói (Outgoing Virtual Mic, Duyệt nhanh 1.5s & Hủy khẩn cấp Esc)
        row2 = QHBoxLayout()
        self.btn_ptt = QPushButton("🎙️ Giữ chuột (hoặc giữ phím F8) để Nói Tiếng Việt")
        self.btn_ptt.setStyleSheet("background-color: #166534; color: white; font-weight: bold; padding: 6px;")
        self.btn_ptt.pressed.connect(self._on_ptt_button_pressed)
        self.btn_ptt.released.connect(self.ptt_recorder.stop_recording)
        row2.addWidget(self.btn_ptt)

        self.chk_preview = QCheckBox("🛡️ Duyệt 1.5s (Esc hủy)")
        self.chk_preview.setChecked(True)
        self.chk_preview.setToolTip(
            "Hiện bản dịch tiếng Anh trước 1.5 giây để bạn kiểm tra nhanh; bấm phím Esc để hủy phát ngay nếu dịch chưa đúng ý"
        )
        self.chk_preview.toggled.connect(self.on_preview_toggled)
        row2.addWidget(self.chk_preview)

        self.btn_cancel_tts = QPushButton("✖ Hủy phát (Esc)")
        self.btn_cancel_tts.setStyleSheet("background-color: #991b1b; color: white; font-weight: bold; padding: 5px 10px;")
        self.btn_cancel_tts.clicked.connect(self.on_cancel_outgoing_clicked)
        row2.addWidget(self.btn_cancel_tts)

        row2.addWidget(QLabel("Micro Ảo:"))
        self.vmic_combo = QComboBox()
        self._populate_virtual_mic_list()
        self.vmic_combo.setMaximumWidth(250)
        self.vmic_combo.view().setMinimumWidth(400)
        self.vmic_combo.currentIndexChanged.connect(self.on_vmic_changed)
        row2.addWidget(self.vmic_combo)

        self.tts_combo = QComboBox()
        self.tts_combo.addItems(["TTS: Neural Voice (Tự nhiên)", "TTS: Offline Native (0ms mạng)"])
        self.tts_combo.currentIndexChanged.connect(self.on_tts_mode_changed)
        row2.addWidget(self.tts_combo)
        main_layout.addLayout(row2)

        # Hàng 3: Từ điển chuyên ngành cuộc họp (Custom Glossary) & Lưu biên bản họp song ngữ
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("📚 Từ điển họp:"))
        self.glossary_input = QLineEdit()
        self.glossary_input.setPlaceholderText(
            "Nhập tên dự án, khách hàng, thuật ngữ chuyên ngành (VD: Kubernetes, SLA, OKR, P&L -> Báo cáo lãi lỗ)..."
        )
        if self._saved_glossary:
            self.glossary_input.setText(self._saved_glossary)
        self.glossary_input.textChanged.connect(self.on_glossary_changed)
        row3.addWidget(self.glossary_input, stretch=1)

        self.chk_transcript = QCheckBox("📝 Lưu biên bản")
        self.chk_transcript.setChecked(True)
        self.chk_transcript.setToolTip(
            "Tự động lưu biên bản họp song ngữ có mốc thời gian vào thư mục meeting_logs/ để đối chiếu sau buổi họp"
        )
        self.chk_transcript.toggled.connect(self.on_transcript_toggled)
        row3.addWidget(self.chk_transcript)

        self.btn_open_transcript = QPushButton("📂 Mở biên bản")
        self.btn_open_transcript.clicked.connect(self.transcript_logger.open_in_explorer)
        row3.addWidget(self.btn_open_transcript)
        main_layout.addLayout(row3)

        # Hàng 4: Thanh trạng thái trực tiếp trên Control Panel
        self.panel_status_label = QLabel(
            "Trạng thái: Sẵn sàng. Đã bật ẩn khi Share màn hình & duyệt 1.5s chiều nói (F8 nói | Esc hủy)."
        )
        self.panel_status_label.setStyleSheet("color: #334155; font-size: 12px; padding-top: 2px;")
        main_layout.addWidget(self.panel_status_label)

    def _populate_loopback_list(self):
        self.loopback_combo.addItem("🎧 Loa thu: Tự động (Loa mặc định Windows)", None)
        for dev_idx, name, is_def in SystemAudioVADWorker.list_loopback_devices():
            tag = " [Mặc định]" if is_def else ""
            clean_name = name.replace(" [Loopback]", "")
            self.loopback_combo.addItem(f"🎧 Loa: {clean_name}{tag}", dev_idx)

    def on_loopback_changed(self, idx: int):
        if self.audio_worker and self.audio_worker.running:
            self.audio_worker.stop()
            self.audio_worker = SystemAudioVADWorker(
                self.incoming_queue,
                status_callback=lambda msg: self.hud_state_signal.emit(msg),
                loopback_device_index=self.loopback_combo.currentData(),
            )
            self.audio_worker.start()

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
        """Cho phép nhấn giữ phím F8 để nói và nhấn phím Esc để hủy phát ngay cả khi đang mở toàn màn hình Zoom / Meet"""
        def on_press(key):
            if key == keyboard.Key.f8:
                if not self.ptt_recorder.is_recording and self.outgoing_worker.is_busy:
                    self.outgoing_worker.cancel_current()
                self.ptt_recorder.start_recording()
            elif key == keyboard.Key.esc:
                if self.outgoing_worker.is_busy:
                    self.outgoing_worker.cancel_current()

        def on_release(key):
            if key == keyboard.Key.f8:
                self.ptt_recorder.stop_recording()

        self.kb_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.kb_listener.daemon = True
        self.kb_listener.start()

    def on_stealth_toggled(self, checked: bool):
        self._hide_from_capture = checked
        ok_overlay = self.overlay.set_hide_from_capture(checked)
        ok_panel = apply_window_capture_stealth(self, checked)
        if checked:
            if ok_overlay and ok_panel:
                self._on_hud_status("🔒 Đã BẬT ẩn phụ đề & bảng điều khiển khi Share màn hình (Zoom/Meet/Teams).")
            else:
                self._on_hud_status("⚠️ Windows hiện tại không hỗ trợ ẩn cửa sổ khỏi trình quay màn hình.")
        else:
            self._on_hud_status("👁️ Đã TẮT ẩn khi Share màn hình (Người xem Share màn hình sẽ thấy phụ đề).")

    def on_preview_toggled(self, checked: bool):
        self.outgoing_worker.preview_before_speak = checked
        if checked:
            self._on_ptt_status("🛡️ Đã BẬT duyệt 1.5s trước khi phát tiếng Anh sang Meet (Nhấn Esc để hủy).")
        else:
            self._on_ptt_status("⚡ Đã TẮT duyệt 1.5s: Phát thẳng tiếng Anh sang Meet ngay sau khi dịch.")

    def on_cancel_outgoing_clicked(self):
        if not self.outgoing_worker.cancel_current():
            self._on_ptt_status("Hiện không có câu tiếng Anh nào đang chờ phát hoặc đang đọc.")

    def on_glossary_changed(self, text: str):
        self.engine.set_custom_glossary(text)
        try:
            with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
                f.write(text.strip())
        except Exception as e:
            logger.warning(f"Không thể lưu custom_glossary.txt: {e}")

    def on_transcript_toggled(self, checked: bool):
        self.transcript_logger.enabled = checked
        state = "BẬT" if checked else "TẮT"
        self._on_hud_status(f"📝 Đã {state} tự động lưu biên bản họp song ngữ (thư mục meeting_logs/).")

    def on_stt_model_changed(self, idx: int):
        model_id = self.stt_combo.currentData()
        if model_id:
            self.engine.stt_model_name = model_id
            self._on_hud_status(f"Đã chọn model STT: {model_id}")

    def on_lang_changed(self, text: str):
        self.engine.source_lang = text.split(" ")[0]
        self.engine.reset_context()

    def on_mode_changed(self, idx: int):
        target_mode = "ollama" if idx == 0 else "nllb_ct2"
        self.engine.set_translation_mode(target_mode)

    def _on_worker_mode_synced(self, mode: str):
        target_idx = 0 if mode == "ollama" else 1
        if self.mode_combo.currentIndex() != target_idx:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(target_idx)
            self.mode_combo.blockSignals(False)
        ollama_tag = (
            f"Đang dịch bằng Ollama ({self.engine.ollama_model})"
            if mode == "ollama"
            else "Đang dịch bằng NLLB-200 INT8 Offline"
        )
        self.info_label.setText(
            f"Phần cứng: {self.profile.device_name} | STT: {self.profile.stt_backend} ({self.profile.stt_compute_type}) | {ollama_tag}"
        )

    def on_vmic_changed(self, idx: int):
        self.tts_router.virtual_mic_index = self.vmic_combo.currentData()

    def on_tts_mode_changed(self, idx: int):
        self.tts_router.tts_engine_mode = "neural_edge" if idx == 0 else "offline_native"

    def toggle_capture(self):
        if self.audio_worker and self.audio_worker.running:
            logger.info("Người dùng bấm dừng chiều nghe.")
            self.audio_worker.stop()
            if self.incoming_ai_worker:
                self.incoming_ai_worker.stop()
            self.btn_toggle.setText("▶ Bật Dịch Chiều Nghe")
            self._on_hud_status("Đã tạm dừng chiều nghe. (Chiều nói F8 vẫn hoạt động)")
        else:
            logger.info("Người dùng bấm bật chiều nghe.")
            self.engine.reset_context()
            self.overlay.reset_subtitles()
            self.btn_toggle.setText("⏹ Dừng Chiều Nghe")
            self.incoming_ai_worker = IncomingInferenceWorker(
                self.incoming_queue,
                self.engine,
                is_vad_speaking_fn=lambda: bool(self.audio_worker and self.audio_worker.is_capturing_speech),
            )
            self.incoming_ai_worker.result_ready.connect(self._on_incoming_completed)
            self.incoming_ai_worker.source_ready.connect(self.overlay.show_incoming_source)
            self.incoming_ai_worker.status_update.connect(self._on_hud_status)
            self.incoming_ai_worker.mode_synced.connect(self._on_worker_mode_synced)
            self.incoming_ai_worker.start()

            self.audio_worker = SystemAudioVADWorker(
                self.incoming_queue,
                status_callback=lambda msg: self.hud_state_signal.emit(msg),
                loopback_device_index=self.loopback_combo.currentData(),
            )
            self.audio_worker.start()

    def closeEvent(self, event):
        logger.info("Đóng ứng dụng.")
        if hasattr(self, "kb_listener") and self.kb_listener:
            try:
                self.kb_listener.stop()
            except Exception:
                pass
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
    panel.raise_()
    panel.activateWindow()
    sys.exit(app.exec())
