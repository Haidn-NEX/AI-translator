import asyncio
import json
import logging
import os
import queue
import secrets
import socket
import threading
import time
from typing import Callable, Literal
import numpy as np
from aiohttp import WSMsgType, web
from PyQt6.QtCore import Qt
from audio_capture import (
    SAMPLE_RATE,
    SystemAudioVADWorker,
    _StreamingResampler,
    _StreamingSileroVAD,
)
from ai_engine import LocalAIEngine
from incoming_pipeline import (
    APP_DIR,
    GLOSSARY_FILE,
    MEETING_LOGS_DIR,
    IncomingInferenceWorker,
    MeetingTranscriptLogger,
    setup_logging,
)
from interpreter_summary import (
    SUMMARY_SETTINGS_FILE,
    SummarySettings,
    SummaryWorker,
)
from youtube_caption import (
    CaptionTranslationWorker,
    is_vietnamese_track,
    normalize_yt_lang,
    parse_youtube_timedtext,
)

logger = setup_logging()

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 47863
BRIDGE_TOKEN_FILE = os.path.join(APP_DIR, "bridge_token.txt")
SUMMARY_SETTINGS_PATH = os.path.join(APP_DIR, SUMMARY_SETTINGS_FILE)


def load_or_create_bridge_token(token_path: str = BRIDGE_TOKEN_FILE) -> str:
    """Đọc token từ bridge_token.txt hoặc tự sinh token mới nếu chưa tồn tại."""
    if os.path.isfile(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if len(existing) >= 16:
                return existing
        except Exception as e:
            logger.warning(f"[Bridge] Không đọc được {token_path}: {e}")

    token = secrets.token_urlsafe(24)
    try:
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception as e:
        logger.warning(f"[Bridge] Không ghi được {token_path}: {e}")
    return token


def _infer_status_level(message: str) -> str:
    msg = (message or "").lstrip()
    if msg.startswith("⏳"):
        return "loading"
    if msg.startswith("✅"):
        return "ready"
    if msg.startswith("⚠️"):
        return "warn"
    if msg.startswith("❌"):
        return "error"
    return "info"


def _adapt_status_for_extension(message: str) -> str:
    """Chuyển đổi câu thông báo của worker sang ngữ cảnh tab Chrome mà không sửa code gốc của worker."""
    if not message:
        return ""
    msg = message
    if "phím [F8]" in msg or "Sẵn sàng 2 Chiều" in msg:
        # Ví dụ: "✅ Sẵn sàng 2 Chiều (Ollama qwen2.5:3b)! Đang lắng nghe âm thanh hệ thống & phím [F8]..."
        start = msg.find("(")
        end = msg.find(")")
        if 0 <= start < end:
            engine_desc = msg[start + 1 : end]
            return f"✅ Sẵn sàng ({engine_desc})! Đang nghe âm thanh tab..."
        return "✅ Sẵn sàng! Đang nghe âm thanh tab..."
    if "Đang lắng nghe âm thanh hệ thống" in msg:
        msg = msg.replace("Đang lắng nghe âm thanh hệ thống", "Đang nghe âm thanh tab")
    return msg


class TabAudioVADWorker(SystemAudioVADWorker):
    """
    Nhận PCM Int16 mono từ WebSocket (Chrome tabCapture) thay vì WASAPI Loopback,
    rồi đưa qua NGUYÊN pipeline _process_audio_stream() sẵn có (Resampler + AGC + HPF + Silero VAD + Smart Valley Split).
    """

    def __init__(self, sentence_queue: queue.Queue, sample_rate: int = 48000, status_callback=None):
        super().__init__(sentence_queue, status_callback=status_callback, loopback_device_index=None)
        self.sample_rate = max(8000, int(sample_rate or 48000))
        self.pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self.name = f"TabAudioVAD-{self.sample_rate}Hz"

    def log(self, msg: str):
        logger.info(f"[Bridge] {msg}")
        if self.status_callback:
            self.status_callback(msg)

    def feed_pcm(self, pcm_int16_bytes: bytes) -> None:
        """Gọi từ luồng asyncio khi nhận frame binary WS, tuyệt đối không block."""
        if not self.running or not pcm_int16_bytes:
            return
        try:
            self.pcm_queue.put_nowait(pcm_int16_bytes)
        except queue.Full:
            try:
                self.pcm_queue.get_nowait()
                self.pcm_queue.put_nowait(pcm_int16_bytes)
            except queue.Empty:
                pass

    def run(self):
        self.running = True
        self.is_capturing_speech = False
        try:
            self.vad_model = _StreamingSileroVAD()
        except Exception as e:
            self.log(f"❌ Lỗi khởi tạo Silero VAD cho tab: {e}")
            self.running = False
            return

        resampler = _StreamingResampler(orig_rate=self.sample_rate, target_rate=SAMPLE_RATE)
        chunk_frames = max(320, self.sample_rate // 50)  # Khung tĩnh 20ms ở sample_rate gốc
        silent_native = np.zeros(chunk_frames, dtype=np.float32)

        def generator():
            while self.running:
                try:
                    first = self.pcm_queue.get(timeout=0.12)
                except queue.Empty:
                    # Khi tab pause hoặc không có gói âm thanh mới trong 120ms, bơm khung tĩnh 20ms
                    # để VAD xả câu cuối cùng ngay lập tức giống hệt nhánh WASAPI Loopback
                    yield resampler(silent_native)
                    continue

                blobs = [first]
                while len(blobs) < 6:
                    try:
                        blobs.append(self.pcm_queue.get_nowait())
                    except queue.Empty:
                        break
                data = b"".join(blobs) if len(blobs) > 1 else first
                if len(data) >= 2:
                    if len(data) % 2 != 0:
                        data = data[:-1]
                    pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    yield resampler(pcm)

        try:
            self.log(f"🎧 Đang thu âm thanh tab Chrome ({self.sample_rate} Hz -> {SAMPLE_RATE} Hz)...")
            self._process_audio_stream(generator())
        except Exception as e:
            logger.exception("[Bridge] Lỗi trong luồng TabAudioVADWorker")
            self.log(f"⚠️ Lỗi xử lý âm thanh tab: {e}")
        finally:
            self.is_capturing_speech = False


class BridgeSession:
    """Quản lý vòng đời 1 phiên dịch của 1 tab Chrome (tối đa 1 phiên active tại một thời điểm)."""

    def __init__(
        self,
        session_id: str,
        ws: web.WebSocketResponse,
        loop: asyncio.AbstractEventLoop,
        engine: LocalAIEngine,
        sample_rate: int = 48000,
        tab_title: str = "",
        tab_url: str = "",
        save_transcript: bool = True,
        on_mode_synced: Callable[[str], None] | None = None,
        summary_settings: SummarySettings | None = None,
        prefer_caption: bool = False,
    ):
        self.session_id = session_id
        self.ws = ws
        self.loop = loop
        self.engine = engine
        self.sample_rate = max(8000, int(sample_rate or 48000))
        self.tab_title = (tab_title or "").strip() or "Video trên Chrome"
        self.tab_url = (tab_url or "").strip()
        self.on_mode_synced = on_mode_synced
        self._prefer_caption = bool(prefer_caption)
        self._input_mode: Literal["pending", "audio", "caption"] = (
            "pending" if self._prefer_caption else "audio"
        )

        self.queue: queue.Queue = queue.Queue()
        self.transcript_logger = MeetingTranscriptLogger(
            file_prefix="video",
            title=self.tab_title,
            url=self.tab_url,
        )
        self.transcript_logger.enabled = bool(save_transcript)

        self.vad_worker: TabAudioVADWorker | None = TabAudioVADWorker(
            self.queue,
            sample_rate=self.sample_rate,
            status_callback=self._on_status_from_worker,
        )
        self.infer_worker: IncomingInferenceWorker | None = IncomingInferenceWorker(
            self.queue,
            self.engine,
            is_vad_speaking_fn=lambda: bool(self.vad_worker and self.vad_worker.is_capturing_speech),
        )
        self.caption_worker: CaptionTranslationWorker | None = None
        self._caption_video_id: str = ""
        self._caption_lang: str = "en"
        self._watched_cue_ids: set[int] = set()
        self._last_playhead_ms: int | None = None
        self._watch_segment_floor_ms: int = 0
        self._playhead_paused: bool = False
        self._caption_watch_lock = threading.Lock()
        self._mode_switch_lock = threading.Lock()
        self._caption_grace_timer: asyncio.TimerHandle | None = None

        init_sum_cfg = (
            SummarySettings(**summary_settings.to_dict())
            if isinstance(summary_settings, SummarySettings)
            else SummarySettings.load(SUMMARY_SETTINGS_PATH)
        )
        self.summary_worker = SummaryWorker(
            engine=self.engine,
            settings=init_sum_cfg,
            is_vad_speaking_fn=self._is_source_speaking,
            is_pipeline_idle_fn=self._is_pipeline_idle,
            source="chrome",
            enable_telemetry=True,
            metrics_dir=MEETING_LOGS_DIR,
        )
        self._stopped = False
        self._stop_lock = threading.Lock()

    def _is_source_speaking(self) -> bool:
        if self._input_mode == "caption" and self.caption_worker is not None:
            return self.caption_worker.is_speaking_at()
        if self._input_mode == "audio" and self.vad_worker is not None:
            return bool(self.vad_worker.is_capturing_speech)
        return False

    def _is_pipeline_idle(self) -> bool:
        if self._input_mode == "caption" and self.caption_worker is not None:
            return self.caption_worker.is_idle()
        if self._input_mode == "audio" and self.infer_worker is not None:
            return self.infer_worker.is_idle()
        return True

    def _cancel_caption_grace_timer(self) -> None:
        timer = self._caption_grace_timer
        self._caption_grace_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _schedule_send(self, payload: dict) -> None:
        if self._stopped or self.ws.closed:
            return
        try:
            self.loop.call_soon_threadsafe(self._async_send_nowait, payload)
        except RuntimeError:
            pass

    def _async_send_nowait(self, payload: dict) -> None:
        if self._stopped or self.ws.closed:
            return
        asyncio.ensure_future(self._safe_ws_send(payload))

    async def _safe_ws_send(self, payload: dict) -> None:
        if self.ws.closed:
            return
        try:
            await self.ws.send_str(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[Bridge] Không gửi được frame WS ({payload.get('type')}): {e}")

    def _on_status_from_worker(self, message: str) -> None:
        if self._stopped:
            return
        adapted = _adapt_status_for_extension(message)
        self._schedule_send(
            {
                "type": "status",
                "message": adapted,
                "level": _infer_status_level(adapted),
            }
        )

    def _on_source_ready(self, src_text: str) -> None:
        if self._stopped or not src_text:
            return
        self._schedule_send({"type": "source", "text": src_text})

    def _on_result_ready(self, src_text: str, vi_text: str, stt_ms: float, trans_ms: float) -> None:
        if self._stopped:
            return
        engine_label = "Ollama" if self.engine.translation_mode == "ollama" else "NLLB"
        self._schedule_send(
            {
                "type": "result",
                "src": src_text,
                "vi": vi_text,
                "stt_ms": round(float(stt_ms), 1),
                "trans_ms": round(float(trans_ms), 1),
                "engine": engine_label,
            }
        )
        self.transcript_logger.log_incoming(src_text, vi_text, self.engine.source_lang)
        if self.summary_worker is not None:
            self.summary_worker.add_sentence(src_text, vi_text)

    def _on_caption_track_initialized(self, payload: dict) -> None:
        if self._stopped or not isinstance(payload, dict):
            return
        self._schedule_send({"type": "caption_track_ready", **payload})
        with self._caption_watch_lock:
            cur_ms = self._last_playhead_ms or 0
        self._record_watched_cues_up_to(cur_ms, is_seek=False)

    def _on_caption_cues_ready(self, video_id: str, updated_cues: list) -> None:
        if self._stopped or not updated_cues:
            return
        self._schedule_send(
            {
                "type": "caption_cues",
                "video_id": video_id,
                "cues": updated_cues,
            }
        )
        with self._caption_watch_lock:
            cur_ms = self._last_playhead_ms
        if cur_ms is not None:
            self._record_watched_cues_up_to(cur_ms, is_seek=False)

    def _record_watched_cues_up_to(self, playhead_ms: int, is_seek: bool = False) -> None:
        """
        Chỉ ghi biên bản và đẩy vào SummaryWorker đúng những câu phụ đề tại mốc thời gian người dùng
        thực sự xem qua (không lộ nội dung tương lai +90s và không đổ dồn đoạn bị tua nhảy cóc).
        """
        worker = self.caption_worker
        if self._stopped or worker is None or self._input_mode != "caption":
            return

        new_entries: list[tuple[str, str]] = []
        with self._caption_watch_lock:
            prev_ms = self._last_playhead_ms
            jumped = (
                is_seek
                or (prev_ms is None)
                or (playhead_ms < prev_ms - 1500)
                or (playhead_ms > prev_ms + 8500)
            )
            if jumped:
                self._watch_segment_floor_ms = max(0, playhead_ms - 800)
            win_start = max(self._watch_segment_floor_ms, playhead_ms - 8000)
            win_end = playhead_ms + 300
            self._last_playhead_ms = playhead_ms

            if self._playhead_paused and not is_seek and prev_ms is not None and prev_ms == playhead_ms:
                # Vẫn cho phép ghi bù câu vừa dịch xong tại mốc hiện tại ngay cả khi video vừa pause
                win_start = max(self._watch_segment_floor_ms, playhead_ms - 3000)

            with worker._lock:
                for c in worker.cues:
                    if c.cue_id in self._watched_cue_ids or not c.vi:
                        continue
                    if c.end_ms >= win_start and c.start_ms <= win_end:
                        self._watched_cue_ids.add(c.cue_id)
                        new_entries.append((c.src, c.vi))
                    elif c.start_ms > win_end + 1000:
                        break

        for src_txt, vi_txt in new_entries:
            self.transcript_logger.log_incoming(src_txt, vi_txt, self._caption_lang)
            if self.summary_worker is not None:
                self.summary_worker.add_sentence(src_txt, vi_txt)

    def on_caption_playhead(
        self,
        video_id: str,
        playhead_ms: int,
        playback_rate: float = 1.0,
        paused: bool = False,
        is_seek: bool = False,
    ) -> None:
        if self._stopped or self._input_mode != "caption" or self.caption_worker is None:
            return
        if video_id and self._caption_video_id and video_id != self._caption_video_id:
            return
        clean_ms = max(0, int(playhead_ms or 0))
        with self._caption_watch_lock:
            self._playhead_paused = bool(paused)
        self.caption_worker.update_playhead(clean_ms, playback_rate=playback_rate, paused=paused)
        self._record_watched_cues_up_to(clean_ms, is_seek=is_seek)

    def _on_summary_ready(self, payload: dict) -> None:
        if self._stopped or not isinstance(payload, dict):
            return
        self._schedule_send({"type": "summary", **payload})
        self.transcript_logger.log_summary(payload)

    def _on_summary_status(self, message: str) -> None:
        if self._stopped:
            return
        self._schedule_send({"type": "summary_status", "message": message or ""})

    def _on_mode_synced_slot(self, mode: str) -> None:
        if self._stopped:
            return
        self._schedule_send({"type": "mode", "translation_mode": mode})
        if self.on_mode_synced:
            try:
                self.on_mode_synced(mode)
            except Exception:
                pass

    def _start_audio_workers_unlocked(self) -> None:
        # Tạo mới instance nếu worker cũ đã từng chạy và dừng (threading.Thread không thể start() 2 lần)
        if self.vad_worker is None or (not self.vad_worker.is_alive() and getattr(self.vad_worker, "_started", None) and self.vad_worker._started.is_set()):
            self.queue = queue.Queue()
            self.vad_worker = TabAudioVADWorker(
                self.queue,
                sample_rate=self.sample_rate,
                status_callback=self._on_status_from_worker,
            )
            self.infer_worker = IncomingInferenceWorker(
                self.queue,
                self.engine,
                is_vad_speaking_fn=lambda: bool(self.vad_worker and self.vad_worker.is_capturing_speech),
            )

        assert self.infer_worker is not None and self.vad_worker is not None
        self.infer_worker.source_ready.connect(self._on_source_ready, Qt.ConnectionType.DirectConnection)
        self.infer_worker.result_ready.connect(self._on_result_ready, Qt.ConnectionType.DirectConnection)
        self.infer_worker.status_update.connect(self._on_status_from_worker, Qt.ConnectionType.DirectConnection)
        self.infer_worker.mode_synced.connect(self._on_mode_synced_slot, Qt.ConnectionType.DirectConnection)
        self._input_mode = "audio"
        self.infer_worker.start()
        self.vad_worker.start()

    def _stop_audio_workers_unlocked(self) -> None:
        if self.infer_worker is not None:
            for sig, slot in (
                (self.infer_worker.source_ready, self._on_source_ready),
                (self.infer_worker.result_ready, self._on_result_ready),
                (self.infer_worker.status_update, self._on_status_from_worker),
                (self.infer_worker.mode_synced, self._on_mode_synced_slot),
            ):
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
        if self.vad_worker is not None:
            try:
                self.vad_worker.stop()
            except Exception:
                pass
        if self.infer_worker is not None:
            try:
                self.infer_worker.stop()
                if self.infer_worker.isRunning():
                    self.infer_worker.wait(3000)
            except Exception:
                pass

    def _stop_caption_worker_unlocked(self) -> None:
        cw = self.caption_worker
        self.caption_worker = None
        if cw is None:
            return
        for sig, slot in (
            (cw.track_initialized, self._on_caption_track_initialized),
            (cw.cues_ready, self._on_caption_cues_ready),
            (cw.status_update, self._on_status_from_worker),
            (cw.mode_synced, self._on_mode_synced_slot),
        ):
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        try:
            cw.stop()
            if cw.isRunning():
                cw.wait(3000)
        except Exception:
            pass

    def _on_caption_grace_timeout(self) -> None:
        if self._stopped or self._input_mode != "pending":
            return
        logger.info(
            f"[Bridge] Quá thời gian chờ phụ đề gốc trên phiên '{self.session_id}' -> tự chuyển sang thu âm thanh tab."
        )
        self._schedule_send({"type": "caption_fallback", "video_id": "", "reason": "grace_timeout"})
        threading.Thread(
            target=self.activate_audio_mode,
            daemon=True,
            name="BridgeAudioFallback",
        ).start()

    def activate_caption_mode(
        self,
        video_id: str,
        raw_text: str,
        lang: str = "en",
        is_asr: bool = False,
        fmt: str = "auto",
        playhead_ms: int = 0,
        playback_rate: float = 1.0,
        tab_title: str = "",
        tab_url: str = "",
    ) -> bool:
        if self._stopped:
            return False
        self._cancel_caption_grace_timer()

        if is_vietnamese_track(lang):
            logger.info(f"[Bridge] Track phụ đề '{lang}' đã là Tiếng Việt -> dùng luồng thu âm thanh dự phòng.")
            self._schedule_send({"type": "caption_fallback", "video_id": video_id, "reason": "already_vi"})
            self.activate_audio_mode(video_id=video_id, tab_title=tab_title, tab_url=tab_url)
            return False

        cues = parse_youtube_timedtext(raw_text, fmt=fmt, is_asr=is_asr)
        if not cues:
            logger.info(f"[Bridge] Không trích xuất được câu phụ đề từ gói '{fmt}' (video={video_id}) -> fallback Audio.")
            self._schedule_send({"type": "caption_fallback", "video_id": video_id, "reason": "empty_cues"})
            self.activate_audio_mode(video_id=video_id, tab_title=tab_title, tab_url=tab_url)
            return False

        with self._mode_switch_lock:
            if self._stopped:
                return False
            video_changed = bool(video_id and self._caption_video_id and video_id != self._caption_video_id)
            self._stop_audio_workers_unlocked()
            self._stop_caption_worker_unlocked()

            if tab_title and tab_title.strip():
                self.tab_title = tab_title.strip()
                self.transcript_logger.title = self.tab_title
            if tab_url and tab_url.strip():
                self.tab_url = tab_url.strip()
                self.transcript_logger.url = self.tab_url
            if video_changed and self.summary_worker is not None:
                self.summary_worker.reset()
                self.engine.reset_context()

            norm_lang = normalize_yt_lang(lang)
            start_ms = max(0, int(playhead_ms or 0))
            with self._caption_watch_lock:
                self._caption_video_id = str(video_id or "yt")
                self._caption_lang = norm_lang
                self._watched_cue_ids = set()
                self._last_playhead_ms = start_ms
                self._watch_segment_floor_ms = max(0, start_ms - 800)
                self._playhead_paused = False

            cw = CaptionTranslationWorker(
                video_id=str(video_id or "yt"),
                cues=cues,
                track_lang=norm_lang,
                is_asr=bool(is_asr),
                engine=self.engine,
                initial_playhead_ms=max(0, int(playhead_ms or 0)),
                playback_rate=float(playback_rate or 1.0),
            )
            cw.track_initialized.connect(
                self._on_caption_track_initialized, Qt.ConnectionType.DirectConnection
            )
            cw.cues_ready.connect(self._on_caption_cues_ready, Qt.ConnectionType.DirectConnection)
            cw.status_update.connect(self._on_status_from_worker, Qt.ConnectionType.DirectConnection)
            cw.mode_synced.connect(self._on_mode_synced_slot, Qt.ConnectionType.DirectConnection)

            self.caption_worker = cw
            self._input_mode = "caption"
            logger.info(
                f"[Bridge] Chuyển sang Caption Mode cho video '{video_id}' "
                f"(lang={norm_lang}, is_asr={is_asr}, cues={len(cues)}, playhead={playhead_ms}ms)"
            )
            cw.start()
            return True

    def activate_audio_mode(
        self, video_id: str = "", tab_title: str = "", tab_url: str = ""
    ) -> None:
        if self._stopped:
            return
        self._cancel_caption_grace_timer()
        with self._mode_switch_lock:
            if self._stopped:
                return
            video_changed = bool(video_id and self._caption_video_id and video_id != self._caption_video_id)
            self._stop_caption_worker_unlocked()

            if tab_title and tab_title.strip():
                self.tab_title = tab_title.strip()
                self.transcript_logger.title = self.tab_title
            if tab_url and tab_url.strip():
                self.tab_url = tab_url.strip()
                self.transcript_logger.url = self.tab_url
            if video_id:
                self._caption_video_id = video_id
            if video_changed and self.summary_worker is not None:
                self.summary_worker.reset()
                self.engine.reset_context()

            if self._input_mode == "audio" and self.infer_worker is not None and self.infer_worker.isRunning():
                return
            logger.info(f"[Bridge] Kích hoạt Audio Mode (tabCapture -> Whisper) cho phiên '{self.session_id}'.")
            self._start_audio_workers_unlocked()

    def start(self) -> None:
        logger.info(
            f"[Bridge] Bắt đầu phiên dịch '{self.session_id}' | Tab: '{self.tab_title}' "
            f"({self.sample_rate} Hz, prefer_caption={self._prefer_caption})"
        )
        self.engine.reset_context()
        self.summary_worker.summary_ready.connect(self._on_summary_ready, Qt.ConnectionType.DirectConnection)
        self.summary_worker.status_update.connect(self._on_summary_status, Qt.ConnectionType.DirectConnection)
        self.summary_worker.reset()
        self.summary_worker.start()

        if self._prefer_caption:
            self._on_status_from_worker("⏳ Đang kiểm tra phụ đề gốc trên YouTube...")
            try:
                self._caption_grace_timer = self.loop.call_later(2.4, self._on_caption_grace_timeout)
            except Exception:
                with self._mode_switch_lock:
                    self._start_audio_workers_unlocked()
        else:
            with self._mode_switch_lock:
                self._start_audio_workers_unlocked()

    def stop(self, reason: str = "client", send_stopped_event: bool = True) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

        self._cancel_caption_grace_timer()
        logger.info(f"[Bridge] Dừng phiên dịch '{self.session_id}' (reason={reason})")
        for sig, slot in (
            (self.summary_worker.summary_ready, self._on_summary_ready),
            (self.summary_worker.status_update, self._on_summary_status),
        ):
            try:
                sig.disconnect(slot)
            except Exception:
                pass

        with self._mode_switch_lock:
            self._stop_caption_worker_unlocked()
            self._stop_audio_workers_unlocked()

        try:
            self.summary_worker.stop()
            self.summary_worker.wait(3000)
        except Exception:
            pass

        if send_stopped_event and not self.ws.closed:
            try:
                self.loop.call_soon_threadsafe(
                    self._async_send_nowait,
                    {"type": "stopped", "session_id": self.session_id, "reason": reason},
                )
            except RuntimeError:
                pass


class BrowserBridgeServer:
    """
    Máy chủ cầu nối cục bộ (HTTP + WebSocket trên 127.0.0.1:47863) nhận luồng PCM từ Chrome Extension
    và đẩy qua LocalAIEngine dùng chung VRAM.
    """

    def __init__(
        self,
        engine: LocalAIEngine,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        token: str | None = None,
        mode: Literal["integrated", "headless"] = "integrated",
        on_session_started: Callable[[str, str], None] | None = None,
        on_session_stopped: Callable[[str, str], None] | None = None,
        on_config_updated: Callable[[dict], None] | None = None,
        on_idle_timeout: Callable[[], None] | None = None,
        idle_exit_sec: float | None = None,
        allowed_extension_id: str | None = None,
    ):
        if host != "127.0.0.1":
            raise ValueError("BrowserBridgeServer bắt buộc chỉ bind trên 127.0.0.1 vì lý do bảo mật.")
        self.engine = engine
        self.host = host
        self.port = int(port)
        self.token = token or load_or_create_bridge_token()
        self.mode = mode
        self.on_session_started = on_session_started
        self.on_session_stopped = on_session_stopped
        self.on_config_updated = on_config_updated
        self.on_idle_timeout = on_idle_timeout
        self.idle_exit_sec = float(idle_exit_sec) if (idle_exit_sec and idle_exit_sec > 0) else None
        self.allowed_extension_id = (allowed_extension_id or "").strip() or None

        self.active_session: BridgeSession | None = None
        self._session_lock = threading.Lock()
        self.summary_settings = SummarySettings.load(SUMMARY_SETTINGS_PATH)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._running = False
        self._started_event = threading.Event()
        self.bind_success = False
        self.last_activity_ts = time.monotonic()

    @property
    def models_loaded(self) -> bool:
        stt_ready = (
            self.engine.whisper_model is not None
            or self.engine.profile.stt_backend == "mlx_whisper"
        )
        nllb_ready = self.engine.nllb_translator is not None
        return bool(stt_ready and nllb_ready)

    def _is_origin_allowed(self, origin: str | None, require_extension_origin: bool) -> bool:
        if not origin:
            return not require_extension_origin
        if origin.startswith("chrome-extension://"):
            if self.allowed_extension_id:
                expected = f"chrome-extension://{self.allowed_extension_id}"
                return origin.rstrip("/") == expected.rstrip("/")
            return True
        if not require_extension_origin and (
            origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")
        ):
            return True
        return False

    def _cors_headers_for_origin(self, origin: str | None) -> dict[str, str]:
        if origin and self._is_origin_allowed(origin, require_extension_origin=False):
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        return {}

    async def _handle_options(self, request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin and not self._is_origin_allowed(origin, require_extension_origin=False):
            return web.Response(status=403, text="Forbidden Origin")
        return web.Response(status=204, headers=self._cors_headers_for_origin(origin))

    async def _handle_health(self, request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin and not self._is_origin_allowed(origin, require_extension_origin=False):
            return web.json_response({"ok": False, "error": "forbidden_origin"}, status=403)

        with self._session_lock:
            session_active = self.active_session is not None
            active_title = self.active_session.tab_title if self.active_session else None

        payload = {
            "ok": True,
            "app": "local-live-translator",
            "version": "1.0",
            "mode": self.mode,
            "models_loaded": self.models_loaded,
            "translation_mode": self.engine.translation_mode,
            "source_lang": self.engine.source_lang,
            "device": self.engine.profile.device_name,
            "session_active": session_active,
            "active_tab_title": active_title,
        }
        return web.json_response(payload, headers=self._cors_headers_for_origin(origin))

    async def _handle_token(self, request: web.Request) -> web.Response:
        """Chỉ trả về token khi request thực sự đến từ Origin chrome-extension:// hợp lệ."""
        origin = request.headers.get("Origin")
        if not self._is_origin_allowed(origin, require_extension_origin=True):
            return web.json_response({"ok": False, "error": "forbidden_origin"}, status=403)
        return web.json_response(
            {"ok": True, "token": self.token, "port": self.port},
            headers=self._cors_headers_for_origin(origin),
        )

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if not self._is_origin_allowed(origin, require_extension_origin=True):
            logger.warning(f"[Bridge] Từ chối kết nối WS do Origin không hợp lệ: {origin!r}")
            return web.json_response(
                {"ok": False, "code": "forbidden_origin", "message": "Origin không hợp lệ."},
                status=403,
            )

        req_token = request.query.get("token", "")
        if not req_token or not secrets.compare_digest(req_token, self.token):
            logger.warning("[Bridge] Từ chối kết nối WS do sai hoặc thiếu token.")
            return web.json_response(
                {"ok": False, "code": "invalid_token", "message": "Token xác thực không hợp lệ."},
                status=403,
            )

        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=25.0)
        await ws.prepare(request)
        self.last_activity_ts = time.monotonic()
        owned_session: BridgeSession | None = None

        try:
            async for msg in ws:
                self.last_activity_ts = time.monotonic()
                if msg.type == WSMsgType.BINARY:
                    if (
                        owned_session
                        and not owned_session._stopped
                        and owned_session._input_mode == "audio"
                        and owned_session.vad_worker is not None
                    ):
                        owned_session.vad_worker.feed_pcm(msg.data)
                    continue

                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                    continue

                try:
                    data = json.loads(msg.data)
                except Exception:
                    await ws.send_str(
                        json.dumps(
                            {"type": "error", "code": "invalid_json", "message": "Dữ liệu JSON không hợp lệ."},
                            ensure_ascii=False,
                        )
                    )
                    continue

                msg_type = data.get("type")
                if msg_type == "hello":
                    sum_patch = self._extract_summary_patch(data)
                    if sum_patch:
                        self.apply_summary_settings(sum_patch)
                        if self.on_config_updated:
                            try:
                                self.on_config_updated(sum_patch)
                            except Exception:
                                pass
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "hello_ack",
                                "mode": self.mode,
                                "models_loaded": self.models_loaded,
                                "translation_mode": self.engine.translation_mode,
                                "source_lang": self.engine.source_lang,
                                "device": self.engine.profile.device_name,
                                "summary_settings": self.summary_settings.to_dict(),
                            },
                            ensure_ascii=False,
                        )
                    )

                elif msg_type == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "t": data.get("t")}))

                elif msg_type == "start":
                    session_id = str(data.get("session_id") or secrets.token_hex(6))
                    sample_rate = int(data.get("sample_rate") or 48000)
                    tab_title = str(data.get("tab_title") or "Video trên Chrome")
                    tab_url = str(data.get("tab_url") or "")
                    save_transcript = bool(data.get("save_transcript", True))

                    cfg_changed = {}
                    src_lang = data.get("source_lang")
                    if src_lang in ("en", "auto", "ja", "zh", "ko"):
                        self.engine.source_lang = src_lang
                        cfg_changed["source_lang"] = src_lang

                    trans_mode = data.get("translation_mode")
                    if trans_mode in ("ollama", "nllb_ct2"):
                        self.engine.set_translation_mode(trans_mode)
                        cfg_changed["translation_mode"] = self.engine.translation_mode

                    if "glossary" in data and data["glossary"] is not None:
                        glossary_str = str(data["glossary"])
                        self._apply_glossary(glossary_str)
                        cfg_changed["glossary"] = glossary_str

                    sum_patch = self._extract_summary_patch(data)
                    if sum_patch:
                        self.apply_summary_settings(sum_patch)
                        cfg_changed.update(sum_patch)

                    if cfg_changed and self.on_config_updated:
                        try:
                            self.on_config_updated(cfg_changed)
                        except Exception:
                            pass

                    # Nếu đang có phiên cũ thì dừng phiên cũ với reason="replaced"
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._replace_or_stop_current_session, "replaced"
                    )

                    new_session = BridgeSession(
                        session_id=session_id,
                        ws=ws,
                        loop=asyncio.get_running_loop(),
                        engine=self.engine,
                        sample_rate=sample_rate,
                        tab_title=tab_title,
                        tab_url=tab_url,
                        save_transcript=save_transcript,
                        on_mode_synced=self._notify_mode_synced,
                        summary_settings=self.summary_settings,
                        prefer_caption=bool(data.get("prefer_caption", False)),
                    )
                    with self._session_lock:
                        self.active_session = new_session
                    owned_session = new_session

                    if self.on_session_started:
                        try:
                            self.on_session_started(session_id, tab_title)
                        except Exception:
                            logger.exception("[Bridge] Lỗi trong callback on_session_started")

                    new_session.start()

                elif msg_type == "config":
                    cfg_changed = {}
                    invalidate_future_captions = False
                    src_lang = data.get("source_lang")
                    if src_lang in ("en", "auto", "ja", "zh", "ko"):
                        if self.engine.source_lang != src_lang:
                            self.engine.source_lang = src_lang
                            self.engine.reset_context()
                        cfg_changed["source_lang"] = src_lang

                    trans_mode = data.get("translation_mode")
                    if trans_mode in ("ollama", "nllb_ct2"):
                        prev_mode = self.engine.translation_mode
                        self.engine.set_translation_mode(trans_mode)
                        cfg_changed["translation_mode"] = self.engine.translation_mode
                        if self.engine.translation_mode != prev_mode:
                            invalidate_future_captions = True
                        await ws.send_str(
                            json.dumps({"type": "mode", "translation_mode": self.engine.translation_mode})
                        )

                    if "glossary" in data and data["glossary"] is not None:
                        glossary_str = str(data["glossary"])
                        if glossary_str.strip() != (self.engine.custom_glossary or "").strip():
                            invalidate_future_captions = True
                        self._apply_glossary(glossary_str)
                        cfg_changed["glossary"] = glossary_str

                    if "save_transcript" in data and owned_session:
                        owned_session.transcript_logger.enabled = bool(data["save_transcript"])
                        cfg_changed["save_transcript"] = bool(data["save_transcript"])

                    if invalidate_future_captions and owned_session and owned_session.caption_worker:
                        owned_session.caption_worker.invalidate_future_translations()

                    sum_patch = self._extract_summary_patch(data)
                    if sum_patch:
                        self.apply_summary_settings(sum_patch)
                        cfg_changed.update(sum_patch)

                    if cfg_changed and self.on_config_updated:
                        try:
                            self.on_config_updated(cfg_changed)
                        except Exception:
                            pass

                elif msg_type == "captions_load":
                    if owned_session and not owned_session._stopped:
                        vid = str(data.get("video_id") or "yt")
                        raw_txt = str(data.get("raw") or "")
                        lang = str(data.get("lang") or self.engine.source_lang or "en")
                        kind = str(data.get("kind") or "")
                        is_asr = bool(data.get("is_asr", False)) or (kind.lower() == "asr")
                        fmt = str(data.get("fmt") or "auto")
                        playhead_ms = int(float(data.get("playhead_ms") or 0))
                        playback_rate = float(data.get("playback_rate") or 1.0)
                        new_title = str(data.get("tab_title") or "")
                        new_url = str(data.get("tab_url") or "")

                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: owned_session.activate_caption_mode(
                                video_id=vid,
                                raw_text=raw_txt,
                                lang=lang,
                                is_asr=is_asr,
                                fmt=fmt,
                                playhead_ms=playhead_ms,
                                playback_rate=playback_rate,
                                tab_title=new_title,
                                tab_url=new_url,
                            ),
                        )
                        if new_title and self.on_session_started:
                            try:
                                self.on_session_started(owned_session.session_id, owned_session.tab_title)
                            except Exception:
                                pass

                elif msg_type == "caption_unavailable":
                    if owned_session and not owned_session._stopped:
                        vid = str(data.get("video_id") or "")
                        new_title = str(data.get("tab_title") or "")
                        new_url = str(data.get("tab_url") or "")
                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: owned_session.activate_audio_mode(
                                video_id=vid,
                                tab_title=new_title,
                                tab_url=new_url,
                            ),
                        )
                        if new_title and self.on_session_started:
                            try:
                                self.on_session_started(owned_session.session_id, owned_session.tab_title)
                            except Exception:
                                pass

                elif msg_type in ("caption_seek", "caption_progress"):
                    if owned_session and not owned_session._stopped:
                        owned_session.on_caption_playhead(
                            video_id=str(data.get("video_id") or ""),
                            playhead_ms=int(float(data.get("playhead_ms") or 0)),
                            playback_rate=float(data.get("playback_rate") or 1.0),
                            paused=bool(data.get("paused", False)),
                            is_seek=(msg_type == "caption_seek"),
                        )

                elif msg_type == "summary_now":
                    with self._session_lock:
                        target_sess = owned_session or self.active_session
                    if target_sess and target_sess.summary_worker:
                        target_sess.summary_worker.request_now()

                elif msg_type == "stop":
                    if owned_session:
                        sid = owned_session.session_id
                        await asyncio.get_running_loop().run_in_executor(
                            None, lambda: self._stop_specific_session(owned_session, "client", True)
                        )
                        owned_session = None
                        logger.info(f"[Bridge] Đã dừng phiên '{sid}' theo yêu cầu client.")
                else:
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "error",
                                "code": "unknown_type",
                                "message": f"Không hỗ trợ loại bản tin '{msg_type}'.",
                            },
                            ensure_ascii=False,
                        )
                    )
        except Exception as e:
            logger.warning(f"[Bridge] Kết nối WS đóng do ngoại lệ: {e}")
        finally:
            self.last_activity_ts = time.monotonic()
            if owned_session:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._stop_specific_session(owned_session, "client", False)
                )
        return ws

    @staticmethod
    def _extract_summary_patch(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        patch = {}
        for k in (
            "summary_mode",
            "summary_interval_sec",
            "summary_pause_sec",
            "summary_min_new_sentences",
            "mode",
            "interval_sec",
            "pause_sec",
            "min_new_sentences",
        ):
            if k in data and data[k] is not None:
                patch[k] = data[k]
        return patch

    def apply_summary_settings(self, d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        changed = self.summary_settings.update_from_dict(d)
        self.summary_settings.save(SUMMARY_SETTINGS_PATH)
        with self._session_lock:
            sess = self.active_session
        if sess is not None and sess.summary_worker is not None:
            sess.summary_worker.apply_settings(self.summary_settings.to_dict())
        return changed

    def _apply_glossary(self, glossary_text: str) -> None:
        self.engine.set_custom_glossary(glossary_text)
        try:
            with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
                f.write(glossary_text.strip())
        except Exception as e:
            logger.warning(f"[Bridge] Không thể ghi custom_glossary.txt: {e}")

    def _notify_mode_synced(self, mode: str) -> None:
        if self.on_config_updated:
            try:
                self.on_config_updated({"translation_mode": mode})
            except Exception:
                pass

    def _stop_specific_session(
        self, session: BridgeSession, reason: str, send_stopped_event: bool = True
    ) -> None:
        was_active = False
        with self._session_lock:
            if self.active_session is session:
                self.active_session = None
                was_active = True
        session.stop(reason=reason, send_stopped_event=send_stopped_event)
        self.last_activity_ts = time.monotonic()
        if was_active and self.on_session_stopped:
            try:
                self.on_session_stopped(session.session_id, reason)
            except Exception:
                logger.exception("[Bridge] Lỗi trong callback on_session_stopped")

    def _replace_or_stop_current_session(self, reason: str) -> None:
        with self._session_lock:
            current = self.active_session
            self.active_session = None
        if current is not None:
            current.stop(reason=reason, send_stopped_event=True)
            self.last_activity_ts = time.monotonic()
            if self.on_session_stopped:
                try:
                    self.on_session_stopped(current.session_id, reason)
                except Exception:
                    logger.exception("[Bridge] Lỗi trong callback on_session_stopped")

    def stop_active_session(self, reason: str = "desktop_takeover") -> bool:
        """Dừng phiên Chrome hiện tại (an toàn đa luồng, dùng khi người dùng bật Chiều Nghe trên Desktop)."""
        with self._session_lock:
            has_session = self.active_session is not None
        if not has_session:
            return False
        self._replace_or_stop_current_session(reason)
        return True

    async def _idle_watchdog(self) -> None:
        while self._running and self.idle_exit_sec:
            await asyncio.sleep(3.0)
            with self._session_lock:
                active = self.active_session is not None
            if active:
                self.last_activity_ts = time.monotonic()
                continue
            idle_for = time.monotonic() - self.last_activity_ts
            if idle_for >= self.idle_exit_sec:
                logger.info(
                    f"[Bridge] Không có phiên dịch nào trong {idle_for:.0f}s (>= {self.idle_exit_sec:.0f}s). "
                    f"Tự động đóng Headless Bridge để giải phóng VRAM."
                )
                if self.on_idle_timeout:
                    try:
                        self.on_idle_timeout()
                    except Exception:
                        logger.exception("[Bridge] Lỗi gọi on_idle_timeout")
                break

    def _run_event_loop(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except Exception as e:
            logger.warning(f"[Bridge] Không thể khởi tạo asyncio event loop ({e}).")
            self.bind_success = False
            self._started_event.set()
            return

        app = web.Application()
        app.router.add_route("OPTIONS", "/health", self._handle_options)
        app.router.add_route("OPTIONS", "/token", self._handle_options)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/token", self._handle_token)
        app.router.add_post("/token", self._handle_token)
        app.router.add_get("/ws", self._handle_ws)

        runner = web.AppRunner(app, access_log=None)
        self._runner = runner

        async def _startup() -> bool:
            await runner.setup()
            site = web.TCPSite(runner, host=self.host, port=self.port)
            try:
                await site.start()
                return True
            except OSError as e:
                logger.warning(
                    f"[Bridge] Cổng {self.host}:{self.port} đang bận ({e}). "
                    f"Đã có một tiến trình Bridge/Desktop khác đang chạy."
                )
                await runner.cleanup()
                return False

        try:
            ok = loop.run_until_complete(_startup())
        except Exception:
            logger.exception("[Bridge] Lỗi không mong đợi khi khởi tạo aiohttp runner:")
            ok = False
        self.bind_success = ok
        self._started_event.set()
        if not ok:
            loop.close()
            return

        logger.info(
            f"[Bridge] BrowserBridgeServer ({self.mode}) đang lắng nghe tại http://{self.host}:{self.port}"
        )
        if self.idle_exit_sec:
            loop.create_task(self._idle_watchdog())

        try:
            loop.run_forever()
        finally:
            try:
                self._replace_or_stop_current_session("shutdown")
                loop.run_until_complete(runner.cleanup())
            except Exception:
                pass
            loop.close()
            logger.info("[Bridge] BrowserBridgeServer đã dừng hoàn toàn.")

    def start(self, wait_ready: bool = True, timeout: float = 5.0) -> bool:
        """Khởi chạy aiohttp server trong daemon thread riêng (không khóa Qt GUI thread)."""
        if self._running:
            return self.bind_success
        self._running = True
        self._started_event.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            daemon=True,
            name="BrowserBridge",
        )
        self._thread.start()
        if wait_ready:
            self._started_event.wait(timeout=timeout)
        return self.bind_success

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._replace_or_stop_current_session("shutdown")
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

