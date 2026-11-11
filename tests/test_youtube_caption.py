import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

from ai_engine import LocalAIEngine, OLLAMA_NUM_CTX
from browser_bridge import BridgeSession
from hardware_profile import SystemProfile
from interpreter_summary import SummarySettings
from youtube_caption import (
    CaptionCue,
    CaptionTranslationCache,
    CaptionTranslationWorker,
    is_vietnamese_track,
    normalize_yt_lang,
    parse_youtube_timedtext,
)


def _make_dummy_profile() -> SystemProfile:
    return SystemProfile(
        os_name="Windows",
        device_name="CPU Test",
        stt_backend="faster_whisper",
        stt_device="cpu",
        stt_compute_type="int8",
        recommended_whisper="deepdml/faster-whisper-large-v3-turbo-ct2",
        recommended_llm="qwen2.5:3b",
        vram_gb=8.0,
    )


class _FakeWS:
    def __init__(self):
        self.closed = False
        self.sent_messages: list[dict] = []

    async def send_str(self, text: str) -> None:
        self.sent_messages.append(json.loads(text))


class _FakeLoop:
    def __init__(self, ws: _FakeWS):
        self._ws = ws

    def call_soon_threadsafe(self, callback, *args):
        if len(args) == 1 and isinstance(args[0], dict):
            self._ws.sent_messages.append(args[0])
        else:
            callback(*args)

    def call_later(self, delay, callback, *args):
        class _DummyHandle:
            def cancel(self):
                pass
        return _DummyHandle()


class _FakeCaptionEngine:
    def __init__(self):
        self.profile = _make_dummy_profile()
        self.translation_mode = "ollama"
        self.ollama_model = "qwen2.5:3b"
        self.source_lang = "en"
        self.custom_glossary = ""
        self.batch_calls: list[tuple[list[str], bool]] = []

    def reset_context(self):
        pass

    def warmup_ollama(self):
        pass

    def _resolve_flores_lang(self, lang: str) -> str:
        return "eng_Latn"

    def _ensure_nllb_loaded(self, lang: str):
        pass

    def contains_cjk(self, text: str) -> bool:
        return False

    def translate_caption_batch(
        self,
        items: list[str],
        prev_context=None,
        next_context=None,
        source_lang=None,
        urgent: bool = False,
    ):
        self.batch_calls.append((list(items), bool(urgent)))
        out = [f"Bản dịch: {s}" for s in items]
        return out, 45.0, "Ollama"

    def generate_summary_json(self, system_msg: str, user_msg: str, max_wall_sec: float = 8.0):
        return (
            {
                "topic": "Kiến trúc dịch phụ đề YouTube",
                "new_points": ["Đã dịch trước theo cửa sổ trượt"],
                "keep_points": [],
                "question": "",
            },
            {"ok": True, "preempted": False, "prompt_ms": 50, "total_ms": 120},
        )


class TestYouTubeCaption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    def test_parse_manual_json3_merges_fragments_and_strips_noise(self):
        payload = {
            "events": [
                {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "[Music] ♪"}]},
                {"tStartMs": 1500, "dDurationMs": 1600, "segs": [{"utf8": ">> Today we&#39;re testing"}]},
                {"tStartMs": 3200, "dDurationMs": 1800, "segs": [{"utf8": "a brand new caption pipeline."}]},
                {"tStartMs": 5200, "dDurationMs": 2600, "segs": [{"utf8": "It runs 100% locally on your GPU."}]},
            ]
        }
        cues = parse_youtube_timedtext(json.dumps(payload), fmt="json3", is_asr=False)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].src, "Today we're testing a brand new caption pipeline.")
        self.assertEqual(cues[0].start_ms, 1500)
        self.assertLessEqual(cues[0].end_ms, cues[1].start_ms)
        self.assertEqual(cues[1].src, "It runs 100% locally on your GPU.")

    def test_parse_asr_json3_rolling_window_no_overlap(self):
        # Mô phỏng chính xác cấu trúc cuộn 2 dòng chồng lấp dDurationMs + aAppend của YouTube ASR
        asr_payload = {
            "events": [
                {"id": 1, "wpWinPosId": 1, "wsWinStyleId": 2},
                {
                    "tStartMs": 1000,
                    "dDurationMs": 4800,
                    "wWinId": 1,
                    "segs": [
                        {"utf8": "welcome"},
                        {"utf8": " back", "tOffsetMs": 280},
                        {"utf8": " everyone", "tOffsetMs": 600},
                        {"utf8": " to", "tOffsetMs": 950},
                        {"utf8": " our", "tOffsetMs": 1120},
                        {"utf8": " channel", "tOffsetMs": 1350},
                    ],
                },
                {"tStartMs": 3790, "dDurationMs": 2010, "wWinId": 1, "aAppend": 1, "segs": [{"utf8": "\n"}]},
                {
                    "tStartMs": 3800,
                    "dDurationMs": 4200,
                    "wWinId": 1,
                    "segs": [
                        {"utf8": "today"},
                        {"utf8": " we", "tOffsetMs": 300},
                        {"utf8": " will", "tOffsetMs": 520},
                        {"utf8": " benchmark", "tOffsetMs": 780},
                        {"utf8": " local", "tOffsetMs": 1150},
                        {"utf8": " translation", "tOffsetMs": 1450},
                    ],
                },
            ]
        }
        cues = parse_youtube_timedtext(json.dumps(asr_payload), fmt="json3", is_asr=True)
        # Khoảng nghỉ từ 2350ms -> 3800ms là 1450ms (>= 1150ms) nên phải tự ngắt thành 2 câu sạch, không chồng lấp
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].src, "welcome back everyone to our channel")
        self.assertEqual(cues[1].src, "today we will benchmark local translation")
        self.assertLessEqual(cues[0].end_ms, cues[1].start_ms)

    def test_parse_xml_srv3_and_vtt(self):
        xml_srv1 = """<transcript>
            <text start="1.0" dur="2.0">First line in XML.</text>
            <text start="3.2" dur="2.2">Second line in XML.</text>
        </transcript>"""
        cues_xml = parse_youtube_timedtext(xml_srv1, fmt="srv1", is_asr=False)
        self.assertEqual(len(cues_xml), 2)
        self.assertEqual(cues_xml[0].start_ms, 1000)

        vtt = """WEBVTT

00:00:01.000 --> 00:00:03.200
<c.colorE5E5E5>Hello from WebVTT.</c>

00:00:03.500 --> 00:00:06.000
Second VTT sentence here!
"""
        cues_vtt = parse_youtube_timedtext(vtt, fmt="vtt")
        self.assertEqual(len(cues_vtt), 2)
        self.assertEqual(cues_vtt[0].src, "Hello from WebVTT.")
        self.assertLessEqual(cues_vtt[0].end_ms, cues_vtt[1].start_ms)

    def test_translate_caption_batch_rescues_cjk_and_missing_key_without_shifting(self):
        with patch.object(LocalAIEngine, "check_ollama_running", return_value=True):
            engine = LocalAIEngine(_make_dummy_profile())
        engine.set_translation_mode("ollama")

        # Giả lập Ollama trả thiếu key "3" và rò chữ Hán ở key "2"
        fake_llm_json = json.dumps(
            {
                "1": "Chào mừng các bạn quay trở lại.",
                "2": "Hệ thống này 非常 mạnh mẽ.",
                "4": "Hãy xem kết quả thực tế ngay sau đây.",
            },
            ensure_ascii=False,
        )
        nllb_calls: list[str] = []

        def fake_nllb(text: str, src_lang: str, tgt_lang: str) -> str:
            nllb_calls.append(text)
            return f"[NLLB chuẩn] {len(nllb_calls)}"

        captured_kwargs = {}

        def fake_ollama_chat(system_msg, user_msg, num_ctx=OLLAMA_NUM_CTX, **kwargs):
            captured_kwargs["num_ctx"] = num_ctx
            captured_kwargs.update(kwargs)
            return fake_llm_json

        with patch.object(engine, "_call_ollama_chat", side_effect=fake_ollama_chat), patch.object(
            engine, "_translate_nllb", side_effect=fake_nllb
        ):
            items = [
                "Welcome back everyone.",
                "This system is extremely powerful.",
                "It keeps your audio 100% private.",
                "Let's see the real results right now.",
            ]
            vi_out, _, label = engine.translate_caption_batch(
                items,
                prev_context=[("Hi there.", "Xin chào.")],
                next_context=["First benchmark starts now."],
                source_lang="en",
                urgent=False,
            )

        self.assertEqual(captured_kwargs["num_ctx"], OLLAMA_NUM_CTX)
        self.assertFalse(captured_kwargs["urgent"])
        self.assertTrue(captured_kwargs["json_mode"])
        self.assertEqual(len(vi_out), 4)
        self.assertEqual(vi_out[0], "Chào mừng các bạn quay trở lại.")
        self.assertEqual(vi_out[1], "[NLLB chuẩn] 1")
        self.assertEqual(vi_out[2], "[NLLB chuẩn] 2")
        self.assertEqual(vi_out[3], "Hãy xem kết quả thực tế ngay sau đây.")
        self.assertEqual(label, "Ollama")
        self.assertEqual(
            nllb_calls,
            ["This system is extremely powerful.", "It keeps your audio 100% private."],
        )

    def test_cache_invalidates_on_glossary_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptionTranslationCache(cache_dir=tmp, max_files=10)
            cues_save = [
                CaptionCue(0, 1000, 3000, "Check the SLA metric.", vi="Kiểm tra chỉ số SLA.", engine="Ollama")
            ]
            cache.save_cues("vid123", "en-US", False, "ollama", "SLA -> Cam kết dịch vụ", cues_save)

            cues_hit = [CaptionCue(0, 1000, 3000, "Check the SLA metric.")]
            hits = cache.load_into_cues(
                "vid123", "en", False, "ollama", "SLA -> Cam kết dịch vụ", cues_hit
            )
            self.assertEqual(hits, 1)
            self.assertEqual(cues_hit[0].vi, "Kiểm tra chỉ số SLA.")

            cues_miss = [CaptionCue(0, 1000, 3000, "Check the SLA metric.")]
            misses = cache.load_into_cues(
                "vid123", "en", False, "ollama", "SLA -> Thỏa thuận mức dịch vụ", cues_miss
            )
            self.assertEqual(misses, 0)
            self.assertEqual(cues_miss[0].vi, "")

    def test_bridge_session_caption_window_seek_and_no_summary_spoilers(self):
        self.assertTrue(is_vietnamese_track("vi"))
        self.assertTrue(is_vietnamese_track("vi-VN"))
        self.assertFalse(is_vietnamese_track("en-US"))
        self.assertEqual(normalize_yt_lang("zh-Hans"), "zh")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("youtube_caption.CAPTION_CACHE_DIR", tmp),
            patch("browser_bridge.MEETING_LOGS_DIR", tmp),
        ):
            fake_ws = _FakeWS()
            fake_loop = _FakeLoop(fake_ws)
            engine = _FakeCaptionEngine()
            sum_cfg = SummarySettings(mode="interval", interval_sec=60.0, min_new_sentences=3)

            session = BridgeSession(
                session_id="cap-sess-1",
                ws=fake_ws,
                loop=fake_loop,
                engine=engine,
                tab_title="YouTube Tech Talk",
                tab_url="https://www.youtube.com/watch?v=abc123xyz",
                save_transcript=False,
                summary_settings=sum_cfg,
                prefer_caption=True,
            )
            session.summary_worker._enable_telemetry = False
            session.start()

            try:
                # 5 câu đầu trong cửa sổ 0..25s, và 1 câu xa ở 150s (ngoài cửa sổ +90s)
                events = [
                    {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "Sentence zero at one second."}]},
                    {"tStartMs": 5000, "dDurationMs": 3000, "segs": [{"utf8": "Sentence one at five seconds."}]},
                    {"tStartMs": 10000, "dDurationMs": 3000, "segs": [{"utf8": "Sentence two at ten seconds."}]},
                    {"tStartMs": 15000, "dDurationMs": 3000, "segs": [{"utf8": "Sentence three at fifteen seconds."}]},
                    {"tStartMs": 20000, "dDurationMs": 3000, "segs": [{"utf8": "Sentence four at twenty seconds."}]},
                    {"tStartMs": 150000, "dDurationMs": 3000, "segs": [{"utf8": "Far future sentence at 150 seconds."}]},
                ]
                ok = session.activate_caption_mode(
                    video_id="abc123xyz",
                    raw_text=json.dumps({"events": events}),
                    lang="en",
                    is_asr=False,
                    fmt="json3",
                    playhead_ms=1200,
                    playback_rate=1.0,
                )
                self.assertTrue(ok)

                qt_loop = QEventLoop()
                QTimer.singleShot(350, qt_loop.quit)
                qt_loop.exec()

                # Câu đầu tiên ngay tại playhead=1200ms phải được dịch đơn khẩn cấp (urgent=True, len=1)
                self.assertGreaterEqual(len(engine.batch_calls), 2)
                self.assertEqual(engine.batch_calls[0], (["Sentence zero at one second."], True))

                # Câu ở 150s nằm ngoài cửa sổ +90s nên chưa được phép dịch (tiết kiệm GPU)
                assert session.caption_worker is not None
                snap = session.caption_worker.get_cues_snapshot()
                self.assertTrue(snap[0]["vi"].startswith("Bản dịch:"))
                self.assertTrue(snap[4]["vi"].startswith("Bản dịch:"))
                self.assertEqual(snap[5]["vi"], "")

                # Kiểm tra chống lộ nội dung tương lai vào SummaryWorker:
                # Dù câu 1..4 (5s..20s) đã dịch xong trước, khi playhead đang ở 1.2s thì chỉ có câu 0 được đưa vào SummaryWorker!
                with session.summary_worker._lock:
                    pending_at_1s = [p[0] for p in session.summary_worker._pending]
                self.assertEqual(pending_at_1s, ["Sentence zero at one second."])

                # Khi người dùng xem tiếp tới 6.0s -> câu 1 mới được đưa vào SummaryWorker
                session.on_caption_playhead("abc123xyz", playhead_ms=6000, playback_rate=1.0, paused=False)
                with session.summary_worker._lock:
                    pending_at_6s = [p[0] for p in session.summary_worker._pending]
                self.assertEqual(
                    pending_at_6s,
                    ["Sentence zero at one second.", "Sentence one at five seconds."],
                )

                # Khi người dùng tua nhảy cóc tới 149.5s -> đánh thức dịch câu 150s mà KHÔNG đổ dồn câu 2,3,4 bị bỏ qua vào SummaryWorker
                session.on_caption_playhead(
                    "abc123xyz", playhead_ms=149500, playback_rate=1.0, paused=False, is_seek=True
                )
                qt_loop2 = QEventLoop()
                QTimer.singleShot(300, qt_loop2.quit)
                qt_loop2.exec()

                snap_after_seek = session.caption_worker.get_cues_snapshot()
                self.assertTrue(snap_after_seek[5]["vi"].startswith("Bản dịch: Far future"))
                with session.summary_worker._lock:
                    pending_after_seek = [p[0] for p in session.summary_worker._pending]
                self.assertNotIn("Sentence two at ten seconds.", pending_after_seek)
                self.assertNotIn("Sentence three at fifteen seconds.", pending_after_seek)
            finally:
                session.stop()


if __name__ == "__main__":
    unittest.main()
