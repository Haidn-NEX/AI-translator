import csv
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal

import ai_engine

logger = logging.getLogger("LiveTranslator")

SUMMARY_SETTINGS_FILE = "interpreter_settings.json"

SUMMARY_SYSTEM_PROMPT = (
    "You are the note-taker for a simultaneous interpreter. You receive NEW transcript lines (any language)\n"
    "from a live meeting/video and your PREVIOUS NOTES. Update the notes for a Vietnamese listener.\n"
    'Return ONLY a JSON object: {"topic": string, "new_points": [string], "question_to_listener": string}\n'
    "RULES:\n"
    "1. All values in natural Vietnamese (100% Quốc ngữ). NEVER output Chinese/Cyrillic characters.\n"
    '2. "topic": <= 12 words, the subject being discussed NOW. Keep the previous topic if unchanged.\n'
    '3. "new_points": 0-3 items, each <= 20 words, ONLY information from NEW LINES that is not already in\n'
    "   PREVIOUS NOTES. Keep numbers, dates, names, decisions and commitments exact.\n"
    '4. "question_to_listener": if a speaker asks the listener/audience a question or requests an action,\n'
    '   summarize it in <= 20 words; otherwise "".\n'
    "5. Ignore greetings, fillers and repetitions. NEVER invent facts that are not in NEW LINES."
)

VALID_SUMMARY_MODES = ("off", "interval", "pause")


@dataclass
class SummarySettings:
    mode: str = "interval"          # "off" | "interval" | "pause"
    interval_sec: float = 30.0      # 15..90
    pause_sec: float = 2.0          # 1.0..5.0
    min_new_sentences: int = 3      # 1..8
    max_wait_sec: float = 60.0      # chốt an toàn cho mode pause
    max_pending_sentences: int = 12 # chốt an toàn cho mode pause
    max_points_shown: int = 4

    def clamp(self) -> "SummarySettings":
        if self.mode not in VALID_SUMMARY_MODES:
            self.mode = "interval"
        try:
            self.interval_sec = float(self.interval_sec)
        except (TypeError, ValueError):
            self.interval_sec = 30.0
        self.interval_sec = max(15.0, min(90.0, self.interval_sec))

        try:
            self.pause_sec = float(self.pause_sec)
        except (TypeError, ValueError):
            self.pause_sec = 2.0
        self.pause_sec = max(1.0, min(5.0, self.pause_sec))

        try:
            self.min_new_sentences = int(self.min_new_sentences)
        except (TypeError, ValueError):
            self.min_new_sentences = 3
        self.min_new_sentences = max(1, min(8, self.min_new_sentences))

        try:
            self.max_wait_sec = float(self.max_wait_sec)
        except (TypeError, ValueError):
            self.max_wait_sec = 60.0
        self.max_wait_sec = max(10.0, min(300.0, self.max_wait_sec))

        try:
            self.max_pending_sentences = int(self.max_pending_sentences)
        except (TypeError, ValueError):
            self.max_pending_sentences = 12
        self.max_pending_sentences = max(1, min(100, self.max_pending_sentences))

        try:
            self.max_points_shown = int(self.max_points_shown)
        except (TypeError, ValueError):
            self.max_points_shown = 4
        self.max_points_shown = max(1, min(12, self.max_points_shown))
        return self

    @classmethod
    def load(cls, path: str = SUMMARY_SETTINGS_FILE) -> "SummarySettings":
        inst = cls()
        if not path or not os.path.isfile(path):
            return inst.clamp()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                inst.update_from_dict(data)
        except Exception as e:
            logger.warning(f"Không thể đọc {path} ({e}), dùng cài đặt mặc định.")
        return inst.clamp()

    def save(self, path: str = SUMMARY_SETTINGS_FILE) -> None:
        if not path:
            return
        try:
            self.clamp()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Không thể lưu {path}: {e}")

    def to_dict(self) -> dict:
        self.clamp()
        return {
            "mode": self.mode,
            "interval_sec": self.interval_sec,
            "pause_sec": self.pause_sec,
            "min_new_sentences": self.min_new_sentences,
            "max_wait_sec": self.max_wait_sec,
            "max_pending_sentences": self.max_pending_sentences,
            "max_points_shown": self.max_points_shown,
        }

    def update_from_dict(self, d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        before = self.to_dict()

        if "mode" in d and d["mode"] is not None:
            self.mode = str(d["mode"]).strip()
        elif "summary_mode" in d and d["summary_mode"] is not None:
            self.mode = str(d["summary_mode"]).strip()

        if "interval_sec" in d and d["interval_sec"] is not None:
            self.interval_sec = d["interval_sec"]
        elif "summary_interval_sec" in d and d["summary_interval_sec"] is not None:
            self.interval_sec = d["summary_interval_sec"]

        if "pause_sec" in d and d["pause_sec"] is not None:
            self.pause_sec = d["pause_sec"]
        elif "summary_pause_sec" in d and d["summary_pause_sec"] is not None:
            self.pause_sec = d["summary_pause_sec"]

        if "min_new_sentences" in d and d["min_new_sentences"] is not None:
            self.min_new_sentences = d["min_new_sentences"]
        elif "summary_min_new_sentences" in d and d["summary_min_new_sentences"] is not None:
            self.min_new_sentences = d["summary_min_new_sentences"]

        if "max_wait_sec" in d and d["max_wait_sec"] is not None:
            self.max_wait_sec = d["max_wait_sec"]

        if "max_pending_sentences" in d and d["max_pending_sentences"] is not None:
            self.max_pending_sentences = d["max_pending_sentences"]

        if "max_points_shown" in d and d["max_points_shown"] is not None:
            self.max_points_shown = d["max_points_shown"]

        after = self.clamp().to_dict()
        return after != before


def _get_state_val(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def decide_trigger(state: Any, settings: SummarySettings, now: float) -> str | None:
    """
    Quyết định lý do kích hoạt tóm tắt:
    Trả về "interval" | "pause" | "cap" | "manual" | None.
    """
    pending_count = _get_state_val(state, "pending_count", None)
    if pending_count is None:
        pending_seq = _get_state_val(state, "pending", ())
        pending_count = len(pending_seq) if pending_seq is not None else 0
    pending_count = int(pending_count)

    if pending_count < 1:
        return None

    manual_requested = bool(_get_state_val(state, "manual_requested", False))
    if manual_requested:
        return "manual"

    mode = settings.mode
    if mode == "off":
        return None

    retry_after = float(_get_state_val(state, "retry_after", 0.0) or 0.0)
    if now < retry_after:
        return None

    if mode == "interval":
        last_run_ts = float(_get_state_val(state, "last_run_ts", 0.0) or 0.0)
        if (now - last_run_ts) >= settings.interval_sec and pending_count >= settings.min_new_sentences:
            return "interval"
        return None

    if mode == "pause":
        last_voice_ts = float(_get_state_val(state, "last_voice_ts", 0.0) or 0.0)
        if (now - last_voice_ts) >= settings.pause_sec and pending_count >= settings.min_new_sentences:
            return "pause"

        if pending_count >= settings.max_pending_sentences:
            return "cap"

        first_pending_ts = _get_state_val(state, "first_pending_ts", None)
        if first_pending_ts is not None and (now - float(first_pending_ts)) >= settings.max_wait_sec:
            return "cap"
        return None

    return None


def _extract_line_text(item: Any) -> str:
    """Ưu tiên câu gốc src; chỉ khi src rỗng mới dùng bản dịch vi."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        src = str(item.get("src") or "").strip()
        vi = str(item.get("vi") or "").strip()
        return src if src else vi
    if isinstance(item, (tuple, list)):
        src = str(item[0] or "").strip() if len(item) > 0 else ""
        vi = str(item[1] or "").strip() if len(item) > 1 else ""
        return src if src else vi
    return str(item or "").strip()


def build_summary_prompt(
    prev_notes: dict | None,
    new_lines: list[Any],
    glossary: str = "",
    max_input_chars: int | None = None,
) -> tuple[str, str, bool]:
    """
    Xây dựng (system_msg, user_msg, truncated).
    Nếu vượt ngân sách max_input_chars thì giữ các câu mới nhất và đặt truncated=True.
    """
    if max_input_chars is None:
        max_input_chars = 1400 if getattr(ai_engine, "OLLAMA_NUM_CTX", 1024) <= 1024 else 3600

    prev_notes = prev_notes or {}
    prev_topic = str(prev_notes.get("topic") or "").strip() or "(chưa có)"
    raw_prev_points = list(prev_notes.get("points") or [])[:3]

    header_lines: list[str] = []
    if isinstance(glossary, (list, tuple)):
        glossary_clean = ", ".join(str(x).strip() for x in glossary if str(x).strip())
    else:
        glossary_clean = str(glossary or "").strip()
    if glossary_clean:
        header_lines.append(f"Glossary / proper nouns: {glossary_clean}")
    header_lines.append("PREVIOUS NOTES:")
    header_lines.append(f"topic: {prev_topic}")
    if raw_prev_points:
        pts_str = "\n".join(f"- {str(p).strip()}" for p in raw_prev_points if str(p).strip())
        header_lines.append(f"points:\n{pts_str}" if pts_str else "points:")
    else:
        header_lines.append("points:")
    header_lines.append("NEW LINES:")
    header_prefix = "\n".join(header_lines) + "\n"

    extracted = [t for t in (_extract_line_text(x) for x in (new_lines or [])) if t]
    if not extracted:
        return SUMMARY_SYSTEM_PROMPT, header_prefix.rstrip("\n"), False

    def _format_user_msg(lines_slice: list[str]) -> str:
        numbered = "\n".join(f"{idx + 1}. {line}" for idx, line in enumerate(lines_slice))
        return header_prefix + numbered

    full_msg = _format_user_msg(extracted)
    if len(full_msg) <= max_input_chars:
        return SUMMARY_SYSTEM_PROMPT, full_msg, False

    truncated = True
    kept: list[str] = []
    for line in reversed(extracted):
        candidate = [line] + kept
        if len(_format_user_msg(candidate)) <= max_input_chars or not kept:
            kept = candidate
        else:
            break

    user_msg = _format_user_msg(kept)
    if len(user_msg) > max_input_chars and len(kept) == 1:
        budget_for_line = max(16, max_input_chars - len(header_prefix) - 4)
        kept = [kept[0][-budget_for_line:].lstrip()]
        user_msg = _format_user_msg(kept)

    return SUMMARY_SYSTEM_PROMPT, user_msg, truncated


def _normalize_for_dedup(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def validate_summary(
    data: Any,
    prev_notes: dict | None,
    contains_cjk_fn: Callable[[str], bool] | None = None,
) -> dict | None:
    """
    Làm sạch và kiểm tra kết quả JSON từ LLM.
    Trả về dict {"topic", "new_points", "question_to_listener", "question"} hoặc None nếu rỗng hoàn toàn.
    """
    if not isinstance(data, dict):
        return None

    if contains_cjk_fn is None:
        contains_cjk_fn = ai_engine.LocalAIEngine._contains_cjk

    prev_notes = prev_notes or {}
    prev_topic = str(prev_notes.get("topic") or "").strip()
    prev_points = [str(p).strip() for p in (prev_notes.get("points") or []) if str(p).strip()]

    raw_topic = data.get("topic")
    topic = str(raw_topic).strip() if isinstance(raw_topic, (str, int, float)) and raw_topic is not None else ""
    topic = re.sub(r"\s+", " ", topic)[:90].strip()
    if topic and contains_cjk_fn(topic):
        topic = ""
    if not topic:
        topic = prev_topic

    raw_points = data.get("new_points")
    if isinstance(raw_points, str):
        candidates = [raw_points] if raw_points.strip() else []
    elif isinstance(raw_points, (list, tuple)):
        candidates = [
            str(item) for item in raw_points if isinstance(item, (str, int, float)) and item is not None
        ]
    else:
        candidates = []

    seen_norms = {_normalize_for_dedup(p) for p in prev_points if _normalize_for_dedup(p)}
    clean_points: list[str] = []
    for raw_pt in candidates:
        pt = re.sub(r"^[\s•\-\*\u2022]+", "", str(raw_pt).strip())
        pt = re.sub(r"\s+", " ", pt)[:140].strip()
        if not pt:
            continue
        if contains_cjk_fn(pt):
            continue
        norm = _normalize_for_dedup(pt)
        if not norm or norm in seen_norms:
            continue
        seen_norms.add(norm)
        clean_points.append(pt)
        if len(clean_points) >= 3:
            break

    raw_q = data.get("question_to_listener")
    if raw_q is None and "question" in data:
        raw_q = data.get("question")
    question = str(raw_q).strip() if isinstance(raw_q, (str, int, float)) and raw_q is not None else ""
    question = re.sub(r"\s+", " ", question)[:140].strip()
    if question and contains_cjk_fn(question):
        question = ""

    if not topic and not clean_points and not question:
        return None

    return {
        "topic": topic,
        "new_points": clean_points,
        "question_to_listener": question,
        "question": question,
    }


class SummaryWorker(QThread):
    summary_ready = pyqtSignal(dict)
    status_update = pyqtSignal(str)

    def __init__(
        self,
        engine: Any,
        settings: SummarySettings,
        is_vad_speaking_fn: Callable[[], bool] | None = None,
        is_pipeline_idle_fn: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        source: str = "desktop",
        tick_sleep_sec: float = 0.20,
        enable_telemetry: bool = False,
        metrics_dir: str = "meeting_logs",
    ):
        super().__init__()
        self.engine = engine
        self.settings = settings.clamp()
        self._is_vad_speaking_fn = is_vad_speaking_fn
        self._is_pipeline_idle_fn = is_pipeline_idle_fn
        self._clock = clock
        self.source = source
        self._tick_sleep_sec = max(0.01, float(tick_sleep_sec))
        self.enable_telemetry = enable_telemetry
        self.metrics_dir = metrics_dir
        self._metrics_csv_path: str | None = None

        self._lock = threading.Lock()
        self.running = False

        now0 = self._clock()
        self._pending: deque[tuple[str, str, float]] = deque()
        self._notes: dict[str, Any] = {
            "topic": "",
            "points": deque(maxlen=12),
            "question": "",
            "question_ts": 0.0,
        }
        self._last_run_ts: float = now0
        self._last_successful_ts: float | None = None
        self._last_voice_ts: float = now0
        self._retry_after: float = 0.0
        self._preempt_count: int = 0
        self._manual_requested: bool = False
        self._cap_or_manual_wait_since: float | None = None
        self._no_ollama_notified: bool = False

    def add_sentence(self, src_text: str, vi_text: str) -> None:
        src = (src_text or "").strip()
        vi = (vi_text or "").strip()
        if not src and not vi:
            return
        with self._lock:
            now = self._clock()
            self._last_voice_ts = now
            self._pending.append((src, vi, now))

    def apply_settings(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        with self._lock:
            old_mode = self.settings.mode
            self.settings.update_from_dict(d)
            if self.settings.mode != old_mode:
                self._last_run_ts = self._clock()

    def request_now(self) -> None:
        with self._lock:
            self._manual_requested = True
            self._retry_after = 0.0

    def reset(self) -> None:
        with self._lock:
            now = self._clock()
            self._pending.clear()
            self._notes = {
                "topic": "",
                "points": deque(maxlen=12),
                "question": "",
                "question_ts": 0.0,
            }
            self._last_run_ts = now
            self._last_successful_ts = None
            self._last_voice_ts = now
            self._retry_after = 0.0
            self._preempt_count = 0
            self._manual_requested = False
            self._cap_or_manual_wait_since = None
            self._no_ollama_notified = False
            self._metrics_csv_path = None

    def stop(self) -> None:
        self.running = False

    def _sleep_tick(self) -> None:
        remaining = self._tick_sleep_sec
        step = min(0.05, remaining)
        while self.running and remaining > 0:
            time.sleep(min(step, remaining))
            remaining -= step

    def _write_telemetry_row(
        self,
        *,
        mode: str,
        trigger: str,
        sentences: int,
        input_chars: int,
        truncated: bool,
        prompt_ms: float,
        total_ms: float,
        preempt_count: int,
        ok: bool,
        error: str,
        new_points: int,
        has_question: bool,
        gap_since_last_s: float,
    ) -> None:
        if not self.enable_telemetry or not self.metrics_dir:
            return
        try:
            os.makedirs(self.metrics_dir, exist_ok=True)
            if not self._metrics_csv_path:
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                self._metrics_csv_path = os.path.join(
                    self.metrics_dir, f"summary_metrics_{stamp}.csv"
                )
            file_exists = os.path.isfile(self._metrics_csv_path)
            with open(self._metrics_csv_path, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "ts",
                        "source",
                        "mode",
                        "trigger",
                        "sentences",
                        "input_chars",
                        "truncated",
                        "prompt_ms",
                        "total_ms",
                        "preempt_count",
                        "ok",
                        "error",
                        "new_points",
                        "has_question",
                        "gap_since_last_s",
                    ])
                writer.writerow([
                    f"{time.time():.3f}",
                    self.source,
                    mode,
                    trigger,
                    sentences,
                    input_chars,
                    int(bool(truncated)),
                    f"{prompt_ms:.1f}",
                    f"{total_ms:.1f}",
                    preempt_count,
                    int(bool(ok)),
                    error or "",
                    new_points,
                    int(bool(has_question)),
                    f"{gap_since_last_s:.1f}",
                ])
        except Exception as e:
            logger.warning(f"Không thể ghi CSV telemetry tóm tắt: {e}")

    def run(self) -> None:
        self.running = True

        while self.running:
            now = self._clock()
            try:
                if callable(self._is_vad_speaking_fn) and self._is_vad_speaking_fn():
                    with self._lock:
                        self._last_voice_ts = now
            except Exception:
                pass

            with self._lock:
                first_pending_ts = self._pending[0][2] if self._pending else None
                state = {
                    "pending_count": len(self._pending),
                    "last_run_ts": self._last_run_ts,
                    "last_voice_ts": self._last_voice_ts,
                    "first_pending_ts": first_pending_ts,
                    "manual_requested": self._manual_requested,
                    "retry_after": self._retry_after,
                }
                trigger = decide_trigger(state, self.settings, now)

            if trigger is None:
                self._cap_or_manual_wait_since = None
                self._sleep_tick()
                continue

            is_idle = True
            if callable(self._is_pipeline_idle_fn):
                try:
                    is_idle = bool(self._is_pipeline_idle_fn())
                except Exception:
                    is_idle = True

            if not is_idle:
                if trigger in ("cap", "manual"):
                    if self._cap_or_manual_wait_since is None:
                        self._cap_or_manual_wait_since = now
                    if (now - self._cap_or_manual_wait_since) < 3.0:
                        self._sleep_tick()
                        continue
                else:
                    self._sleep_tick()
                    continue

            self._cap_or_manual_wait_since = None

            with self._lock:
                if not self._pending:
                    self._manual_requested = False
                    self._sleep_tick()
                    continue
                in_flight = list(self._pending)
                self._pending.clear()
                was_manual = self._manual_requested
                if trigger == "manual":
                    self._manual_requested = False
                prev_notes_snapshot = {
                    "topic": self._notes["topic"],
                    "points": list(self._notes["points"]),
                    "question": self._notes["question"],
                }
                cur_mode = self.settings.mode
                max_points_shown = self.settings.max_points_shown

            glossary = getattr(self.engine, "custom_glossary", "") or ""
            system_msg, user_msg, truncated = build_summary_prompt(
                prev_notes_snapshot,
                [(s, v) for s, v, _ in in_flight],
                glossary=glossary,
            )
            input_chars = len(user_msg)

            can_ollama = True
            if hasattr(self.engine, "_can_use_ollama_now"):
                try:
                    can_ollama = bool(self.engine._can_use_ollama_now())
                except Exception:
                    can_ollama = True

            if can_ollama:
                self.status_update.emit("⏳ Đang tổng hợp ý...")

            data, meta = self.engine.generate_summary_json(system_msg, user_msg)
            meta = meta or {}
            now_after = self._clock()
            err = str(meta.get("error") or "")

            if err == "no_ollama":
                with self._lock:
                    for item in reversed(in_flight):
                        self._pending.appendleft(item)
                    while len(self._pending) > 40:
                        self._pending.popleft()
                    self._last_run_ts = now_after
                    self._retry_after = now_after + 2.0
                    should_notify = not self._no_ollama_notified
                    self._no_ollama_notified = True
                if should_notify:
                    self.status_update.emit("🧭 Ý chính cần Ollama — đang tạm dừng")
                self._sleep_tick()
                continue

            if meta.get("preempted") or meta.get("busy"):
                with self._lock:
                    for item in reversed(in_flight):
                        self._pending.appendleft(item)
                    self._preempt_count += 1
                    cur_preempt_cnt = self._preempt_count
                    if cur_preempt_cnt >= 4:
                        self._preempt_count = 0
                        self._last_run_ts = now_after
                        self._retry_after = now_after + 1.0
                        while len(self._pending) > 40:
                            self._pending.popleft()
                        starved = True
                    else:
                        self._retry_after = now_after + 1.0
                        if was_manual:
                            self._manual_requested = True
                        starved = False

                if starved:
                    gap_s = (
                        (now_after - self._last_successful_ts)
                        if self._last_successful_ts is not None
                        else (now_after - self._last_run_ts)
                    )
                    self._write_telemetry_row(
                        mode=cur_mode,
                        trigger=trigger,
                        sentences=len(in_flight),
                        input_chars=input_chars,
                        truncated=truncated,
                        prompt_ms=float(meta.get("prompt_ms", 0.0) or 0.0),
                        total_ms=float(meta.get("total_ms", 0.0) or 0.0),
                        preempt_count=cur_preempt_cnt,
                        ok=False,
                        error="starved",
                        new_points=0,
                        has_question=False,
                        gap_since_last_s=gap_s,
                    )
                    self.status_update.emit("")
                self._sleep_tick()
                continue

            # Đã hoàn tất lời gọi Ollama (thành công hoặc lỗi JSON/timeout)
            with self._lock:
                self._no_ollama_notified = False
                turn_preempt_cnt = self._preempt_count
                self._preempt_count = 0
                prev_success_ts = self._last_successful_ts
                prev_run_ts = self._last_run_ts
                self._last_run_ts = now_after

            gap_since_last_s = (
                (now_after - prev_success_ts)
                if prev_success_ts is not None
                else (now_after - prev_run_ts)
            )

            if not meta.get("ok"):
                self._write_telemetry_row(
                    mode=cur_mode,
                    trigger=trigger,
                    sentences=len(in_flight),
                    input_chars=input_chars,
                    truncated=truncated,
                    prompt_ms=float(meta.get("prompt_ms", 0.0) or 0.0),
                    total_ms=float(meta.get("total_ms", 0.0) or 0.0),
                    preempt_count=turn_preempt_cnt,
                    ok=False,
                    error=err or "unknown",
                    new_points=0,
                    has_question=False,
                    gap_since_last_s=gap_since_last_s,
                )
                self.status_update.emit("")
                self._sleep_tick()
                continue

            contains_cjk_fn = getattr(self.engine, "_contains_cjk", ai_engine.LocalAIEngine._contains_cjk)
            validated = validate_summary(data, prev_notes_snapshot, contains_cjk_fn)
            if validated is None:
                self._write_telemetry_row(
                    mode=cur_mode,
                    trigger=trigger,
                    sentences=len(in_flight),
                    input_chars=input_chars,
                    truncated=truncated,
                    prompt_ms=float(meta.get("prompt_ms", 0.0) or 0.0),
                    total_ms=float(meta.get("total_ms", 0.0) or 0.0),
                    preempt_count=turn_preempt_cnt,
                    ok=False,
                    error="empty_after_validate",
                    new_points=0,
                    has_question=False,
                    gap_since_last_s=gap_since_last_s,
                )
                self.status_update.emit("")
                self._sleep_tick()
                continue

            with self._lock:
                self._last_successful_ts = now_after
                if validated["topic"]:
                    self._notes["topic"] = validated["topic"]
                new_pts = validated["new_points"]
                for pt in reversed(new_pts):
                    self._notes["points"].appendleft(pt)

                q_new = validated["question_to_listener"]
                if q_new:
                    self._notes["question"] = q_new
                    self._notes["question_ts"] = now_after
                elif self._notes["question"] and (now_after - float(self._notes["question_ts"])) >= 90.0:
                    self._notes["question"] = ""

                shown_points = list(self._notes["points"])[:max_points_shown]
                new_count = min(len(new_pts), len(shown_points))
                payload = {
                    "topic": self._notes["topic"],
                    "points": shown_points,
                    "new_count": new_count,
                    "question": self._notes["question"],
                    "trigger": trigger,
                    "mode": cur_mode,
                    "latency_ms": int(round(float(meta.get("total_ms", 0.0) or 0.0))),
                    "sentences": len(in_flight),
                    "truncated": bool(truncated),
                    "ts": time.time(),
                }

            self._write_telemetry_row(
                mode=cur_mode,
                trigger=trigger,
                sentences=len(in_flight),
                input_chars=input_chars,
                truncated=truncated,
                prompt_ms=float(meta.get("prompt_ms", 0.0) or 0.0),
                total_ms=float(meta.get("total_ms", 0.0) or 0.0),
                preempt_count=turn_preempt_cnt,
                ok=True,
                error="",
                new_points=len(new_pts),
                has_question=bool(payload["question"]),
                gap_since_last_s=gap_since_last_s,
            )
            self.status_update.emit("")
            self.summary_ready.emit(payload)
            self._sleep_tick()
