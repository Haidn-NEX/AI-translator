import dataclasses
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from PyQt6.QtCore import QThread, pyqtSignal

from ai_engine import LocalAIEngine, _CJK_RE, _MUSIC_TAG_RE
from incoming_pipeline import APP_DIR, _PURE_FILLERS

logger = logging.getLogger("LiveTranslator")

CAPTION_CACHE_DIR = os.path.join(APP_DIR, "caption_cache")
CAPTION_CACHE_SCHEMA_VER = 1
CAPTION_CACHE_MAX_FILES = 80

# Nhãn âm thanh nền nhúng bên trong câu phụ đề (VD: "[Music] Hello everyone" -> "Hello everyone")
_INLINE_NOISE_TAG_RE = re.compile(
    r"[\[\(\{]\s*(?:music|nhạc|applause|vỗ tay|laughter|cười|cheering|sigh|sighs|gasps|groans|"
    r"instrumental|singing|intro music|upbeat music|inaudible|speaking [^\[\]\(\)]+)\s*[\]\)\}]",
    re.IGNORECASE,
)
_INLINE_NOTES_RE = re.compile(r"[♪♫♬♩]+")
_SPEAKER_ARROW_RE = re.compile(r"^\s*(?:>>+|-\s*(?=[A-Z]))\s*")
_VTT_TAG_RE = re.compile(r"<[^>]+>")
_CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

_TERMINAL_PUNCT = (".", "!", "?", "。", "！", "？", "…")
_CLAUSE_PUNCT = (",", ";", ":", "，", "；", "：", "、")

_CLAUSE_CONJUNCTIONS = {
    "and", "but", "so", "because", "while", "although", "though",
    "when", "whenever", "if", "unless", "which", "where", "whereas",
    "however", "therefore", "then", "instead", "otherwise",
}

_DANGLING_TAIL_WORDS = {
    "the", "a", "an", "to", "of", "in", "on", "at", "for", "with", "by", "from",
    "into", "about", "as", "and", "but", "or", "so", "because", "if", "when",
    "while", "that", "which", "who", "whose", "is", "are", "was", "were", "be",
    "been", "have", "has", "had", "do", "does", "did", "will", "would", "can",
    "could", "should", "must", "my", "your", "his", "her", "our", "their", "its",
    "this", "these", "those", "very", "too", "just", "not",
}


@dataclasses.dataclass
class CaptionCue:
    cue_id: int
    start_ms: int
    end_ms: int
    src: str
    vi: str = ""
    engine: str = ""
    trans_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cue_id": self.cue_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "src": self.src,
            "vi": self.vi,
            "engine": self.engine,
            "trans_ms": round(float(self.trans_ms), 1),
        }


def is_vietnamese_track(lang: str | None) -> bool:
    raw = (lang or "").strip().lower()
    if raw.startswith("a."):
        raw = raw[2:]
    return raw == "vi" or raw.startswith("vi-") or raw.startswith("vi_")


def normalize_yt_lang(lang: str | None) -> str:
    raw = (lang or "").strip().lower()
    if not raw:
        return "en"
    if raw.startswith("a.") and len(raw) > 2:
        raw = raw[2:]
    base = raw.split("-")[0].split("_")[0]
    return base or "en"


def _clean_caption_text(raw: str, strip_arrows: bool = True) -> str:
    if not raw:
        return ""
    txt = html.unescape(html.unescape(raw))
    txt = txt.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    txt = _INLINE_NOISE_TAG_RE.sub(" ", txt)
    txt = _INLINE_NOTES_RE.sub(" ", txt)

    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) >= 2 and all(ln.startswith("-") for ln in lines):
        # Giữ ranh giới đối thoại 2 nhân vật trong cùng 1 cue thủ công
        parts = [ln.lstrip("-").strip() for ln in lines if ln.lstrip("-").strip()]
        txt = " — ".join(parts)
    else:
        txt = " ".join(lines)

    if strip_arrows:
        txt = _SPEAKER_ARROW_RE.sub("", txt)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    if not txt or _MUSIC_TAG_RE.match(txt):
        return ""

    alpha_tokens = re.findall(r"[a-zA-Z']+", txt.lower())
    if alpha_tokens and len(alpha_tokens) <= 2 and all(t in _PURE_FILLERS for t in alpha_tokens):
        if not _CJK_CHAR_RE.search(txt):
            return ""
    return txt


def _effective_unit_length(text: str) -> float:
    """Đo độ dài tương đương số từ tiếng Anh (hỗ trợ cả câu tiếng Nhật/Trung không có khoảng trắng)."""
    cjk_count = len(_CJK_CHAR_RE.findall(text))
    non_cjk = _CJK_CHAR_RE.sub(" ", text)
    space_words = len([w for w in non_cjk.split() if w.strip()])
    return space_words + (cjk_count * 0.55)


def _last_ascii_word_lower(text: str) -> str:
    m = re.search(r"([a-zA-Z']+)\W*$", (text or "").strip())
    return m.group(1).lower() if m else ""


def _first_ascii_word_lower(text: str) -> str:
    m = re.match(r"^\W*([a-zA-Z']+)", (text or "").strip())
    return m.group(1).lower() if m else ""


def _finalize_non_overlapping_cues(raw_cues: list[tuple[int, int, str]]) -> list[CaptionCue]:
    """Chuẩn hóa danh sách (start_ms, end_ms, src) sao cho mốc giờ tăng dần và không bao giờ chồng lấp."""
    filtered: list[list] = []
    for s_ms, e_ms, src in raw_cues:
        cleaned = _clean_caption_text(src)
        if not cleaned:
            continue
        start = max(0, int(s_ms))
        end = max(start + 350, int(e_ms))
        if filtered and start == filtered[-1][0] and cleaned == filtered[-1][2]:
            filtered[-1][1] = max(filtered[-1][1], end)
            continue
        filtered.append([start, end, cleaned])

    filtered.sort(key=lambda x: (x[0], x[1]))
    n = len(filtered)
    out: list[CaptionCue] = []
    for idx, (start, end, txt) in enumerate(filtered):
        if idx + 1 < n:
            next_start = filtered[idx + 1][0]
            if end > next_start:
                end = max(start + 200, next_start)
                if next_start > start:
                    end = min(end, next_start)
        if end <= start:
            end = start + 250
        out.append(CaptionCue(cue_id=idx, start_ms=start, end_ms=end, src=txt))
    return out


def _merge_short_manual_fragments(cues: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """
    Gộp các mảnh phụ đề thủ công bị cắt vụn giữa mệnh đề (VD: 2-5 từ không có dấu chấm cuối câu,
    khoảng nghỉ < 450ms) thành câu tự nhiên (<= 18 từ, <= 6.2s) để Ollama dịch đúng trật tự ngữ pháp.
    """
    if len(cues) <= 1:
        return cues

    merged: list[list] = []
    for s_ms, e_ms, raw_txt in cues:
        txt = _clean_caption_text(raw_txt)
        if not txt:
            continue
        if not merged:
            merged.append([s_ms, e_ms, txt])
            continue

        prev_s, prev_e, prev_txt = merged[-1]
        gap_ms = s_ms - prev_e
        prev_ends_sentence = prev_txt.endswith(_TERMINAL_PUNCT)
        has_dialogue_dash = (" — " in prev_txt) or (" — " in txt)
        combined_txt = f"{prev_txt} {txt}".strip()
        combined_len = _effective_unit_length(combined_txt)
        combined_dur = max(e_ms, prev_e) - prev_s

        prev_len = _effective_unit_length(prev_txt)
        cur_len = _effective_unit_length(txt)
        prev_tail = _last_ascii_word_lower(prev_txt)
        cur_head = _first_ascii_word_lower(txt)

        should_merge = (
            not prev_ends_sentence
            and not has_dialogue_dash
            and gap_ms <= 450
            and combined_len <= 18.0
            and combined_dur <= 6200
            and (
                prev_len <= 5.5
                or cur_len <= 4.0
                or prev_tail in _DANGLING_TAIL_WORDS
                or prev_txt.endswith(",")
                or cur_head in {"and", "but", "or", "so", "to", "that", "which", "because"}
            )
        )
        if should_merge:
            # Nối khoảng trắng chuẩn (không chèn khoảng trắng giữa 2 ký tự CJK liền nhau)
            if _CJK_CHAR_RE.search(prev_txt[-1:]) and _CJK_CHAR_RE.search(txt[:1]):
                merged[-1][2] = f"{prev_txt}{txt}"
            else:
                merged[-1][2] = combined_txt
            merged[-1][1] = max(prev_e, e_ms)
        else:
            merged.append([s_ms, e_ms, txt])

    return [(s, e, t) for s, e, t in merged]


def _reconstruct_asr_word_cues(word_tokens: list[tuple[int, int, str]]) -> list[CaptionCue]:
    """
    Tái tạo câu hoàn chỉnh từ luồng từ đơn có mốc tOffsetMs của YouTube ASR (khắc phục triệt để
    lỗi cửa sổ cuộn 2 dòng chồng lấn thời gian wWinId / aAppend của json3 ASR).
    """
    if not word_tokens:
        return []

    # Khử trùng lặp khi YouTube phát lại dòng cuộn cùng mốc giờ
    deduped: list[tuple[int, int, str]] = []
    for w_start, ev_end, raw_w in word_tokens:
        w = raw_w.replace("\n", " ").strip()
        w = _INLINE_NOISE_TAG_RE.sub("", w).strip()
        w = _INLINE_NOTES_RE.sub("", w).strip()
        if not w:
            continue
        if deduped and abs(w_start - deduped[-1][0]) <= 40 and w.lower() == deduped[-1][2].lower():
            continue
        deduped.append((max(0, int(w_start)), max(int(w_start) + 250, int(ev_end)), w))

    if not deduped:
        return []

    deduped.sort(key=lambda x: x[0])
    total = len(deduped)
    raw_cues: list[tuple[int, int, str]] = []

    buf_words: list[str] = []
    seg_start_ms = deduped[0][0]
    last_word_start_ms = deduped[0][0]
    last_ev_end_ms = deduped[0][1]

    def _join_words(words: list[str]) -> str:
        if not words:
            return ""
        out_parts = [words[0]]
        for tok in words[1:]:
            prev_ch = out_parts[-1][-1:]
            next_ch = tok[:1]
            if (
                (_CJK_CHAR_RE.search(prev_ch) and _CJK_CHAR_RE.search(next_ch))
                or tok in {".", ",", "!", "?", ";", ":", "'s", "n't", "'re", "'ve", "'ll", "'d", "'m"}
            ):
                out_parts.append(tok)
            else:
                out_parts.append(" " + tok)
        return "".join(out_parts).strip()

    for i, (w_start, ev_end, word) in enumerate(deduped):
        if not buf_words:
            seg_start_ms = w_start
        buf_words.append(word)
        last_word_start_ms = w_start
        last_ev_end_ms = ev_end

        if i == total - 1:
            est_end = min(last_ev_end_ms, last_word_start_ms + max(450, len(word) * 75))
            raw_cues.append((seg_start_ms, max(seg_start_ms + 500, est_end), _join_words(buf_words)))
            buf_words = []
            break

        next_start, _, next_word = deduped[i + 1]
        gap_ms = max(0, next_start - w_start)
        joined_now = _join_words(buf_words)
        cur_len = _effective_unit_length(joined_now)
        cur_dur = next_start - seg_start_ms

        tail_low = _last_ascii_word_lower(word)
        next_low = _first_ascii_word_lower(next_word)
        is_dangling_tail = tail_low in _DANGLING_TAIL_WORDS and cur_len < 19.0

        # 1. Điều kiện ngắt cứng (khoảng nghỉ dài, dấu kết câu rõ ràng, hoặc chạm trần độ dài)
        ends_terminal = word.endswith(_TERMINAL_PUNCT)
        hard_split = (
            gap_ms >= 1150
            or (ends_terminal and cur_len >= 3.5 and not word.lower().endswith(("mr.", "ms.", "dr.", "vs.", "u.s.")))
            or cur_len >= 20.0
            or cur_dur >= 6500
        )

        # 2. Điều kiện ngắt mệnh đề mềm khi câu đã đủ dài
        remaining = total - (i + 1)
        soft_split = (
            not is_dangling_tail
            and remaining >= 3
            and (cur_len >= 11.0 or cur_dur >= 3600)
            and (
                gap_ms >= 520
                or word.endswith(_CLAUSE_PUNCT)
                or (next_low in _CLAUSE_CONJUNCTIONS and cur_len >= 9.0)
            )
        )

        if hard_split or soft_split:
            est_word_dur = max(320, min(900, len(word) * 70))
            seg_end_ms = min(next_start, max(seg_start_ms + 450, w_start + est_word_dur))
            raw_cues.append((seg_start_ms, seg_end_ms, joined_now))
            buf_words = []

    return _finalize_non_overlapping_cues(raw_cues)


def _parse_json3(data: dict, is_asr: bool | None = None) -> list[CaptionCue]:
    events = data.get("events")
    if not isinstance(events, list) or not events:
        return []

    if is_asr is None:
        offset_segs = 0
        append_events = 0
        for ev in events[:40]:
            if not isinstance(ev, dict):
                continue
            if ev.get("aAppend"):
                append_events += 1
            for seg in ev.get("segs") or []:
                if isinstance(seg, dict) and seg.get("tOffsetMs"):
                    offset_segs += 1
        is_asr = (offset_segs >= 3) or (append_events >= 2)

    if is_asr:
        word_tokens: list[tuple[int, int, str]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            segs = ev.get("segs")
            if not isinstance(segs, list) or not segs:
                continue
            ev_start = int(ev.get("tStartMs", 0) or 0)
            ev_dur = int(ev.get("dDurationMs", 2500) or 2500)
            ev_end = ev_start + max(300, ev_dur)

            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                utf8 = seg.get("utf8")
                if not utf8 or not str(utf8).strip():
                    continue
                offset = int(seg.get("tOffsetMs", 0) or 0)
                w_start = ev_start + max(0, offset)
                # Nếu 1 seg chứa cụm nhiều từ cách nhau khoảng trắng, tách nhẹ theo bước 180ms
                raw_str = str(utf8).strip()
                parts = raw_str.split()
                if len(parts) > 1 and not _CJK_CHAR_RE.search(raw_str):
                    step = min(220, max(60, (ev_end - w_start) // max(1, len(parts))))
                    for p_idx, part in enumerate(parts):
                        word_tokens.append((w_start + p_idx * step, ev_end, part))
                else:
                    word_tokens.append((w_start, ev_end, raw_str))
        return _reconstruct_asr_word_cues(word_tokens)

    # Phụ đề thủ công (Manual json3)
    manual_raw: list[tuple[int, int, str]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        segs = ev.get("segs")
        if not isinstance(segs, list) or not segs:
            continue
        full_text = "".join(str(s.get("utf8") or "") for s in segs if isinstance(s, dict))
        if not full_text.strip():
            continue
        t_start = int(ev.get("tStartMs", 0) or 0)
        d_dur = int(ev.get("dDurationMs", 2500) or 2500)
        manual_raw.append((t_start, t_start + max(350, d_dur), full_text))

    stitched = _merge_short_manual_fragments(manual_raw)
    return _finalize_non_overlapping_cues(stitched)


def _parse_xml_timedtext(raw_xml: str, is_asr: bool | None = None) -> list[CaptionCue]:
    root = ET.fromstring(raw_xml)
    p_nodes = root.findall(".//p")
    if p_nodes:
        # Định dạng srv3 (<timedtext><body><p t="..." d="..."><s>...</s></p></body></timedtext>)
        has_word_s = any(len(p.findall("s")) > 1 for p in p_nodes[:25])
        if is_asr or has_word_s:
            word_tokens: list[tuple[int, int, str]] = []
            for p in p_nodes:
                t_start = int(float(p.attrib.get("t", "0") or 0))
                d_dur = int(float(p.attrib.get("d", "2500") or 2500))
                ev_end = t_start + max(300, d_dur)
                s_children = p.findall("s")
                if s_children:
                    for s_el in s_children:
                        txt = (s_el.text or "").strip()
                        if not txt:
                            continue
                        off = int(float(s_el.attrib.get("t", "0") or 0))
                        word_tokens.append((t_start + max(0, off), ev_end, txt))
                else:
                    txt = "".join(p.itertext()).strip()
                    if txt:
                        word_tokens.append((t_start, ev_end, txt))
            return _reconstruct_asr_word_cues(word_tokens)

        manual_raw: list[tuple[int, int, str]] = []
        for p in p_nodes:
            txt = "".join(p.itertext())
            if not txt.strip():
                continue
            t_start = int(float(p.attrib.get("t", "0") or 0))
            d_dur = int(float(p.attrib.get("d", "2500") or 2500))
            manual_raw.append((t_start, t_start + max(350, d_dur), txt))
        return _finalize_non_overlapping_cues(_merge_short_manual_fragments(manual_raw))

    # Định dạng srv1 (<transcript><text start="1.24" dur="3.1">...</text></transcript>)
    text_nodes = root.findall(".//text")
    raw_items: list[tuple[int, int, str]] = []
    for t_el in text_nodes:
        txt = "".join(t_el.itertext())
        if not txt.strip():
            continue
        s_ms = int(round(float(t_el.attrib.get("start", "0") or 0) * 1000))
        d_ms = int(round(float(t_el.attrib.get("dur", "2.5") or 2.5) * 1000))
        raw_items.append((s_ms, s_ms + max(350, d_ms), txt))
    return _finalize_non_overlapping_cues(_merge_short_manual_fragments(raw_items))


def _parse_vtt_timestamp_ms(ts: str) -> int:
    clean = ts.strip().split()[0].replace(",", ".")
    parts = clean.split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    elif len(parts) == 2:
        h, m, s = 0, int(parts[0]), float(parts[1])
    else:
        return 0
    return int(round((h * 3600 + m * 60 + s) * 1000))


def _parse_vtt(raw_vtt: str) -> list[CaptionCue]:
    blocks = re.split(r"\r?\n\r?\n+", (raw_vtt or "").strip())
    raw_items: list[tuple[int, int, str]] = []
    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        arrow_idx = -1
        for idx, ln in enumerate(lines):
            if "-->" in ln:
                arrow_idx = idx
                break
        if arrow_idx < 0 or arrow_idx + 1 >= len(lines):
            continue
        left, right = lines[arrow_idx].split("-->", 1)
        s_ms = _parse_vtt_timestamp_ms(left)
        e_ms = _parse_vtt_timestamp_ms(right)
        body = " ".join(_VTT_TAG_RE.sub("", ln) for ln in lines[arrow_idx + 1 :])
        if body.strip():
            raw_items.append((s_ms, max(s_ms + 350, e_ms), body))
    return _finalize_non_overlapping_cues(_merge_short_manual_fragments(raw_items))


def parse_youtube_timedtext(
    raw_text: str,
    fmt: str = "auto",
    is_asr: bool | None = None,
) -> list[CaptionCue]:
    """
    Phân tích gói phụ đề /api/timedtext của YouTube (json3, srv3/srv1 XML, hoặc WebVTT)
    thành danh sách CaptionCue chuẩn hóa, không chồng lấp mốc thời gian.
    """
    body = (raw_text or "").strip()
    if not body:
        return []

    fmt_low = (fmt or "auto").strip().lower()
    if fmt_low == "json3" or body.startswith("{"):
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return _parse_json3(data, is_asr=is_asr)
        except Exception as e:
            logger.warning(f"[Caption] Không parse được JSON3 timedtext: {e}")
            if fmt_low == "json3":
                return []

    if body.startswith("<") or fmt_low in ("srv3", "srv2", "srv1", "xml", "ttml"):
        try:
            return _parse_xml_timedtext(body, is_asr=is_asr)
        except Exception as e:
            logger.warning(f"[Caption] Không parse được XML timedtext: {e}")
            return []

    if "-->" in body or body.upper().startswith("WEBVTT") or fmt_low == "vtt":
        return _parse_vtt(body)

    return []


class CaptionTranslationCache:
    """Bộ nhớ đệm cục bộ lưu bản dịch phụ đề YouTube theo (video_id, lang, kind, mode, glossary_hash)."""

    def __init__(self, cache_dir: str | None = None, max_files: int = CAPTION_CACHE_MAX_FILES):
        self.cache_dir = cache_dir or CAPTION_CACHE_DIR
        self.max_files = max(10, int(max_files))

    @staticmethod
    def compute_glossary_hash(glossary_text: str) -> str:
        cleaned = (glossary_text or "").strip()
        if not cleaned:
            return "nogloss"
        return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]

    def _build_path(
        self,
        video_id: str,
        lang: str,
        is_asr: bool,
        translation_mode: str,
        glossary_text: str,
    ) -> str:
        safe_vid = re.sub(r"[^a-zA-Z0-9_-]", "_", (video_id or "unknown"))[:32]
        safe_lang = re.sub(r"[^a-zA-Z0-9_-]", "_", normalize_yt_lang(lang))[:12]
        kind_tag = "asr" if is_asr else "manual"
        mode_tag = "ollama" if translation_mode == "ollama" else "nllb"
        gloss_tag = self.compute_glossary_hash(glossary_text)
        fname = f"{safe_vid}_{safe_lang}_{kind_tag}_{mode_tag}_{gloss_tag}.json"
        return os.path.join(self.cache_dir, fname)

    def load_into_cues(
        self,
        video_id: str,
        lang: str,
        is_asr: bool,
        translation_mode: str,
        glossary_text: str,
        cues: list[CaptionCue],
    ) -> int:
        if not video_id or not cues:
            return 0
        path = self._build_path(video_id, lang, is_asr, translation_mode, glossary_text)
        if not os.path.isfile(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("schema_ver") != CAPTION_CACHE_SCHEMA_VER:
                return 0
            by_key: dict[tuple[int, str], tuple[str, str]] = {}
            for item in payload.get("cues") or []:
                if not isinstance(item, dict):
                    continue
                vi = str(item.get("vi") or "").strip()
                src = str(item.get("src") or "").strip()
                if not vi or not src or _CJK_RE.search(vi):
                    continue
                s_ms = int(item.get("start_ms", -1))
                eng = str(item.get("engine") or "Cache")
                by_key[(s_ms, src)] = (vi, eng)

            hits = 0
            for c in cues:
                hit = by_key.get((c.start_ms, c.src))
                if hit:
                    c.vi, c.engine = hit
                    c.trans_ms = 0.0
                    hits += 1
            if hits > 0:
                try:
                    os.utime(path, None)
                except Exception:
                    pass
            return hits
        except Exception as e:
            logger.debug(f"[Caption] Bỏ qua file cache lỗi {path}: {e}")
            return 0

    def save_cues(
        self,
        video_id: str,
        lang: str,
        is_asr: bool,
        translation_mode: str,
        glossary_text: str,
        cues: list[CaptionCue],
    ) -> None:
        if not video_id or not cues:
            return
        translated = [c.to_dict() for c in cues if c.vi]
        if not translated:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            path = self._build_path(video_id, lang, is_asr, translation_mode, glossary_text)
            tmp_path = f"{path}.{threading.get_ident()}.tmp"
            payload = {
                "schema_ver": CAPTION_CACHE_SCHEMA_VER,
                "video_id": video_id,
                "lang": normalize_yt_lang(lang),
                "is_asr": bool(is_asr),
                "translation_mode": translation_mode,
                "glossary_hash": self.compute_glossary_hash(glossary_text),
                "updated_at": time.time(),
                "cues": translated,
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, path)
            self._prune_lru()
        except Exception as e:
            logger.debug(f"[Caption] Không lưu được cache phụ đề: {e}")

    def _prune_lru(self) -> None:
        try:
            entries = [
                os.path.join(self.cache_dir, fn)
                for fn in os.listdir(self.cache_dir)
                if fn.endswith(".json")
            ]
            if len(entries) <= self.max_files:
                return
            entries.sort(key=lambda p: os.path.getmtime(p))
            for stale in entries[: len(entries) - self.max_files]:
                try:
                    os.remove(stale)
                except Exception:
                    pass
        except Exception:
            pass


class CaptionTranslationWorker(QThread):
    """
    Luồng dịch đón đầu theo cửa sổ trượt [playhead, playhead + 90s] cho phụ đề YouTube:
    - Không nạp/gọi Whisper STT (0% tải STT).
    - Câu sát playhead (<= 8.5s) đi đường ưu tiên cao (1 câu đơn, ~0.8s).
    - Các câu phía sau dịch theo batch 4 câu kèm ngữ cảnh trước + lookahead sau, tự nghỉ 0% GPU khi đã dịch đủ +90s.
    """

    track_initialized = pyqtSignal(dict)
    cues_ready = pyqtSignal(str, list)
    status_update = pyqtSignal(str)
    mode_synced = pyqtSignal(str)

    BASE_LOOKAHEAD_MS = 90_000
    BASE_URGENT_MS = 8_500
    BATCH_SIZE = 4

    def __init__(
        self,
        video_id: str,
        cues: list[CaptionCue],
        track_lang: str,
        is_asr: bool,
        engine: LocalAIEngine,
        initial_playhead_ms: int = 0,
        playback_rate: float = 1.0,
        cache: CaptionTranslationCache | None = None,
    ):
        super().__init__()
        self.video_id = str(video_id or "yt")
        self.cues: list[CaptionCue] = list(cues)
        self.track_lang = normalize_yt_lang(track_lang)
        self.is_asr = bool(is_asr)
        self.engine = engine
        self.cache = cache or CaptionTranslationCache()

        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._playhead_ms = max(0, int(initial_playhead_ms or 0))
        self._playback_rate = max(0.25, min(3.0, float(playback_rate or 1.0)))
        self._paused = False

        self.running = True
        self.is_loading_models = True
        self.is_translating_batch = False
        self._cache_dirty = False
        self._last_cache_save_ts = 0.0

    def update_playhead(
        self, playhead_ms: int, playback_rate: float = 1.0, paused: bool = False
    ) -> None:
        new_ms = max(0, int(playhead_ms or 0))
        new_rate = max(0.25, min(3.0, float(playback_rate or 1.0)))
        wake = False
        with self._lock:
            if abs(new_ms - self._playhead_ms) >= 1200 or abs(new_rate - self._playback_rate) >= 0.2:
                wake = True
            self._playhead_ms = new_ms
            self._playback_rate = new_rate
            self._paused = bool(paused)
            # Cũng đánh thức khi cửa sổ đón đầu còn dưới 50 giây
            horizon = new_ms + int(50_000 * max(1.0, min(2.0, new_rate)))
            for c in self.cues:
                if c.end_ms >= new_ms and c.start_ms <= horizon and not c.vi:
                    wake = True
                    break
        if wake:
            self._wake_event.set()

    def invalidate_future_translations(self) -> None:
        """Xóa các bản dịch chưa xem từ playhead trở đi khi người dùng đổi từ điển hoặc đổi chế độ dịch."""
        with self._lock:
            cutoff = self._playhead_ms + 1500
            for c in self.cues:
                if c.start_ms >= cutoff:
                    c.vi = ""
                    c.engine = ""
                    c.trans_ms = 0.0
        self._wake_event.set()

    def is_speaking_at(self, playhead_ms: int | None = None) -> bool:
        with self._lock:
            if self._paused:
                return False
            t_ms = self._playhead_ms if playhead_ms is None else int(playhead_ms)
            for c in self.cues:
                if c.start_ms - 150 <= t_ms <= c.end_ms + 250:
                    return True
                if c.start_ms > t_ms + 500:
                    break
        return False

    def is_idle(self) -> bool:
        if self.is_loading_models or self.is_translating_batch:
            return False
        with self._lock:
            urgent_limit = self._playhead_ms + int(
                self.BASE_URGENT_MS * max(1.0, min(2.0, self._playback_rate))
            )
            for c in self.cues:
                if c.end_ms >= self._playhead_ms and c.start_ms <= urgent_limit and not c.vi:
                    return False
        return True

    def get_cues_snapshot(self) -> list[dict]:
        with self._lock:
            return [c.to_dict() for c in self.cues]

    def _select_next_batch(self) -> tuple[list[int], bool]:
        """Chọn danh sách chỉ số cue cần dịch tiếp theo và cờ urgent."""
        with self._lock:
            playhead = self._playhead_ms
            rate = max(1.0, min(2.0, self._playback_rate))
            horizon_ms = playhead + int(self.BASE_LOOKAHEAD_MS * rate)
            urgent_ms = playhead + int(self.BASE_URGENT_MS * rate)

            first_idx = -1
            for idx, c in enumerate(self.cues):
                if c.end_ms >= playhead - 1200 and c.start_ms <= horizon_ms and not c.vi:
                    first_idx = idx
                    break

            if first_idx < 0:
                return [], False

            first_cue = self.cues[first_idx]
            is_urgent = first_cue.start_ms <= urgent_ms

            # Kiểm tra xem ngay tại playhead hiện tại đã có câu dịch sẵn để đọc chưa
            has_translated_at_playhead = any(
                (c.start_ms <= playhead + 1500 and c.end_ms >= playhead and bool(c.vi))
                for c in self.cues
            )
            if is_urgent and not has_translated_at_playhead:
                # Ưu tiên dịch 1 câu đơn ngay lập tức để hiện lên màn hình sau ~0.8s
                return [first_idx], True

            indices = [first_idx]
            cursor = first_idx + 1
            while cursor < len(self.cues) and len(indices) < self.BATCH_SIZE:
                cand = self.cues[cursor]
                if cand.vi or cand.start_ms > horizon_ms:
                    break
                indices.append(cursor)
                cursor += 1
            return indices, is_urgent

    def _flush_cache_if_needed(self, force: bool = False) -> None:
        if not self._cache_dirty:
            return
        now = time.monotonic()
        if not force and (now - self._last_cache_save_ts) < 12.0:
            return
        with self._lock:
            snapshot = [dataclasses.replace(c) for c in self.cues]
            mode = self.engine.translation_mode
            glossary = self.engine.custom_glossary
            self._cache_dirty = False
            self._last_cache_save_ts = now
        self.cache.save_cues(
            self.video_id, self.track_lang, self.is_asr, mode, glossary, snapshot
        )

    def run(self):
        self.is_loading_models = True
        kind_desc = "tự động ASR" if self.is_asr else "chuẩn"
        try:
            if self.engine.translation_mode == "ollama":
                self.status_update.emit(
                    f"⏳ Đang chuẩn bị Ollama ({self.engine.ollama_model}) cho phụ đề YouTube ({kind_desc})..."
                )
                self.engine.warmup_ollama()
            src_flores = self.engine._resolve_flores_lang(self.track_lang)
            self.engine._ensure_nllb_loaded(src_flores)
            self.engine._ensure_nllb_loaded("vie_Latn")
            self.mode_synced.emit(self.engine.translation_mode)
        except Exception as e:
            self.is_loading_models = False
            logger.exception("[Caption] Lỗi nạp model dịch cho Caption Mode")
            self.status_update.emit(f"❌ Lỗi nạp model dịch: {e}")
            return
        finally:
            self.is_loading_models = False

        # Nạp các câu đã có sẵn trong bộ nhớ đệm (0ms)
        with self._lock:
            cache_hits = self.cache.load_into_cues(
                self.video_id,
                self.track_lang,
                self.is_asr,
                self.engine.translation_mode,
                self.engine.custom_glossary,
                self.cues,
            )
            init_payload = {
                "video_id": self.video_id,
                "lang": self.track_lang,
                "is_asr": self.is_asr,
                "total_cues": len(self.cues),
                "cached_cues": cache_hits,
                "cues": [c.to_dict() for c in self.cues],
            }
        self.track_initialized.emit(init_payload)

        active_desc = (
            f"Ollama {self.engine.ollama_model}"
            if self.engine.translation_mode == "ollama"
            else "NLLB-200 INT8"
        )
        self.status_update.emit(
            f"✅ Đã bắt phụ đề gốc YouTube ({self.track_lang.upper()} · {kind_desc}, {len(self.cues)} câu) — {active_desc}"
        )

        announced_ahead_ready = False
        while self.running:
            indices, is_urgent = self._select_next_batch()
            if not indices:
                self._flush_cache_if_needed(force=False)
                if not announced_ahead_ready:
                    announced_ahead_ready = True
                    self.status_update.emit(
                        f"✅ Phụ đề gốc YT ({self.track_lang.upper()} · {kind_desc}) — Đã dịch đón đầu sẵn sàng!"
                    )
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()
                continue

            announced_ahead_ready = False
            with self._lock:
                batch_srcs = [self.cues[i].src for i in indices]
                prev_ctx: list[tuple[str, str]] = []
                start_prev = max(0, indices[0] - 2)
                for p_idx in range(start_prev, indices[0]):
                    pc = self.cues[p_idx]
                    if pc.src and pc.vi:
                        prev_ctx.append((pc.src, pc.vi))
                next_ctx: list[str] = []
                last_idx = indices[-1]
                for n_idx in range(last_idx + 1, min(len(self.cues), last_idx + 3)):
                    if self.cues[n_idx].src:
                        next_ctx.append(self.cues[n_idx].src)

            self.is_translating_batch = True
            try:
                vi_list, trans_ms, eng_label = self.engine.translate_caption_batch(
                    batch_srcs,
                    prev_context=prev_ctx,
                    next_context=next_ctx,
                    source_lang=self.track_lang,
                    urgent=is_urgent,
                )
                if not self.running:
                    break
                per_cue_ms = trans_ms / max(1, len(indices))
                updated_dicts: list[dict] = []
                with self._lock:
                    for i, vi_text in zip(indices, vi_list):
                        c = self.cues[i]
                        # Kiểm tra tránh ghi đè rỗng nếu đang dừng
                        if vi_text:
                            c.vi = vi_text
                            c.engine = eng_label
                            c.trans_ms = per_cue_ms
                            updated_dicts.append(c.to_dict())
                    if updated_dicts:
                        self._cache_dirty = True

                self.mode_synced.emit(self.engine.translation_mode)
                if updated_dicts:
                    self.cues_ready.emit(self.video_id, updated_dicts)
            except Exception as e:
                logger.exception("[Caption] Lỗi dịch batch phụ đề YouTube")
                self.status_update.emit(f"⚠️ Lỗi dịch phụ đề: {e}")
                time.sleep(0.5)
            finally:
                self.is_translating_batch = False

        self._flush_cache_if_needed(force=True)

    def stop(self):
        self.running = False
        self._wake_event.set()
