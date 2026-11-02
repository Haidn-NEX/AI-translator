import ctypes
import logging
import platform
import re
import time
from collections import deque
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGraphicsDropShadowEffect
)

logger = logging.getLogger("LiveTranslator")

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+ / Windows 11: tàng hình 100% khi Share Entire Screen


def apply_window_capture_stealth(widget: QWidget, enabled: bool) -> bool:
    """
    Ẩn cửa sổ khỏi tính năng Chia sẻ toàn màn hình (Zoom / Google Meet / Teams / OBS) trên Windows 10/11
    nhưng mắt người dùng nhìn trực tiếp trên màn hình vẫn thấy rõ 100%.
    """
    if platform.system() != "Windows" or widget is None:
        return False
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        flag = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        ok = bool(user32.SetWindowDisplayAffinity(hwnd, flag))
        if not ok and enabled:
            # Fallback cho bản Windows 10 cũ (< build 2004)
            ok = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR))
        return ok
    except Exception as e:
        logger.warning(f"Không thể đặt chế độ ẩn khi Share màn hình: {e}")
        return False


class SubtitleOverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI 2-Way Live Subtitle Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(980)
        self.resize(980, 195)
        self._drag_pos = None
        self._hide_from_capture = (platform.system() == "Windows")

        # Quản lý trạng thái phụ đề cuộn 2 tầng (Rolling 2-Line), gộp câu theo ngữ pháp & bộ điều nhịp đọc (Reading Pacer)
        self._current_src = ""
        self._current_vi = ""
        self._prev_vi = ""
        self._last_update_ts = 0.0
        self._line_shown_ts = 0.0
        self._show_bilingual = True
        self._display_queue: deque[tuple[str, str]] = deque()

        self._pacer_timer = QTimer(self)
        self._pacer_timer.setSingleShot(True)
        self._pacer_timer.timeout.connect(self._on_pacer_tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)

        self.container = QWidget(self)
        self.container.setStyleSheet("""
            QWidget {
                background-color: rgba(15, 18, 24, 198);
                border: 1px solid rgba(255, 255, 255, 52);
                border-radius: 12px;
            }
        """)
        box_layout = QVBoxLayout(self.container)
        box_layout.setContentsMargins(16, 8, 16, 10)
        box_layout.setSpacing(4)

        # 1. Thanh Latency HUD & Trạng thái hệ thống
        self.hud_label = QLabel("Sẵn sàng (Kéo thả khung này để di chuyển) | Giữ phím [F8] để nói Tiếng Việt -> Phát Tiếng Anh")
        self.hud_label.setStyleSheet("color: #7dd3fc; font-size: 11px; background: transparent; border: none;")
        box_layout.addWidget(self.hud_label)

        # 2. Thanh kiểm tra Chiều Nói (Outgoing Voice Feedback)
        self.outgoing_label = QLabel("🎙️ [Chiều nói VI -> EN]: Nhấn giữ phím F8 để phát biểu bằng tiếng Việt...")
        self.outgoing_label.setWordWrap(True)
        self.outgoing_label.setStyleSheet("color: #4ade80; font-size: 13px; font-weight: bold; background: transparent; border: none;")
        box_layout.addWidget(self.outgoing_label)

        # 3. Dòng lịch sử câu tiếng Việt vừa qua (Cuộn 2 tầng - ẩn gọn khi chưa có câu lịch sử)
        self.prev_vi_label = QLabel("")
        self.prev_vi_label.setWordWrap(True)
        self.prev_vi_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_vi_label.setStyleSheet("color: #94a3b8; font-size: 15px; font-style: italic; background: transparent; border: none;")
        self.prev_vi_label.setVisible(False)
        box_layout.addWidget(self.prev_vi_label)

        # 4. Dòng phụ đề gốc Chiều Nghe (tiếng Anh/Nhật/Trung...)
        self.src_label = QLabel("Phụ đề gốc (Chiều nghe) sẽ hiển thị tại đây...")
        self.src_label.setWordWrap(True)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.src_label.setStyleSheet("color: #cbd5e1; font-size: 14px; background: transparent; border: none;")
        box_layout.addWidget(self.src_label)

        # 5. Dòng phụ đề tiếng Việt Chiều Nghe hiện tại
        self.vi_label = QLabel("Bản dịch tiếng Việt thời gian thực")
        self.vi_label.setWordWrap(True)
        self.vi_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vi_label.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        self.vi_label.setStyleSheet("color: #fef08a; font-size: 21px; font-weight: bold; background: transparent; border: none;")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(8)
        shadow.setColor(QColor(0, 0, 0, 220))
        shadow.setOffset(1, 1)
        self.vi_label.setGraphicsEffect(shadow)
        box_layout.addWidget(self.vi_label)

        layout.addWidget(self.container)
        self.position_at_bottom_center()

    def position_at_bottom_center(self):
        """Đặt khung phụ đề ở sát đáy màn hình để KHÔNG bao giờ đè lên bảng Control Panel ở phía trên"""
        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            x = avail.x() + (avail.width() - self.width()) // 2
            y = avail.y() + avail.height() - self.height() - 35
            self.move(max(0, x), max(0, y))

    def _sync_window_height(self):
        """Tự co giãn chiều cao mượt theo số dòng thực tế (neo cố định cạnh đáy) để không bao giờ bị cắt cụt chữ"""
        if self._drag_pos is not None:
            return
        if self.layout() is not None:
            self.layout().activate()
        min_h = 142 if not self._show_bilingual else 172
        hint_h = self.sizeHint().height()
        target_h = max(min_h, min(330, hint_h))
        cur_h = self.height()
        if abs(target_h - cur_h) >= 6:
            bottom_y = self.y() + cur_h
            new_y = max(0, bottom_y - target_h)
            self.setGeometry(self.x(), new_y, self.width(), target_h)

    @staticmethod
    def _format_prev_line(prev_vi: str) -> str:
        text = (prev_vi or "").strip()
        if not text:
            return ""
        if len(text) <= 135:
            return f"⤴ {text}"
        tail = text[-128:]
        space_idx = tail.find(" ")
        if 0 < space_idx < 25:
            tail = tail[space_idx + 1 :]
        return f"⤴ …{tail}"

    @staticmethod
    def _merge_text_cleanly(left: str, right: str, src_right: str = "") -> str:
        """Nối 2 mảnh câu bị ngắt hơi thành 1 câu liền mạch, tự động dọn dấu '...' thừa ở ranh giới nối"""
        l_raw = (left or "").strip()
        r_raw = (right or "").strip()
        if not l_raw:
            return r_raw
        if not r_raw:
            return l_raw

        left_open = l_raw.endswith(("...", "…", "-", "—")) or l_raw[-1:] not in ".!?"
        right_cont = r_raw.startswith(("...", "…"))
        l_clean = re.sub(r"(?:\.{2,}|…|\s*-\s*)$", "", l_raw).rstrip()
        r_clean = re.sub(r"^(?:\.{2,}|…)\s*", "", r_raw).lstrip()

        if (
            left_open
            and r_clean
            and len(r_clean) >= 2
            and r_clean[0].isupper()
            and not r_clean[:2].isupper()
        ):
            src_r_clean = re.sub(r"^(?:\.{2,}|…)\s*", "", (src_right or "").strip())
            if right_cont or (src_r_clean and src_r_clean[0].islower()):
                r_clean = r_clean[0].lower() + r_clean[1:]

        return re.sub(r"\s{2,}", " ", f"{l_clean} {r_clean}").strip()

    @staticmethod
    def _is_syntactic_continuation(cur_src: str, cur_vi: str, new_src: str, new_vi: str) -> bool:
        cs = (cur_src or "").rstrip()
        cv = (cur_vi or "").rstrip()
        ns = (new_src or "").lstrip()
        nv = (new_vi or "").lstrip()
        if cs.endswith(("...", "…", ",", "-", "—")) or cv.endswith(("...", "…", ",", "-", "—")):
            return True
        if ns.startswith(("...", "…")) or nv.startswith(("...", "…")):
            return True
        if cs and cs[-1] not in ".!?":
            return True
        return False

    def _should_merge_with_current(self, src_text: str, vi_text: str, elapsed: float) -> bool:
        if not self._current_vi or self._display_queue:
            return False
        cur_words = len(self._current_vi.split())
        new_words = len(vi_text.split())
        if cur_words + new_words > 22:
            return False

        # 1. Nối mượt khi câu trước còn bỏ lửng ngữ pháp ('...', ',', chưa có dấu chấm kết câu)
        if elapsed < 4.2 and self._is_syntactic_continuation(self._current_src, self._current_vi, src_text, vi_text):
            return True

        # 2. Gộp mảnh câu cực ngắn (<= 4 từ) đến nhanh để tránh chớp nháy dòng chính
        if new_words <= 4 and elapsed < 2.6 and (cur_words + new_words <= 18):
            return True
        if cur_words <= 4 and new_words <= 9 and elapsed < 2.0:
            return True
        return False

    def _calculate_min_dwell_sec(self) -> float:
        """Tính thời gian lưu tối thiểu trên dòng vàng chính theo độ dài câu để mắt người đọc kịp hiểu"""
        q_len = len(self._display_queue)
        if q_len >= 2:
            return 0.95
        words = len(self._current_vi.split())
        dwell = min(3.1, max(1.45, 0.95 + words * 0.13))
        if q_len == 1:
            return min(dwell, 2.15)
        return dwell

    def _roll_and_render(self, src_text: str, vi_text: str, now: float):
        if self._current_vi:
            self._prev_vi = self._current_vi
            self.prev_vi_label.setText(self._format_prev_line(self._prev_vi))
            self.prev_vi_label.setVisible(True)
        self._current_src = src_text
        self._current_vi = vi_text
        self._line_shown_ts = now
        self._last_update_ts = now
        self.src_label.setText(self._current_src)
        self.vi_label.setText(self._current_vi)
        self._sync_window_height()

    def _on_pacer_tick(self):
        if not self._display_queue:
            return
        now = time.monotonic()
        next_src, next_vi = self._display_queue.popleft()
        # Nếu ngay sau câu vừa lấy ra là một mảnh đuôi ngắn nối tiếp ngữ pháp thì ghép gọn luôn một lần
        if self._display_queue:
            peek_src, peek_vi = self._display_queue[0]
            if (
                len(next_vi.split()) + len(peek_vi.split()) <= 22
                and (
                    self._is_syntactic_continuation(next_src, next_vi, peek_src, peek_vi)
                    or len(peek_vi.split()) <= 3
                )
            ):
                self._display_queue.popleft()
                next_src = self._merge_text_cleanly(next_src, peek_src, peek_src)
                next_vi = self._merge_text_cleanly(next_vi, peek_vi, peek_src)

        self._roll_and_render(next_src, next_vi, now)
        if self._display_queue:
            wait_ms = int(max(120.0, self._calculate_min_dwell_sec() * 1000.0))
            self._pacer_timer.start(wait_ms)

    def show_incoming_source(self, src_text: str):
        """Báo trước câu gốc vừa nhận diện trên HUD mà không ghi đè sớm làm lệch pha dòng phụ đề hiện tại"""
        if not self._current_vi and not self._display_queue:
            self.src_label.setText(src_text)
            self.hud_label.setText("⚡ Đã nhận diện xong, đang dịch sang tiếng Việt...")
        else:
            preview = src_text if len(src_text) <= 75 else src_text[:72] + "..."
            self.hud_label.setText(f"⚡ Đang dịch tiếp: \"{preview}\"")

    def update_incoming_subtitle(self, src_text: str, vi_text: str, stt_ms: float, trans_ms: float, device_tag: str):
        total_ms = stt_ms + trans_ms
        self.hud_label.setText(
            f"[Chiều Nghe - {device_tag}] STT: {stt_ms:.0f} ms | Dịch: {trans_ms:.0f} ms | Tổng: {total_ms:.0f} ms  (Giữ F8 để nói)"
        )

        now = time.monotonic()
        elapsed = now - self._last_update_ts if self._last_update_ts > 0 else 999.0

        # 1. Chưa có câu nào trên màn hình -> hiển thị ngay lập tức
        if not self._current_vi:
            self._roll_and_render(src_text, vi_text, now)
            return

        # 2. Mảnh câu nối tiếp ngữ pháp hoặc mảnh cực ngắn -> ghép mượt tại chỗ, không tạo hiệu ứng nháy dòng
        if self._should_merge_with_current(src_text, vi_text, elapsed):
            self._current_src = self._merge_text_cleanly(self._current_src, src_text, src_text)
            self._current_vi = self._merge_text_cleanly(self._current_vi, vi_text, src_text)
            self._last_update_ts = now
            # Cộng thêm thời gian đọc tương ứng cho phần từ mới vừa nối thêm
            extra_sec = 0.35 + len(vi_text.split()) * 0.12
            self._line_shown_ts = min(now, self._line_shown_ts + extra_sec)
            self.src_label.setText(self._current_src)
            self.vi_label.setText(self._current_vi)
            self._sync_window_height()
            return

        # 3. Nếu đang có hàng đợi điều nhịp hoặc câu hiện tại trên dòng chính chưa đủ thời gian đọc tối thiểu
        dwell_elapsed = now - self._line_shown_ts
        req_dwell = self._calculate_min_dwell_sec()
        if self._display_queue or dwell_elapsed < req_dwell:
            self._display_queue.append((src_text, vi_text))
            # Chống trễ tuyệt đối: nếu dồn > 2 câu trong hàng đợi UI thì cuộn ngay câu đầu tiên lên
            if len(self._display_queue) > 2:
                self._pacer_timer.stop()
                self._on_pacer_tick()
                return
            rem_sec = max(0.08, self._calculate_min_dwell_sec() - (now - self._line_shown_ts))
            if not self._pacer_timer.isActive():
                self._pacer_timer.start(int(rem_sec * 1000.0))
            else:
                self._pacer_timer.start(min(self._pacer_timer.remainingTime(), int(rem_sec * 1000.0)))
            return

        # 4. Đã đủ thời gian đọc thoải mái -> cuộn mượt câu cũ lên dòng lịch sử và hiện câu mới ngay
        self._roll_and_render(src_text, vi_text, now)

    def set_show_source_subtitle(self, show_src: bool):
        """Bật/tắt hiển thị dòng tiếng Anh gốc để xem nhanh chỉ tiếng Việt"""
        self._show_bilingual = bool(show_src)
        self.src_label.setVisible(self._show_bilingual)
        self._sync_window_height()

    def reset_subtitles(self):
        """Làm mới trạng thái phụ đề khi bắt đầu phiên nghe mới"""
        self._pacer_timer.stop()
        self._display_queue.clear()
        self._current_src = ""
        self._current_vi = ""
        self._prev_vi = ""
        self._last_update_ts = 0.0
        self._line_shown_ts = 0.0
        self.prev_vi_label.setText("")
        self.prev_vi_label.setVisible(False)
        self.src_label.setText("Phụ đề gốc (Chiều nghe) sẽ hiển thị tại đây...")
        self.vi_label.setText("Bản dịch tiếng Việt thời gian thực")
        self._sync_window_height()

    def update_outgoing_status(self, vi_said: str, en_spoken: str, stt_ms: float, trans_ms: float, tts_ms: float):
        total = stt_ms + trans_ms + tts_ms
        self.outgoing_label.setText(
            f"🎙️ Bạn nói: \"{vi_said}\"  ➔  🔊 Phát sang Meet: \"{en_spoken}\" "
            f"(STT: {stt_ms:.0f}ms | Dịch: {trans_ms:.0f}ms | TTS: {tts_ms:.0f}ms | Tổng: {total:.0f}ms)"
        )
        self._sync_window_height()

    def set_hide_from_capture(self, enabled: bool) -> bool:
        self._hide_from_capture = bool(enabled)
        return apply_window_capture_stealth(self, self._hide_from_capture)

    def showEvent(self, event):
        super().showEvent(event)
        if self._hide_from_capture:
            apply_window_capture_stealth(self, True)

    def set_click_through(self, enabled: bool):
        pos = self.pos()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        self.move(pos)
        self.show()
        # Gắn lại cờ ẩn khỏi Share Screen vì setWindowFlag() tạo lại native HWND trên Windows
        if self._hide_from_capture:
            apply_window_capture_stealth(self, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
