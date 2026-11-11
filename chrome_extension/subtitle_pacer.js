function splitWords(text) {
  const s = (text || "").trim();
  return s ? s.split(/\s+/) : [];
}

function isUpperChar(ch) {
  return Boolean(ch) && ch === ch.toUpperCase() && ch !== ch.toLowerCase();
}

function isLowerChar(ch) {
  return Boolean(ch) && ch === ch.toLowerCase() && ch !== ch.toUpperCase();
}

function isAllUpper(str) {
  if (!str) return false;
  const chars = Array.from(str);
  let hasCased = false;
  for (const c of chars) {
    if (c !== c.toLowerCase() || c !== c.toUpperCase()) {
      hasCased = true;
      if (c !== c.toUpperCase()) return false;
    }
  }
  return hasCased;
}

function endsWithAny(str, suffixes) {
  return suffixes.some((sfx) => str.endsWith(sfx));
}

function startsWithAny(str, prefixes) {
  return prefixes.some((pfx) => str.startsWith(pfx));
}

export class SubtitlePacer {
  constructor({ onRender = null, onHud = null, nowFn = null } = {}) {
    this.onRender = onRender;
    this.onHud = onHud;
    this._nowFn = nowFn || (() => performance.now() / 1000.0);

    this._current_src = "";
    this._current_vi = "";
    this._prev_vi = "";
    this._last_update_ts = 0.0;
    this._line_shown_ts = 0.0;
    this._show_bilingual = true;
    this._display_queue = [];

    this._timerId = null;
    this._timerDeadlineMs = 0;
  }

  static _format_prev_line(prev_vi) {
    const text = (prev_vi || "").trim();
    if (!text) {
      return "";
    }
    if (text.length <= 135) {
      return `⤴ ${text}`;
    }
    let tail = text.slice(-128);
    const space_idx = tail.indexOf(" ");
    if (space_idx > 0 && space_idx < 25) {
      tail = tail.slice(space_idx + 1);
    }
    return `⤴ …${tail}`;
  }

  static _merge_text_cleanly(left, right, src_right = "") {
    const l_raw = (left || "").trim();
    const r_raw = (right || "").trim();
    if (!l_raw) return r_raw;
    if (!r_raw) return l_raw;

    const lastChar = l_raw.slice(-1);
    const left_open =
      endsWithAny(l_raw, ["...", "…", "-", "—"]) || ![".", "!", "?"].includes(lastChar);
    const right_cont = startsWithAny(r_raw, ["...", "…"]);
    const l_clean = l_raw.replace(/(?:\.{2,}|…|\s*-\s*)$/, "").trimEnd();
    let r_clean = r_raw.replace(/^(?:\.{2,}|…)\s*/, "").trimStart();

    if (
      left_open &&
      r_clean &&
      r_clean.length >= 2 &&
      isUpperChar(r_clean[0]) &&
      !isAllUpper(r_clean.slice(0, 2))
    ) {
      const src_r_clean = (src_right || "").trim().replace(/^(?:\.{2,}|…)\s*/, "");
      if (right_cont || (src_r_clean && isLowerChar(src_r_clean[0]))) {
        r_clean = r_clean[0].toLowerCase() + r_clean.slice(1);
      }
    }

    return `${l_clean} ${r_clean}`.replace(/\s{2,}/g, " ").trim();
  }

  static _is_syntactic_continuation(cur_src, cur_vi, new_src, new_vi) {
    const cs = (cur_src || "").trimEnd();
    const cv = (cur_vi || "").trimEnd();
    const ns = (new_src || "").trimStart();
    const nv = (new_vi || "").trimStart();

    if (
      endsWithAny(cs, ["...", "…", ",", "-", "—"]) ||
      endsWithAny(cv, ["...", "…", ",", "-", "—"])
    ) {
      return true;
    }
    if (startsWithAny(ns, ["...", "…"]) || startsWithAny(nv, ["...", "…"])) {
      return true;
    }
    if (cs && ![".", "!", "?"].includes(cs.slice(-1))) {
      return true;
    }
    return false;
  }

  _should_merge_with_current(src_text, vi_text, elapsed) {
    if (!this._current_vi || this._display_queue.length > 0) {
      return false;
    }
    const cur_words = splitWords(this._current_vi).length;
    const new_words = splitWords(vi_text).length;
    if (cur_words + new_words > 22) {
      return false;
    }

    if (
      elapsed < 4.2 &&
      SubtitlePacer._is_syntactic_continuation(
        this._current_src,
        this._current_vi,
        src_text,
        vi_text
      )
    ) {
      return true;
    }

    if (new_words <= 4 && elapsed < 2.6 && cur_words + new_words <= 18) {
      return true;
    }
    if (cur_words <= 4 && new_words <= 9 && elapsed < 2.0) {
      return true;
    }
    return false;
  }

  _calculate_min_dwell_sec() {
    const q_len = this._display_queue.length;
    if (q_len >= 2) {
      return 0.95;
    }
    const words = splitWords(this._current_vi).length;
    const dwell = Math.min(3.1, Math.max(1.45, 0.95 + words * 0.13));
    if (q_len === 1) {
      return Math.min(dwell, 2.15);
    }
    return dwell;
  }

  _stop_timer() {
    if (this._timerId !== null) {
      clearTimeout(this._timerId);
      this._timerId = null;
      this._timerDeadlineMs = 0;
    }
  }

  _is_timer_active() {
    return this._timerId !== null;
  }

  _remaining_time_ms() {
    if (this._timerId === null) return 0;
    return Math.max(0, Math.round(this._timerDeadlineMs - this._nowFn() * 1000.0));
  }

  _start_timer(wait_ms) {
    this._stop_timer();
    const ms = Math.max(0, Math.round(wait_ms));
    this._timerDeadlineMs = this._nowFn() * 1000.0 + ms;
    this._timerId = setTimeout(() => {
      this._timerId = null;
      this._timerDeadlineMs = 0;
      this._on_pacer_tick();
    }, ms);
  }

  _emit_render() {
    if (typeof this.onRender === "function") {
      this.onRender({
        prev: this._prev_vi ? SubtitlePacer._format_prev_line(this._prev_vi) : "",
        src: this._show_bilingual ? this._current_src : "",
        rawSrc: this._current_src,
        vi: this._current_vi,
        showBilingual: this._show_bilingual
      });
    }
  }

  _emit_hud(text) {
    if (typeof this.onHud === "function") {
      this.onHud(text);
    }
  }

  _roll_and_render(src_text, vi_text, now) {
    if (this._current_vi) {
      this._prev_vi = this._current_vi;
    }
    this._current_src = src_text;
    this._current_vi = vi_text;
    this._line_shown_ts = now;
    this._last_update_ts = now;
    this._emit_render();
  }

  _on_pacer_tick() {
    if (this._display_queue.length === 0) {
      return;
    }
    const now = this._nowFn();
    let [next_src, next_vi] = this._display_queue.shift();

    if (this._display_queue.length > 0) {
      const [peek_src, peek_vi] = this._display_queue[0];
      if (
        splitWords(next_vi).length + splitWords(peek_vi).length <= 22 &&
        (SubtitlePacer._is_syntactic_continuation(next_src, next_vi, peek_src, peek_vi) ||
          splitWords(peek_vi).length <= 3)
      ) {
        this._display_queue.shift();
        next_src = SubtitlePacer._merge_text_cleanly(next_src, peek_src, peek_src);
        next_vi = SubtitlePacer._merge_text_cleanly(next_vi, peek_vi, peek_src);
      }
    }

    this._roll_and_render(next_src, next_vi, now);
    if (this._display_queue.length > 0) {
      const wait_ms = Math.round(Math.max(120.0, this._calculate_min_dwell_sec() * 1000.0));
      this._start_timer(wait_ms);
    }
  }

  show_incoming_source(src_text) {
    const clean = (src_text || "").trim();
    if (!clean) return;
    if (!this._current_vi && this._display_queue.length === 0) {
      this._current_src = clean;
      this._emit_render();
      this._emit_hud("⚡ Đã nhận diện xong, đang dịch sang tiếng Việt...");
    } else {
      const preview = clean.length <= 75 ? clean : `${clean.slice(0, 72)}...`;
      this._emit_hud(`⚡ Đang dịch tiếp: "${preview}"`);
    }
  }

  update_incoming_subtitle(src_text, vi_text, stt_ms = 0, trans_ms = 0, engine_tag = "AI") {
    const src = (src_text || "").trim();
    const vi = (vi_text || "").trim();
    if (!vi) return;

    const total_ms = Math.round(Number(stt_ms) + Number(trans_ms));
    this._emit_hud(
      `[Chiều Nghe - ${engine_tag}] STT: ${Math.round(Number(stt_ms))} ms | Dịch: ${Math.round(Number(trans_ms))} ms | Tổng: ${total_ms} ms`
    );

    const now = this._nowFn();
    const elapsed = this._last_update_ts > 0 ? now - this._last_update_ts : 999.0;

    // 1. Chưa có câu nào trên màn hình -> hiển thị ngay lập tức
    if (!this._current_vi) {
      this._roll_and_render(src, vi, now);
      return;
    }

    // 2. Mảnh câu nối tiếp ngữ pháp hoặc mảnh cực ngắn -> ghép mượt tại chỗ
    if (this._should_merge_with_current(src, vi, elapsed)) {
      this._current_src = SubtitlePacer._merge_text_cleanly(this._current_src, src, src);
      this._current_vi = SubtitlePacer._merge_text_cleanly(this._current_vi, vi, src);
      this._last_update_ts = now;
      const extra_sec = 0.35 + splitWords(vi).length * 0.12;
      this._line_shown_ts = Math.min(now, this._line_shown_ts + extra_sec);
      this._emit_render();
      return;
    }

    // 3. Đang có hàng đợi điều nhịp hoặc câu hiện tại chưa đủ thời gian đọc tối thiểu
    const dwell_elapsed = now - this._line_shown_ts;
    const req_dwell = this._calculate_min_dwell_sec();
    if (this._display_queue.length > 0 || dwell_elapsed < req_dwell) {
      this._display_queue.push([src, vi]);
      if (this._display_queue.length > 2) {
        this._stop_timer();
        this._on_pacer_tick();
        return;
      }
      const rem_sec = Math.max(0.08, this._calculate_min_dwell_sec() - (now - this._line_shown_ts));
      const rem_ms = Math.round(rem_sec * 1000.0);
      if (!this._is_timer_active()) {
        this._start_timer(rem_ms);
      } else {
        this._start_timer(Math.min(this._remaining_time_ms(), rem_ms));
      }
      return;
    }

    // 4. Đã đủ thời gian đọc -> cuộn mượt câu cũ lên dòng lịch sử và hiện câu mới ngay
    this._roll_and_render(src, vi, now);
  }

  set_show_source_subtitle(show_src) {
    this._show_bilingual = Boolean(show_src);
    this._emit_render();
  }

  reset_subtitles() {
    this._stop_timer();
    this._display_queue = [];
    this._current_src = "";
    this._current_vi = "";
    this._prev_vi = "";
    this._last_update_ts = 0.0;
    this._line_shown_ts = 0.0;
    this._emit_render();
  }
}

if (typeof window !== "undefined") {
  window.SubtitlePacer = SubtitlePacer;
}

