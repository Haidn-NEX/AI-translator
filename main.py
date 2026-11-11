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
    QPushButton, QComboBox, QCheckBox, QLabel, QLineEdit,
    QDoubleSpinBox, QSpinBox
)
from hardware_profile import detect_hardware_profile
from audio_capture import SystemAudioVADWorker, MicPushToTalkRecorder
from ai_engine import LocalAIEngine
from tts_virtual_mic import VirtualMicTTSRouter
from overlay_ui import SubtitleOverlayWindow, SummaryPanelWindow, apply_window_capture_stealth
from incoming_pipeline import (
    APP_DIR,
    LOG_FILE,
    GLOSSARY_FILE,
    MEETING_LOGS_DIR,
    _PURE_FILLERS,
    _BARE_CONJUNCTIONS,
    _DANGLING_CONNECTORS,
    setup_logging,
    IncomingInferenceWorker,
    MeetingTranscriptLogger,
)
from interpreter_summary import (
    SUMMARY_SETTINGS_FILE,
    SummarySettings,
    SummaryWorker,
)
from browser_bridge import BrowserBridgeServer

SUMMARY_SETTINGS_PATH = os.path.join(APP_DIR, SUMMARY_SETTINGS_FILE)

logger = setup_logging()


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
    bridge_event_signal = pyqtSignal(str, str, str)
    bridge_config_signal = pyqtSignal(dict)

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
        self.summary_worker: SummaryWorker | None = None

        self.summary_settings = SummarySettings.load(SUMMARY_SETTINGS_PATH)

        self.overlay = SubtitleOverlayWindow()
        self.overlay.show()
        self.summary_panel = SummaryPanelWindow(max_points_shown=self.summary_settings.max_points_shown)

        self.ptt_state_signal.connect(self._on_ptt_status)
        self.hud_state_signal.connect(self._on_hud_status)
        self.bridge_event_signal.connect(self._on_bridge_event)
        self.bridge_config_signal.connect(self._on_bridge_config_updated)

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

        # Khởi chạy BrowserBridgeServer ở chế độ Integrated (dùng chung 100% VRAM với Desktop)
        self.bridge_server = BrowserBridgeServer(
            engine=self.engine,
            mode="integrated",
            on_session_started=lambda sid, title: self.bridge_event_signal.emit("started", sid, title),
            on_session_stopped=lambda sid, reason: self.bridge_event_signal.emit("stopped", sid, reason),
            on_config_updated=lambda cfg: self.bridge_config_signal.emit(cfg),
        )
        self.bridge_server.start(wait_ready=False)

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
        self.resize(1060, 290)

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
        self.chk_click.toggled.connect(self.on_click_through_toggled)
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

        self.chrome_status_label = QLabel("🌐 Chrome: Chưa kết nối")
        self.chrome_status_label.setStyleSheet("color: #0284c7; font-size: 12px; font-weight: bold;")
        row_display.addWidget(self.chrome_status_label)
        main_layout.addLayout(row_display)

        # Hàng 1c: Tầng 2 — "🧭 Ý chính đang bàn" (Chế độ Phiên dịch viên Cabin, đổi nóng khi đang chạy)
        row_summary = QHBoxLayout()
        row_summary.setSpacing(10)
        summary_lbl = QLabel("🧭 Ý chính:")
        summary_lbl.setStyleSheet("color: #0f172a; font-weight: bold;")
        row_summary.addWidget(summary_lbl)

        self.summary_mode_combo = QComboBox()
        self.summary_mode_combo.addItem("Tắt", "off")
        self.summary_mode_combo.addItem("⏱ Theo chu kỳ", "interval")
        self.summary_mode_combo.addItem("⏸ Theo nhịp nghỉ", "pause")
        self.summary_mode_combo.setMaximumWidth(165)
        self.summary_mode_combo.currentIndexChanged.connect(self.on_summary_mode_changed)
        row_summary.addWidget(self.summary_mode_combo)

        self.summary_param_label = QLabel("Mỗi (giây):")
        row_summary.addWidget(self.summary_param_label)

        self.summary_param_spin = QDoubleSpinBox()
        self.summary_param_spin.setMaximumWidth(80)
        self.summary_param_spin.valueChanged.connect(self.on_summary_param_changed)
        row_summary.addWidget(self.summary_param_spin)

        row_summary.addWidget(QLabel("Tối thiểu câu:"))
        self.summary_min_sent_spin = QSpinBox()
        self.summary_min_sent_spin.setRange(1, 8)
        self.summary_min_sent_spin.setValue(self.summary_settings.min_new_sentences)
        self.summary_min_sent_spin.setMaximumWidth(65)
        self.summary_min_sent_spin.valueChanged.connect(self.on_summary_min_sent_changed)
        row_summary.addWidget(self.summary_min_sent_spin)

        self.btn_summary_now = QPushButton("🔄 Tổng hợp ngay")
        self.btn_summary_now.setToolTip("Tổng hợp ngay các câu mới đang chờ (dùng để kiểm thử nhanh)")
        self.btn_summary_now.setStyleSheet("font-weight: bold; padding: 3px 10px;")
        self.btn_summary_now.clicked.connect(self.on_summary_now_clicked)
        row_summary.addWidget(self.btn_summary_now)
        row_summary.addStretch(1)
        main_layout.addLayout(row_summary)

        self._sync_summary_controls_from_settings()

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

    def on_click_through_toggled(self, checked: bool):
        self.overlay.set_click_through(checked)
        self.summary_panel.set_click_through(checked)

    def on_stealth_toggled(self, checked: bool):
        self._hide_from_capture = checked
        ok_overlay = self.overlay.set_hide_from_capture(checked)
        self.summary_panel.set_hide_from_capture(checked)
        ok_panel = apply_window_capture_stealth(self, checked)
        if checked:
            if ok_overlay and ok_panel:
                self._on_hud_status("🔒 Đã BẬT ẩn phụ đề & bảng điều khiển khi Share màn hình (Zoom/Meet/Teams).")
            else:
                self._on_hud_status("⚠️ Windows hiện tại không hỗ trợ ẩn cửa sổ khỏi trình quay màn hình.")
        else:
            self._on_hud_status("👁️ Đã TẮT ẩn khi Share màn hình (Người xem Share màn hình sẽ thấy phụ đề).")

    def _sync_summary_controls_from_settings(self):
        mode = self.summary_settings.mode
        idx = self.summary_mode_combo.findData(mode)
        if idx < 0:
            idx = 1
        self.summary_mode_combo.blockSignals(True)
        self.summary_mode_combo.setCurrentIndex(idx)
        self.summary_mode_combo.blockSignals(False)

        self.summary_param_spin.blockSignals(True)
        if mode == "pause":
            self.summary_param_label.setText("Im lặng (giây):")
            self.summary_param_spin.setDecimals(1)
            self.summary_param_spin.setSingleStep(0.5)
            self.summary_param_spin.setRange(1.0, 5.0)
            self.summary_param_spin.setValue(self.summary_settings.pause_sec)
            self.summary_param_spin.setEnabled(True)
            self.summary_min_sent_spin.setEnabled(True)
        elif mode == "interval":
            self.summary_param_label.setText("Mỗi (giây):")
            self.summary_param_spin.setDecimals(0)
            self.summary_param_spin.setSingleStep(5.0)
            self.summary_param_spin.setRange(15.0, 90.0)
            self.summary_param_spin.setValue(self.summary_settings.interval_sec)
            self.summary_param_spin.setEnabled(True)
            self.summary_min_sent_spin.setEnabled(True)
        else:
            self.summary_param_label.setText("Mỗi (giây):")
            self.summary_param_spin.setDecimals(0)
            self.summary_param_spin.setSingleStep(5.0)
            self.summary_param_spin.setRange(15.0, 90.0)
            self.summary_param_spin.setValue(self.summary_settings.interval_sec)
            self.summary_param_spin.setEnabled(False)
            self.summary_min_sent_spin.setEnabled(False)
        self.summary_param_spin.blockSignals(False)

        self.summary_min_sent_spin.blockSignals(True)
        self.summary_min_sent_spin.setValue(self.summary_settings.min_new_sentences)
        self.summary_min_sent_spin.blockSignals(False)

        if mode == "off":
            self.summary_panel.hide()
        elif self.audio_worker and self.audio_worker.running:
            self.summary_panel.show()

    def _propagate_summary_settings(self):
        self.summary_settings.save(SUMMARY_SETTINGS_PATH)
        d = self.summary_settings.to_dict()
        if self.summary_worker and self.summary_worker.isRunning():
            self.summary_worker.apply_settings(d)
        if hasattr(self, "bridge_server") and self.bridge_server and hasattr(self.bridge_server, "apply_summary_settings"):
            self.bridge_server.apply_summary_settings(d)

    def on_summary_mode_changed(self, idx: int):
        new_mode = str(self.summary_mode_combo.currentData() or "interval")
        self.summary_settings.update_from_dict({"mode": new_mode})
        self._sync_summary_controls_from_settings()
        self._propagate_summary_settings()

    def on_summary_param_changed(self, val: float):
        mode = self.summary_settings.mode
        if mode == "pause":
            self.summary_settings.update_from_dict({"pause_sec": float(val)})
        else:
            self.summary_settings.update_from_dict({"interval_sec": float(val)})
        self._propagate_summary_settings()

    def on_summary_min_sent_changed(self, val: int):
        self.summary_settings.update_from_dict({"min_new_sentences": int(val)})
        self._propagate_summary_settings()

    def on_summary_now_clicked(self):
        if self.summary_worker and self.summary_worker.isRunning():
            self.summary_worker.request_now()
            self._on_hud_status("🔄 Đã yêu cầu tổng hợp ý chính ngay lập tức...")
        elif hasattr(self, "bridge_server") and self.bridge_server and getattr(self.bridge_server, "active_session", None):
            sess = self.bridge_server.active_session
            if getattr(sess, "summary_worker", None):
                sess.summary_worker.request_now()
                self._on_hud_status("🔄 Đã yêu cầu tổng hợp ý chính ngay trên phiên Chrome...")
        else:
            self._on_hud_status("ℹ️ Hãy bật Dịch Chiều Nghe (hoặc dịch tab Chrome) trước khi bấm Tổng hợp ngay.")

    def _stop_summary_worker(self):
        if self.summary_worker is not None:
            try:
                self.summary_worker.stop()
                self.summary_worker.wait(3000)
            except Exception:
                pass
            self.summary_worker = None
        self.summary_panel.hide()

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

    def _on_bridge_event(self, evt_type: str, session_id: str, detail: str):
        """Xử lý sự kiện từ BrowserBridgeServer trên luồng Qt GUI chính (an toàn đa luồng)."""
        if evt_type == "started":
            short_title = detail if len(detail) <= 36 else f"{detail[:33]}..."
            self.chrome_status_label.setText(f"🌐 Chrome: Đang dịch '{short_title}'")
            self.chrome_status_label.setStyleSheet("color: #16a34a; font-size: 12px; font-weight: bold;")
            self.chrome_status_label.setToolTip(f"Phiên Chrome ({session_id}): {detail}")

            # Độc quyền nguồn Chiều Nghe: tự dừng WASAPI Loopback nếu đang chạy
            if self.audio_worker and self.audio_worker.running:
                logger.info(
                    f"[Bridge] Phiên Chrome '{detail}' bắt đầu -> Tự động tạm dừng Chiều Nghe loa hệ thống."
                )
                self.audio_worker.stop()
                if self.incoming_ai_worker:
                    self.incoming_ai_worker.stop()
                self._stop_summary_worker()
                self.btn_toggle.setText("▶ Bật Dịch Chiều Nghe")
                self._on_hud_status(
                    f"🌐 Chrome đang dịch tab: {detail}. Đã tạm dừng Chiều Nghe loa hệ thống."
                )
            else:
                self._on_hud_status(f"🌐 Chrome đang dịch tab: {detail}")

        elif evt_type == "stopped":
            self.chrome_status_label.setText("🌐 Chrome: Chưa kết nối")
            self.chrome_status_label.setStyleSheet("color: #0284c7; font-size: 12px; font-weight: bold;")
            self.chrome_status_label.setToolTip("")
            if detail != "desktop_takeover" and not (self.audio_worker and self.audio_worker.running):
                self._on_hud_status("🌐 Đã kết thúc phiên dịch trên tab Chrome.")

    def _on_bridge_config_updated(self, cfg: dict):
        """Đồng bộ lại các điều khiển trên ControlBar khi người dùng đổi cấu hình từ Chrome Extension."""
        if not cfg:
            return
        if "source_lang" in cfg:
            lang = cfg["source_lang"]
            for i in range(self.lang_combo.count()):
                if self.lang_combo.itemText(i).startswith(f"{lang} "):
                    self.lang_combo.blockSignals(True)
                    self.lang_combo.setCurrentIndex(i)
                    self.lang_combo.blockSignals(False)
                    break
        if "translation_mode" in cfg:
            self._on_worker_mode_synced(cfg["translation_mode"])
        if "glossary" in cfg and cfg["glossary"] is not None:
            new_gloss = str(cfg["glossary"]).strip()
            if self.glossary_input.text().strip() != new_gloss:
                self.glossary_input.blockSignals(True)
                self.glossary_input.setText(new_gloss)
                self.glossary_input.blockSignals(False)
        if any(
            k in cfg
            for k in (
                "summary_mode",
                "summary_interval_sec",
                "summary_pause_sec",
                "summary_min_new_sentences",
                "mode",
                "interval_sec",
                "pause_sec",
                "min_new_sentences",
            )
        ):
            if self.summary_settings.update_from_dict(cfg):
                self._sync_summary_controls_from_settings()
                if self.summary_worker and self.summary_worker.isRunning():
                    self.summary_worker.apply_settings(self.summary_settings.to_dict())

    def toggle_capture(self):
        if self.audio_worker and self.audio_worker.running:
            logger.info("Người dùng bấm dừng chiều nghe.")
            self.audio_worker.stop()
            if self.incoming_ai_worker:
                self.incoming_ai_worker.stop()
            self._stop_summary_worker()
            self.btn_toggle.setText("▶ Bật Dịch Chiều Nghe")
            self._on_hud_status("Đã tạm dừng chiều nghe. (Chiều nói F8 vẫn hoạt động)")
        else:
            logger.info("Người dùng bấm bật chiều nghe.")
            # Độc quyền nguồn Chiều Nghe: nếu đang có phiên tab Chrome thì dừng phiên tab trước
            if hasattr(self, "bridge_server") and self.bridge_server:
                self.bridge_server.stop_active_session(reason="desktop_takeover")
            self._stop_summary_worker()
            self.engine.reset_context()
            self.overlay.reset_subtitles()
            self.btn_toggle.setText("⏹ Dừng Chiều Nghe")
            vad_speaking_fn = lambda: bool(self.audio_worker and self.audio_worker.is_capturing_speech)
            self.incoming_ai_worker = IncomingInferenceWorker(
                self.incoming_queue,
                self.engine,
                is_vad_speaking_fn=vad_speaking_fn,
            )
            self.incoming_ai_worker.result_ready.connect(self._on_incoming_completed)
            self.incoming_ai_worker.source_ready.connect(self.overlay.show_incoming_source)
            self.incoming_ai_worker.status_update.connect(self._on_hud_status)
            self.incoming_ai_worker.mode_synced.connect(self._on_worker_mode_synced)

            self.summary_worker = SummaryWorker(
                engine=self.engine,
                settings=SummarySettings.from_dict(self.summary_settings.to_dict())
                if hasattr(SummarySettings, "from_dict")
                else SummarySettings(**self.summary_settings.to_dict()),
                is_vad_speaking_fn=vad_speaking_fn,
                is_pipeline_idle_fn=self.incoming_ai_worker.is_idle,
                source="desktop",
                enable_telemetry=True,
                metrics_dir=MEETING_LOGS_DIR,
            )
            self.incoming_ai_worker.result_ready.connect(
                lambda src, vi, _stt, _trans: self.summary_worker.add_sentence(src, vi)
                if self.summary_worker
                else None
            )
            self.summary_worker.summary_ready.connect(self.summary_panel.update_summary)
            self.summary_worker.summary_ready.connect(self.transcript_logger.log_summary)
            self.summary_worker.status_update.connect(self.summary_panel.set_status)
            self.summary_panel.reset()
            if self.summary_settings.mode != "off":
                self.summary_panel.show()
            else:
                self.summary_panel.hide()
            self.summary_worker.start()
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
        if hasattr(self, "bridge_server") and self.bridge_server:
            try:
                self.bridge_server.stop()
            except Exception:
                pass
        if self.audio_worker:
            self.audio_worker.stop()
        if self.incoming_ai_worker:
            self.incoming_ai_worker.stop()
        self._stop_summary_worker()
        if self.outgoing_worker:
            self.outgoing_worker.stop()
        self.summary_panel.close()
        self.overlay.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = ControlBar()
    panel.show()
    panel.raise_()
    panel.activateWindow()
    sys.exit(app.exec())
