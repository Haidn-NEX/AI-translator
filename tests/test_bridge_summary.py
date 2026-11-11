import asyncio
import json
import os
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt
from browser_bridge import BridgeSession, BrowserBridgeServer
from interpreter_summary import SummarySettings


class FakeProfile:
    device_name = "CPU Test"
    stt_backend = "faster_whisper"


class FakeAIEngine:
    def __init__(self):
        self.profile = FakeProfile()
        self.prefer_ollama = True
        self.translation_mode = "ollama"
        self.source_lang = "en"
        self.whisper_model = object()
        self.nllb_translator = object()
        self.custom_glossary = "Q3, Gross Margin"

    def reset_context(self):
        pass

    def contains_cjk(self, text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    def generate_summary_json(self, system_msg: str, user_msg: str, max_wall_sec: float = 8.0):
        data = {
            "topic": "Báo cáo doanh thu quý 3",
            "new_points": [
                "Doanh thu quý 3 tăng 18% so với quý trước",
                "Biên lợi nhuận gộp đạt mức 42%",
            ],
            "keep_points": [],
            "question": "Kế hoạch ngân sách marketing quý 4 là bao nhiêu?",
        }
        metrics = {
            "ok": True,
            "preempted": False,
            "reason": "ok",
            "prompt_ms": 110,
            "gen_ms": 230,
            "total_ms": 340,
            "input_chars": len(user_msg),
            "output_chars": 150,
        }
        return data, metrics


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent_messages: list[dict] = []

    async def send_str(self, text: str) -> None:
        self.sent_messages.append(json.loads(text))


class FakeEventLoop:
    """Event loop giả lập cho môi trường test không mở socketpair."""

    def __init__(self, ws: FakeWebSocket):
        self._ws = ws

    def call_soon_threadsafe(self, callback, *args):
        if len(args) == 1 and isinstance(args[0], dict):
            self._ws.sent_messages.append(args[0])
        else:
            callback(*args)


class TestBridgeSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._tmp_dir.name, "interpreter_settings.json")

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_hello_ack_and_config_sync_and_summary_now(self):
        """
        Kiểm tra:
        1. Gửi config với summary_mode='pause' cập nhật summary_settings và hello_ack.summary_settings đồng bộ đúng.
        2. Gửi summary_now kích hoạt SummaryWorker phát sự kiện {'type': 'summary', ...} qua WS.
        """
        engine = FakeAIEngine()
        server = BrowserBridgeServer(
            engine=engine,
            host="127.0.0.1",
            port=47869,
            token="test_secret_token_1234567890abcdef",
            mode="headless",
        )
        server.summary_settings = SummarySettings.load(self.settings_path)

        # 1. Trích xuất và áp dụng config với summary_mode="pause"
        patch = server._extract_summary_patch(
            {
                "type": "config",
                "summary_mode": "pause",
                "summary_pause_sec": 2.5,
                "summary_min_new_sentences": 2,
            }
        )
        server.apply_summary_settings(patch)
        self.assertEqual(server.summary_settings.mode, "pause")
        self.assertAlmostEqual(server.summary_settings.pause_sec, 2.5)
        self.assertEqual(server.summary_settings.min_new_sentences, 2)

        # Kiểm tra cấu trúc hello_ack.summary_settings đồng bộ đúng
        hello_ack_payload = {
            "type": "hello_ack",
            "mode": server.mode,
            "models_loaded": server.models_loaded,
            "translation_mode": server.engine.translation_mode,
            "source_lang": server.engine.source_lang,
            "device": server.engine.profile.device_name,
            "summary_settings": server.summary_settings.to_dict(),
        }
        self.assertEqual(hello_ack_payload["summary_settings"]["mode"], "pause")
        self.assertAlmostEqual(hello_ack_payload["summary_settings"]["pause_sec"], 2.5)
        self.assertEqual(hello_ack_payload["summary_settings"]["min_new_sentences"], 2)

        # 2. Khởi tạo BridgeSession và kích hoạt summary_now -> kiểm tra sự kiện {"type": "summary", ...}
        fake_ws = FakeWebSocket()
        fake_loop = FakeEventLoop(fake_ws)
        session = BridgeSession(
            session_id="test-summary-session",
            ws=fake_ws,
            loop=fake_loop,
            engine=engine,
            sample_rate=48000,
            tab_title="Test Summary Video",
            tab_url="https://example.com/video",
            save_transcript=False,
            summary_settings=server.summary_settings,
        )
        with server._session_lock:
            server.active_session = session

        session.infer_worker.is_loading_models = False
        session.summary_worker.summary_ready.connect(
            session._on_summary_ready, Qt.ConnectionType.DirectConnection
        )
        session.summary_worker.status_update.connect(
            session._on_summary_status, Qt.ConnectionType.DirectConnection
        )
        session.summary_worker.reset()
        session.summary_worker.start()

        try:
            session.summary_worker.add_sentence(
                "Q3 revenue grew 18 percent quarter over quarter.",
                "Doanh thu quý 3 tăng 18% so với quý trước.",
            )
            session.summary_worker.add_sentence(
                "Gross margin reached 42 percent.",
                "Biên lợi nhuận gộp đạt 42%.",
            )

            # Mô phỏng nhận {"type": "summary_now"} từ WS
            session.summary_worker.request_now()

            qt_loop = QEventLoop()
            QTimer.singleShot(700, qt_loop.quit)
            qt_loop.exec()

            summary_msgs = [m for m in fake_ws.sent_messages if m.get("type") == "summary"]
            self.assertEqual(
                len(summary_msgs), 1, f"Kỳ vọng 1 sự kiện summary, nhận: {fake_ws.sent_messages}"
            )
            s_evt = summary_msgs[0]
            self.assertEqual(s_evt["topic"], "Báo cáo doanh thu quý 3")
            self.assertEqual(s_evt["trigger"], "manual")
            self.assertEqual(s_evt["new_count"], 2)
            self.assertGreaterEqual(len(s_evt["points"]), 2)
        finally:
            session.summary_worker.stop()
            session.summary_worker.wait(2000)


if __name__ == "__main__":
    unittest.main()
