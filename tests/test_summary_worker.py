import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

from ai_engine import LocalAIEngine
from interpreter_summary import (
    SummarySettings,
    SummaryWorker,
    build_summary_prompt,
    decide_trigger,
    validate_summary,
)


class FakeClock:
    def __init__(self, initial: float = 100.0):
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class FakeEngine:
    def __init__(self, responses: list[tuple[dict | None, dict]], clock: FakeClock | None = None):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.custom_glossary = "QA, SLA"
        self.clock = clock

    def _can_use_ollama_now(self) -> bool:
        return True

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return LocalAIEngine._contains_cjk(text)

    def generate_summary_json(self, system_msg: str, user_msg: str, max_wall_sec: float = 8.0):
        self.calls.append((system_msg, user_msg))
        if self.responses:
            data, meta = self.responses.pop(0)
        else:
            data, meta = None, {"ok": False, "error": "no_more_responses"}
        if self.clock is not None and (meta.get("preempted") or meta.get("busy")):
            # Tua đồng hồ giả vượt qua retry_after = now + 1.0s để luồng test không phải chờ thực
            self.clock.advance(1.2)
        return data, meta


class TestSummaryWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_settings_clamp_and_persistence(self):
        s = SummarySettings(mode="invalid", interval_sec=5.0, pause_sec=99.0, min_new_sentences=20).clamp()
        self.assertEqual(s.mode, "interval")
        self.assertEqual(s.interval_sec, 15.0)
        self.assertEqual(s.pause_sec, 5.0)
        self.assertEqual(s.min_new_sentences, 8)

        with tempfile.TemporaryDirectory() as tmpdir:
            p = os.path.join(tmpdir, "test_settings.json")
            s.save(p)
            loaded = SummarySettings.load(p)
            self.assertEqual(loaded.to_dict(), s.to_dict())

    def test_decide_trigger_interval_pause_cap_and_off(self):
        # 1. Mode interval: đủ thời gian nhưng thiếu câu -> None; đủ câu -> "interval"
        cfg_interval = SummarySettings(mode="interval", interval_sec=30.0, min_new_sentences=3)
        state = {"pending_count": 2, "last_run_ts": 100.0, "last_voice_ts": 130.0, "first_pending_ts": 105.0}
        self.assertIsNone(decide_trigger(state, cfg_interval, now=135.0))

        state["pending_count"] = 3
        self.assertIsNone(decide_trigger(state, cfg_interval, now=120.0))
        self.assertEqual(decide_trigger(state, cfg_interval, now=130.0), "interval")

        # 2. Mode pause: đúng ngưỡng im lặng + đủ câu -> "pause"
        cfg_pause = SummarySettings(
            mode="pause",
            pause_sec=2.0,
            min_new_sentences=3,
            max_wait_sec=60.0,
            max_pending_sentences=12,
        )
        state_p = {
            "pending_count": 3,
            "last_run_ts": 100.0,
            "last_voice_ts": 110.0,
            "first_pending_ts": 101.0,
        }
        self.assertIsNone(decide_trigger(state_p, cfg_pause, now=111.5))
        self.assertEqual(decide_trigger(state_p, cfg_pause, now=112.0), "pause")

        # 3. Chốt cap theo số câu (max_pending_sentences) khi người nói không nghỉ
        state_cap_cnt = {
            "pending_count": 12,
            "last_run_ts": 100.0,
            "last_voice_ts": 125.0,
            "first_pending_ts": 105.0,
        }
        self.assertEqual(decide_trigger(state_cap_cnt, cfg_pause, now=125.2), "cap")

        # 4. Chốt cap theo thời gian chờ (max_wait_sec) dù chỉ có 1 câu
        state_cap_wait = {
            "pending_count": 1,
            "last_run_ts": 100.0,
            "last_voice_ts": 165.0,
            "first_pending_ts": 105.0,
        }
        self.assertEqual(decide_trigger(state_cap_wait, cfg_pause, now=165.0), "cap")

        # 5. Mode off: chỉ nhận manual (khi có >= 1 câu)
        cfg_off = SummarySettings(mode="off")
        state_off = {
            "pending_count": 5,
            "last_run_ts": 0.0,
            "last_voice_ts": 0.0,
            "first_pending_ts": 1.0,
            "manual_requested": False,
        }
        self.assertIsNone(decide_trigger(state_off, cfg_off, now=500.0))
        state_off["manual_requested"] = True
        self.assertEqual(decide_trigger(state_off, cfg_off, now=500.0), "manual")

    def test_build_summary_prompt_budget_and_glossary(self):
        prev_notes = {"topic": "Kế hoạch Q4", "points": ["Lùi lịch 2 tuần"]}
        lines = [
            ("First old sentence that should be dropped when budget is tight.", "Câu cũ 1"),
            ("Second old sentence that should also be dropped.", "Câu cũ 2"),
            ("Newest critical sentence that must be kept in prompt.", "Câu mới nhất"),
        ]
        # Khi glossary rỗng -> bỏ dòng Glossary / proper nouns
        sys_msg, user_msg, truncated = build_summary_prompt(prev_notes, lines, glossary="", max_input_chars=1400)
        self.assertNotIn("Glossary / proper nouns:", user_msg)
        self.assertFalse(truncated)
        self.assertIn("1. First old sentence", user_msg)
        self.assertIn("3. Newest critical sentence", user_msg)

        # Khi có glossary và ngân sách hẹp -> giữ câu mới nhất, truncated=True
        _, user_msg_tight, truncated_tight = build_summary_prompt(
            prev_notes,
            lines,
            glossary="Kubernetes, SLA",
            max_input_chars=195,
        )
        self.assertTrue(truncated_tight)
        self.assertIn("Glossary / proper nouns: Kubernetes, SLA", user_msg_tight)
        self.assertIn("Newest critical sentence", user_msg_tight)
        self.assertNotIn("First old sentence", user_msg_tight)

    def test_validate_summary_cjk_dedup_and_fallback_topic(self):
        prev_notes = {
            "topic": "Ngân sách Marketing Q4",
            "points": ["Tăng ngân sách quảng cáo 15%"],
        }
        raw_json = {
            "topic": "",  # Topic rỗng -> giữ topic cũ
            "new_points": [
                "Tăng ngân sách quảng cáo 15%!",  # Trùng với điểm cũ sau khi bỏ dấu câu -> loại
                "Cần bổ sung 2 kỹ sư诸位 QA",      # Chứa chữ Hán -> loại
                "Hoàn tất kiểm thử trước ngày 29/11",  # Hợp lệ
                "Hoàn tất kiểm thử trước ngày 29/11.", # Trùng ngay trong lượt -> loại
            ],
            # Thiếu khóa question_to_listener -> mặc định ""
        }
        res = validate_summary(raw_json, prev_notes, LocalAIEngine._contains_cjk)
        self.assertIsNotNone(res)
        self.assertEqual(res["topic"], "Ngân sách Marketing Q4")
        self.assertEqual(res["new_points"], ["Hoàn tất kiểm thử trước ngày 29/11"])
        self.assertEqual(res["question_to_listener"], "")

        # Trường hợp toàn bộ rỗng và không có topic cũ -> trả None
        empty_res = validate_summary(
            {"topic": "中文主題", "new_points": ["汉字"], "question_to_listener": "Привет"},
            {"topic": "", "points": []},
            LocalAIEngine._contains_cjk,
        )
        self.assertIsNone(empty_res)

    def test_worker_preempted_twice_then_succeeds_without_losing_sentences(self):
        clock = FakeClock(1000.0)
        responses = [
            (None, {"ok": False, "preempted": True, "busy": False, "error": "preempted", "prompt_ms": 0.0, "total_ms": 15.0}),
            (None, {"ok": False, "preempted": True, "busy": False, "error": "preempted", "prompt_ms": 0.0, "total_ms": 18.0}),
            (
                {
                    "topic": "Tiến độ kiểm thử thanh toán",
                    "new_points": [
                        "Đội QA cần thêm 2 tuần để test hồi quy",
                        "Đề xuất dời lịch phát hành sang 29/11",
                    ],
                    "question_to_listener": "Bạn có đồng ý lùi lịch 2 tuần không?",
                },
                {"ok": True, "preempted": False, "busy": False, "error": "", "prompt_ms": 320.0, "total_ms": 910.0},
            ),
        ]
        engine = FakeEngine(responses, clock=clock)
        settings = SummarySettings(mode="interval", interval_sec=20.0, min_new_sentences=3)
        def _tick_vad():
            if len(engine.calls) >= 1:
                clock.advance(1.1)
            return False

        worker = SummaryWorker(
            engine=engine,
            settings=settings,
            is_vad_speaking_fn=_tick_vad,
            is_pipeline_idle_fn=lambda: True,
            clock=clock,
            tick_sleep_sec=0.015,
            enable_telemetry=False,
        )

        emitted: list[dict] = []
        loop = QEventLoop()

        def _on_summary(payload: dict):
            emitted.append(payload)
            loop.quit()

        worker.summary_ready.connect(_on_summary)
        worker.add_sentence("Sentence one about QA regression testing.", "Câu 1")
        worker.add_sentence("Sentence two about moving deadline to Nov 29.", "Câu 2")
        worker.add_sentence("Sentence three asking if you agree.", "Câu 3")
        # Tua đồng hồ vượt interval_sec=20s để kích hoạt trigger "interval"
        clock.advance(25.0)
        worker.start()
        try:
            if not emitted:
                QTimer.singleShot(2000, loop.quit)
                loop.exec()
        finally:
            worker.stop()
            worker.wait(2000)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(len(engine.calls), 3)
        # Cả 3 lần gọi đều phải chứa đủ cả 3 câu (không bị rơi mất câu sau 2 lần preempt)
        for _, usr_prompt in engine.calls:
            self.assertIn("Sentence one about QA regression testing.", usr_prompt)
            self.assertIn("Sentence two about moving deadline to Nov 29.", usr_prompt)
            self.assertIn("Sentence three asking if you agree.", usr_prompt)

        payload = emitted[0]
        self.assertEqual(payload["topic"], "Tiến độ kiểm thử thanh toán")
        self.assertEqual(payload["new_count"], 2)
        self.assertEqual(payload["sentences"], 3)
        self.assertEqual(payload["trigger"], "interval")

    def test_apply_settings_hot_switch_mode_preserves_pending(self):
        clock = FakeClock(500.0)
        engine = FakeEngine(
            [
                (
                    {
                        "topic": "Chuyển đổi chế độ nóng",
                        "new_points": ["Giữ nguyên các câu đang chờ khi đổi sang pause"],
                        "question_to_listener": "",
                    },
                    {"ok": True, "preempted": False, "busy": False, "error": "", "prompt_ms": 200.0, "total_ms": 600.0},
                )
            ],
            clock=clock,
        )
        settings = SummarySettings(mode="interval", interval_sec=60.0, pause_sec=2.0, min_new_sentences=2)
        def _tick_vad():
            if len(engine.calls) >= 1:
                clock.advance(1.1)
            return False

        worker = SummaryWorker(
            engine=engine,
            settings=settings,
            is_vad_speaking_fn=_tick_vad,
            is_pipeline_idle_fn=lambda: True,
            clock=clock,
            tick_sleep_sec=0.015,
            enable_telemetry=False,
        )

        emitted: list[dict] = []
        loop = QEventLoop()

        def _on_summary_pause(payload: dict):
            emitted.append(payload)
            loop.quit()

        worker.summary_ready.connect(_on_summary_pause)

        worker.add_sentence("Pending line A before mode change.", "Câu A")
        worker.add_sentence("Pending line B before mode change.", "Câu B")
        # Đổi nóng từ interval (60s) sang pause (2.0s) khi đang chạy
        worker.apply_settings({"mode": "pause", "pause_sec": 2.0})
        self.assertEqual(len(worker._pending), 2)

        # Tua đồng hồ 2.5s (>= pause_sec=2.0s) để kích hoạt theo mode pause mới
        clock.advance(2.5)
        worker.start()
        try:
            if not emitted:
                QTimer.singleShot(2000, loop.quit)
                loop.exec()
        finally:
            worker.stop()
            worker.wait(2000)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["mode"], "pause")
        self.assertEqual(emitted[0]["trigger"], "pause")
        self.assertEqual(emitted[0]["sentences"], 2)
        self.assertIn("Pending line A before mode change.", engine.calls[0][1])
        self.assertIn("Pending line B before mode change.", engine.calls[0][1])


if __name__ == "__main__":
    unittest.main()
