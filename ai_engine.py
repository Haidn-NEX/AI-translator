import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
import numpy as np
import requests
from hardware_profile import SystemProfile

logger = logging.getLogger("LiveTranslator")

OLLAMA_NUM_CTX = 1024

NLLB_LANG_MAP = {
    "en": "eng_Latn",
    "auto": "eng_Latn",
    "ja": "jpn_Jpan",
    "zh": "zho_Hans",
    "ko": "kor_Hang",
    "vi": "vie_Latn",
}

# Biểu thức phát hiện ký tự chữ Hán / CJK / Kana / Hangul / Cyrillic / Thái / Ả Rập bị rò rỉ khi Qwen2.5 dịch sang tiếng Việt
_CJK_RE = re.compile(
    r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f"
    r"\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)

# Biểu thức nhận diện các đoạn chỉ chứa ký hiệu nhạc nền / hiệu ứng âm thanh không lời của Whisper
_MUSIC_TAG_RE = re.compile(
    r"^[\s\[\(\*♪♫♬♩]*(?:music|nhạc|instrumental|singing|applause|laughter|sigh|cheering)?[\s\]\)\*♪♫♬♩\.\!\?,-]*$",
    re.IGNORECASE,
)

# Các câu ảo giác (hallucination) điển hình của Whisper trên đoạn nhạc không lời / nhiễu / tiếng thở
_ALWAYS_HALLUCINATIONS = {
    "thanks for watching",
    "thank you for watching",
    "see you next week",
    "see you next time",
    "subtitles by",
    "amaraorg",
    "please subscribe",
    "like and subscribe",
}
_WHISPER_HALLUCINATIONS = _ALWAYS_HALLUCINATIONS | {
    "thank you",
    "thank you very much",
    "bye",
    "bye bye",
    "goodbye",
    "you",
    "amen",
}

# Biểu thức kiểm tra ký tự có dấu tiếng Việt
_VI_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
    re.IGNORECASE,
)

# Tập từ chức năng và từ hội thoại tiếng Anh phổ biến (loại trừ các âm tiết trùng tiếng Việt không dấu như 'can', 'than')
_EN_STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "have", "has", "had", "not",
    "are", "was", "were", "been", "being", "will", "would", "could", "should",
    "about", "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "too", "very",
    "just", "now", "your", "you", "they", "them", "their", "what",
    "which", "who", "whom", "whose", "because", "while", "although", "though",
    "seems", "might", "confusion", "message", "source", "snippet", "provided",
    "appears", "unrelated", "discuss", "elaborate", "happy", "thanks", "thank",
    "please", "welcome", "everyone", "everybody", "instructor", "different",
    "working", "learning", "students", "university", "career", "really", "actually",
}

# Các thuật ngữ công nghệ thông dụng được phép giữ nguyên dạng tiếng Anh trong câu tiếng Việt hội thảo IT/AI
_ALLOWED_ASCII_LOANWORDS = {
    "website", "websites", "internet", "online", "offline", "laptop", "laptops",
    "video", "videos", "email", "emails", "podcast", "podcasts", "livestream",
    "marketing", "blockchain", "startup", "startups", "workshop", "workshops",
    "webinar", "panel", "mentor", "mentors", "mentorship", "intern", "interns",
    "coding", "bootcamp", "bootcamps", "profile", "platform", "blog", "vlog",
    "feedback", "deadline", "software", "hardware", "chatbot", "prompt", "prompts",
    "token", "tokens", "server", "cloud", "dataset", "datasets", "sprint",
}

# Tách đoạn nhiều câu thành danh sách câu đơn cho NLLB-200 (tránh cắt nhầm từ viết tắt Mr., Dr., vs., etc., U.S.)
_NMT_SENT_SPLIT_RE = re.compile(
    r"(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bvs)(?<!\bMrs)(?<!\betc)(?<!\.[A-Z])(?<=[\.\!\?])\s+"
)


class LocalAIEngine:
    _OLLAMA_COOLDOWN_SEC = 30.0

    def __init__(self, profile: SystemProfile):
        self.profile = profile
        self.whisper_model = None
        self._loaded_stt_name = None
        self.stt_model_name = profile.recommended_whisper
        self.ollama_model = profile.recommended_llm
        self.ollama_url = "http://127.0.0.1:11434/api/generate"
        self.ollama_chat_url = "http://127.0.0.1:11434/api/chat"
        self.source_lang = "en"           # Ngôn ngữ chiều nghe: "auto", "en", "ja", "zh", "ko"
        self._ollama_vulkan_retried = False
        self._http = requests.Session()
        self._llm_lock = threading.Lock()
        self._llm_preempt = threading.Event()
        self._summary_resp_lock = threading.Lock()
        self._active_summary_resp = None
        self._active_summary_conn = None
        self._summary_stream_aborted = False
        self._http_summary = self._create_summary_session()
        self.last_translate_preempted_summary = False

        # Tự động kiểm tra Ollama đang chạy hoặc đã cài sẵn model trên ổ cứng
        self.ollama_available = (
            self.check_ollama_running(self.ollama_model)
            or (self._find_ollama_exe() is not None and self._has_local_ollama_model(self.ollama_model))
        )
        self.translation_mode = "ollama" if self.ollama_available else "nllb_ct2"
        self._user_preferred_mode = self.translation_mode
        self._ollama_cooldown_until = 0.0
        self._ollama_fail_streak = 0
        self._ollama_recovering = False

        # Khóa đồng bộ GPU (RLock chống deadlock khi gọi lồng nhau trên cùng 1 luồng)
        self.gpu_lock = threading.RLock()

        # Lưu 4 câu gần nhất cho chiều nghe (ngữ cảnh dịch thuật) & 2 câu gần nhất cho từ vựng Whisper STT
        self.context_history = deque(maxlen=4)
        self.stt_prompt_history = deque(maxlen=2)

        # Từ điển thuật ngữ dự án / tên riêng tùy chỉnh cho họp công việc
        self.custom_glossary = ""
        self._glossary_stt_hint = ""
        self._glossary_ascii_terms: set[str] = set()

        # Biến cho NLLB-200 CTranslate2 INT8
        self.nllb_translator = None
        self._nllb_model_dir = None
        self.nllb_tokenizers = {}
        self.nllb_sp = None

    def set_translation_mode(self, mode: str):
        """Đặt chế độ dịch thuật do người dùng chủ động chọn từ giao diện"""
        self.translation_mode = mode
        self._user_preferred_mode = mode
        if mode == "ollama":
            self.ollama_available = True
            self._ollama_cooldown_until = 0.0
            self._ollama_fail_streak = 0

    @staticmethod
    def _collapse_repetitions(text: str) -> str:
        """Thu gọn các cụm từ bị lặp vô hạn (Whisper decoder loop) và mệnh đề lặp đôi mà không làm mất phần đuôi mới"""
        if not text:
            return ""
        cleaned = text.strip()

        # 1. Thu gọn cụm 1..10 từ lặp liên tiếp >= 3 lần, đồng thời gọt luôn mảnh tiền tố bị cắt cụt ở cuối vòng lặp
        loop_re = re.compile(
            r"\b(\w+(?:['’]\w+)?(?:\s+\w+(?:['’]\w+)?){0,9})(?:[\s,;\.]+?\1\b){2,}",
            flags=re.IGNORECASE,
        )
        while True:
            m = loop_re.search(cleaned)
            if not m:
                break
            phrase = m.group(1)
            tail = cleaned[m.end():]
            tail_words = tail.strip(" ,;.-").split()
            phrase_words = phrase.split()
            drop_tail = False
            if 0 < len(tail_words) <= len(phrase_words):
                p_norm = [re.sub(r"[^\w]", "", w.lower()) for w in phrase_words[: len(tail_words)]]
                t_norm = [re.sub(r"[^\w]", "", w.lower()) for w in tail_words]
                is_strict_subprefix = len(tail_words) < len(phrase_words) or len(t_norm[-1]) < len(p_norm[-1])
                if (
                    is_strict_subprefix
                    and bool(t_norm[-1])
                    and p_norm[:-1] == t_norm[:-1]
                    and p_norm[-1].startswith(t_norm[-1])
                ):
                    drop_tail = True
            cleaned = cleaned[: m.start()] + phrase + ("" if drop_tail else tail)

        # 2. Cửa sổ trượt phát hiện mọi cụm 2..30 từ lặp nguyên văn 2 lần liên tiếp ở BẤT KỲ vị trí nào trong câu
        #    (Bảo toàn cả từ dẫn phía trước lẫn vế mới phía sau; không gộp 1 từ đơn lặp 2 lần để giữ từ láy 'luôn luôn', 'từ từ')
        words = cleaned.split()
        if len(words) >= 4:
            norm_words = [re.sub(r"[^\w]", "", w.lower()) for w in words]
            out_words = []
            i = 0
            n = len(words)
            while i < n:
                max_span = min(30, (n - i) // 2)
                matched_span = 0
                for span in range(max_span, 1, -1):
                    w1 = norm_words[i : i + span]
                    w2 = norm_words[i + span : i + 2 * span]
                    if all(w1) and w1 == w2:
                        matched_span = span
                        break
                if matched_span >= 2:
                    kept = list(words[i : i + matched_span])
                    second = words[i + matched_span : i + 2 * matched_span]
                    last_tok = kept[-1].rstrip(",;-")
                    if second[-1][-1:] in ".!?" and last_tok[-1:] not in ".!?":
                        kept[-1] = last_tok + second[-1][-1]
                    elif kept[-1][-1:] in ",;-":
                        kept[-1] = last_tok
                    out_words.extend(kept)
                    i += 2 * matched_span
                    while i + matched_span <= n and norm_words[i : i + matched_span] == norm_words[i - matched_span : i]:
                        i += matched_span
                else:
                    out_words.append(words[i])
                    i += 1
            cleaned = " ".join(out_words)

        return cleaned.strip()

    def set_custom_glossary(self, glossary_text: str):
        """Cập nhật từ điển thuật ngữ cuộc họp (mồi từ vựng cho cả Whisper STT và Ollama LLM)"""
        self.custom_glossary = (glossary_text or "").strip()[:300]
        terms = []
        ascii_terms = set()
        for raw_item in re.split(r"[,;\n]+", self.custom_glossary):
            item = raw_item.strip()
            if not item:
                continue
            for sep in ("->", "=>", "="):
                if sep in item:
                    left, right = item.split(sep, 1)
                    if left.strip():
                        terms.append(left.strip())
                    if right.strip() and right.strip().lower() != left.strip().lower():
                        terms.append(right.strip())
                    break
            else:
                terms.append(item)
        for term in terms:
            for tok in re.findall(r"[a-zA-Z]+", term.lower()):
                if len(tok) >= 3:
                    ascii_terms.add(tok)
        self._glossary_ascii_terms = ascii_terms
        self._glossary_stt_hint = ", ".join(terms)[:160].strip()

    def reset_context(self):
        """Làm mới bộ nhớ ngữ cảnh khi bắt đầu phiên nghe mới hoặc đổi ngôn ngữ"""
        self.context_history.clear()
        self.stt_prompt_history.clear()

    @staticmethod
    def _find_ollama_exe() -> str | None:
        found = shutil.which("ollama")
        if found:
            return found
        if platform.system() == "Windows":
            candidate = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
            if os.path.isfile(candidate):
                return candidate
        return None

    @staticmethod
    def _has_local_ollama_model(model_name: str) -> bool:
        if not model_name:
            return False
        parts = model_name.split(":", 1)
        repo = parts[0]
        tag = parts[1] if len(parts) > 1 else "latest"
        manifest_path = os.path.join(
            os.path.expanduser("~"), ".ollama", "models", "manifests", "registry.ollama.ai", "library", repo, tag
        )
        return os.path.isfile(manifest_path)

    @staticmethod
    def check_ollama_running(model_name: str = None, timeout: float = 0.5) -> bool:
        try:
            resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=timeout)
            if resp.status_code != 200:
                return False
            if not model_name:
                return True
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            return any(m == model_name or m.startswith(f"{model_name}:") for m in models)
        except Exception:
            return False

    def _start_or_restart_ollama(self, use_vulkan: bool = False) -> bool:
        """
        Khởi động (hoặc khởi động lại sang chế độ GPU Vulkan khi gặp lỗi xung đột CUDA PTX trên GTX 1070)
        tiến trình ngầm ollama serve mà KHÔNG ảnh hưởng tới biến môi trường CUDA của Faster-Whisper.
        """
        ollama_exe = self._find_ollama_exe()
        if not ollama_exe:
            return False

        create_flags = 0x08000000 if platform.system() == "Windows" else 0
        try:
            if use_vulkan and platform.system() == "Windows":
                for proc_name in ("ollama app.exe", "ollama.exe"):
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/IM", proc_name],
                            creationflags=create_flags,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4,
                        )
                    except Exception:
                        pass
                time.sleep(0.8)

            child_env = os.environ.copy()
            if use_vulkan:
                # Ẩn CUDA chỉ riêng với tiến trình con của Ollama để Ollama dùng GPU GTX 1070 qua Vulkan
                child_env["OLLAMA_VULKAN"] = "1"
                child_env["CUDA_VISIBLE_DEVICES"] = "-1"

            mode_label = "GPU Vulkan" if use_vulkan else "mặc định"
            logger.info(f"Đang khởi động dịch vụ ngầm Ollama ({mode_label})...")
            subprocess.Popen(
                [ollama_exe, "serve"],
                env=child_env,
                creationflags=create_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            for _ in range(15):
                if self.check_ollama_running(self.ollama_model, timeout=1.0):
                    return True
                time.sleep(0.8)
        except Exception as e:
            logger.warning(f"Không thể khởi động lại dịch vụ Ollama ({e}).")
        return False

    def _probe_ollama_inference(self) -> None:
        resp = self._http.post(
            self.ollama_chat_url,
            json={
                "model": self.ollama_model,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "num_predict": 1,
                    "num_ctx": OLLAMA_NUM_CTX,
                },
            },
            timeout=35,
        )
        if resp.status_code != 200:
            err_detail = resp.text[:240]
            raise RuntimeError(f"HTTP {resp.status_code}: {err_detail}")

    def warmup_ollama(self):
        """Nạp sẵn model LLM lên VRAM ngay khi bật chiều nghe và tự sửa lỗi CUDA PTX sang Vulkan nếu cần"""
        try:
            prefer_vulkan_first = ("SM 6." in self.profile.device_name and platform.system() == "Windows")
            if not self.check_ollama_running(self.ollama_model, timeout=1.0):
                self._start_or_restart_ollama(use_vulkan=prefer_vulkan_first)
                if prefer_vulkan_first:
                    self._ollama_vulkan_retried = True

            logger.info(f"Đang nạp sẵn model Ollama '{self.ollama_model}' lên VRAM...")
            try:
                self._probe_ollama_inference()
            except Exception as first_err:
                if platform.system() == "Windows" and not self._ollama_vulkan_retried:
                    self._ollama_vulkan_retried = True
                    logger.warning(
                        f"Ollama gặp lỗi CUDA ({first_err}). Đang tự động chuyển Ollama sang backend GPU Vulkan..."
                    )
                    if self._start_or_restart_ollama(use_vulkan=True):
                        self._probe_ollama_inference()
                    else:
                        raise
                else:
                    raise

            self.ollama_available = True
            self._ollama_fail_streak = 0
            self._ollama_cooldown_until = 0.0
            logger.info(f"Đã nạp sẵn Ollama '{self.ollama_model}' thành công.")
        except Exception as e:
            logger.warning(f"Không thể warmup Ollama ({e}), tự động chuyển sang NLLB-200 INT8.")
            self.ollama_available = False
            self.translation_mode = "nllb_ct2"
            self._ollama_cooldown_until = time.monotonic() + 60.0

    def _trigger_async_ollama_recovery(self):
        """Khởi động luồng ngầm tự hồi sinh dịch vụ Ollama mà KHÔNG làm khựng dòng phụ đề trực tiếp"""
        if self._ollama_recovering or self._find_ollama_exe() is None:
            return
        self._ollama_recovering = True

        def _healer():
            try:
                prefer_vulkan = (
                    ("SM 6." in self.profile.device_name and platform.system() == "Windows")
                    or self._ollama_vulkan_retried
                )
                need_restart = (
                    not self.check_ollama_running(self.ollama_model, timeout=1.0)
                    or self._ollama_fail_streak >= 2
                )
                if need_restart:
                    logger.info("Đang tự động hồi sinh tiến trình ngầm Ollama ở hậu trường...")
                    self._start_or_restart_ollama(use_vulkan=prefer_vulkan)
                    if prefer_vulkan:
                        self._ollama_vulkan_retried = True
                self._probe_ollama_inference()
                self.ollama_available = True
                self._ollama_fail_streak = 0
                self._ollama_cooldown_until = 0.0
                if self._user_preferred_mode == "ollama":
                    self.translation_mode = "ollama"
                logger.info(f"Đã hồi phục thành công Ollama '{self.ollama_model}' ở chế độ nền.")
            except Exception as err:
                logger.warning(f"Chưa thể hồi phục ngầm Ollama ({err}), tiếp tục dùng NLLB-200 thêm 60s.")
                self._ollama_cooldown_until = time.monotonic() + 60.0
            finally:
                self._ollama_recovering = False

        threading.Thread(target=_healer, daemon=True, name="OllamaHealer").start()

    def _handle_ollama_failure(self, err_msg: str):
        self._ollama_fail_streak += 1
        self._ollama_cooldown_until = time.monotonic() + self._OLLAMA_COOLDOWN_SEC
        self.translation_mode = "nllb_ct2"
        logger.warning(
            f"{err_msg} -> Chuyển tạm thời sang NLLB-200 INT8 (cooldown {self._OLLAMA_COOLDOWN_SEC:.0f}s, "
            f"streak={self._ollama_fail_streak})."
        )
        if self._ollama_fail_streak >= 2 or not self.check_ollama_running(timeout=0.35):
            self._trigger_async_ollama_recovery()

    @staticmethod
    def _resolve_hf_repo_fast(repo_id: str) -> str:
        """Ưu tiên lấy ngay đường dẫn snapshot đã có sẵn trong ổ cứng (0ms mạng), chỉ tải online nếu chưa có"""
        if "/" not in repo_id:
            return repo_id
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(repo_id, local_files_only=True, ignore_patterns=["*.md", ".gitattributes"])
        except Exception:
            return snapshot_download(repo_id, ignore_patterns=["*.md", ".gitattributes"])

    def load_stt_model(self, model_name: str = None):
        with self.gpu_lock:
            if model_name:
                self.stt_model_name = model_name
            if self.profile.stt_backend == "faster_whisper":
                if self.whisper_model is None or self._loaded_stt_name != self.stt_model_name:
                    from faster_whisper import WhisperModel
                    logger.info(
                        f"Đang nạp Faster-Whisper '{self.stt_model_name}' "
                        f"(device={self.profile.stt_device}, compute_type={self.profile.stt_compute_type})..."
                    )
                    resolved_path = self._resolve_hf_repo_fast(self.stt_model_name)
                    self.whisper_model = WhisperModel(
                        resolved_path,
                        device=self.profile.stt_device,
                        compute_type=self.profile.stt_compute_type
                    )
                    self._loaded_stt_name = self.stt_model_name
                    logger.info(f"Đã nạp xong Faster-Whisper '{self.stt_model_name}'.")

    def transcribe(self, audio_np: np.ndarray, force_lang: str = None) -> tuple[str, float]:
        """Nhận diện giọng nói dùng chung cho cả Chiều Nghe và Chiều Nói (force_lang='vi')"""
        if audio_np is None or len(audio_np) == 0:
            return "", 0.0
        rms = float(np.sqrt(np.mean(np.square(audio_np))))
        if rms < 0.00035:
            return "", 0.0

        # Chuẩn hóa biên độ cho đoạn thoại nhỏ để Mel-spectrogram của Whisper luôn rõ nét
        peak = float(np.max(np.abs(audio_np)))
        if 1e-4 < peak < 0.35:
            gain = min(12.0, 0.65 / peak)
            audio_np = np.clip(audio_np * gain, -1.0, 1.0).astype(np.float32, copy=False)

        t0 = time.perf_counter()
        audio_dur = len(audio_np) / 16000.0
        lang = force_lang if force_lang else (None if self.source_lang == "auto" else self.source_lang)

        # Nạp từ điển dự án (Custom Glossary) + ngữ cảnh từ vựng 1-2 câu gần nhất cho Whisper STT
        prompt_parts = []
        if self._glossary_stt_hint:
            prompt_parts.append(self._glossary_stt_hint)
        if force_lang is None and self.stt_prompt_history:
            prompt_parts.append(" ".join(self.stt_prompt_history)[-160:].strip())
        initial_prompt = ". ".join(p for p in prompt_parts if p).strip() or None

        with self.gpu_lock:
            if self.profile.stt_backend == "mlx_whisper":
                import mlx_whisper
                res = mlx_whisper.transcribe(
                    audio_np,
                    path_or_hf_repo=self.stt_model_name,
                    language=lang,
                    initial_prompt=initial_prompt,
                    temperature=0.0,
                    condition_on_previous_text=False
                )
                raw_mlx = res.get("text", "").strip()
                text = "" if _MUSIC_TAG_RE.match(raw_mlx) else self._collapse_repetitions(raw_mlx)
                if force_lang is None and text and len(text.split()) >= 3:
                    self.stt_prompt_history.append(text)
            else:
                if self.whisper_model is None or self._loaded_stt_name != self.stt_model_name:
                    self.load_stt_model()
                segments, _ = self.whisper_model.transcribe(
                    audio_np,
                    language=lang,
                    beam_size=2,
                    best_of=1,
                    temperature=0.0,
                    initial_prompt=initial_prompt,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.75,
                    log_prob_threshold=-1.0,
                    compression_ratio_threshold=2.3,
                    repetition_penalty=1.15,
                    no_repeat_ngram_size=4,
                )
                kept_texts = []
                logprobs = []
                max_comp_ratio = 1.0
                for seg in segments:
                    raw_txt = seg.text.strip()
                    if not raw_txt:
                        continue
                    # Bỏ qua các đoạn chỉ có nốt nhạc / nhãn nhạc nền không lời ('♪', '[Music]', '(applause)')
                    if _MUSIC_TAG_RE.match(raw_txt):
                        continue
                    # Thu gọn các chuỗi bị lặp vô hạn
                    seg_txt = self._collapse_repetitions(raw_txt)
                    if not seg_txt:
                        continue
                    comp_ratio = getattr(seg, "compression_ratio", 1.0)
                    if comp_ratio > max_comp_ratio:
                        max_comp_ratio = comp_ratio
                    # Nếu compression ratio cao bất thường và chuỗi ngắn sau khi thu gọn thì bỏ qua
                    if comp_ratio > 2.4 and (len(seg_txt.split()) < 3 or seg.avg_logprob < -0.70):
                        continue
                    # Giữ được thoại phim có nhạc nền lớn (no_speech_prob cao vừa phải), chỉ lọc nhiễu thực sự
                    if seg.no_speech_prob > 0.85 and seg.avg_logprob < -0.80:
                        continue
                    if seg.avg_logprob < -1.25:
                        continue
                    norm = re.sub(r"[^\w\s]", "", seg_txt.lower()).strip()
                    if norm in _ALWAYS_HALLUCINATIONS and (seg.no_speech_prob > 0.30 or seg.avg_logprob < -0.55):
                        continue
                    if (
                        norm in _WHISPER_HALLUCINATIONS
                        and (audio_dur < 1.6 or seg.no_speech_prob > 0.55)
                        and (seg.no_speech_prob > 0.30 or seg.avg_logprob < -0.60 or rms < 0.004)
                    ):
                        continue
                    kept_texts.append(seg_txt)
                    logprobs.append(seg.avg_logprob)

                text = " ".join(kept_texts).strip()
                # Thu gọn lần cuối toàn bộ câu nếu có đoạn lặp vắt qua 2 segments
                text = self._collapse_repetitions(text)
                if force_lang is None and text and logprobs and max_comp_ratio <= 2.1:
                    mean_lp = sum(logprobs) / len(logprobs)
                    if len(text.split()) >= 3 and mean_lp > -0.75:
                        self.stt_prompt_history.append(text)

        stt_ms = (time.perf_counter() - t0) * 1000.0
        return text, stt_ms

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return bool(_CJK_RE.search(text))

    @staticmethod
    def _is_untranslated_echo(
        source_text: str,
        vi_text: str,
        source_lang: str,
        extra_allowed: set[str] | None = None,
    ) -> bool:
        """Phát hiện trường hợp LLM bê nguyên xi câu tiếng Anh, sinh phản hồi Chatbot, hoặc chêm từ tiếng Anh chưa dịch"""
        if source_lang not in ("en", "auto") or not vi_text:
            return False
        vi_low = vi_text.lower().strip()
        # 1. Phát hiện phản hồi dạng trợ lý AI / Chatbot tiếng Anh
        if vi_low.startswith((
            "it seems", "i'm sorry", "sorry,", "sure,", "here is", "the source text",
            "based on what", "if you'd like", "what specific", "it appears", "there is some missing",
            "as an ai", "could you please", "please provide",
        )):
            return True

        # 2. Tách từ theo biên Unicode chuẩn (KHÔNG cắt nát chữ tiếng Việt có dấu thành mảnh ASCII rời)
        unicode_tokens = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", vi_text)
        # Phát hiện lỗi LLM dính liền âm tiết tiếng Việt có dấu với từ tiếng Anh viết hoa (VD: 'lờiAdvice', 'ngườiSpeaker')
        for tok in unicode_tokens:
            if len(tok) >= 5 and _VI_DIACRITIC_RE.search(tok) and re.search(r"[A-Z][a-z]{2,}", tok[1:]):
                return True
        ascii_words = [t for t in unicode_tokens if t.isascii()]
        if ascii_words:
            allowed = _ALLOWED_ASCII_LOANWORDS | (extra_allowed or set())
            ascii_low = [w.lower() for w in ascii_words]
            en_hits = sum(1 for w in ascii_low if w in _EN_STOPWORDS)
            total_words = max(1, len(unicode_tokens))
            # Câu dài từ 4 từ trở lên, hoàn toàn không có dấu tiếng Việt và có từ chức năng tiếng Anh
            if total_words >= 4 and not _VI_DIACRITIC_RE.search(vi_text) and en_hits >= 1:
                return True
            # Tỷ lệ từ chức năng tiếng Anh chiếm >= 25% tổng số từ
            if en_hits >= 3 and (en_hits / total_words) >= 0.25:
                return True

            # Quy luật âm vị Quốc ngữ: Không tồn tại âm tiết tiếng Việt không dấu nào dài >= 7 chữ cái thuần ASCII
            # hoặc kết thúc bằng hậu tố hình thái tiếng Anh (-ing, -tion, -ment, -ness, -less, -able, -ible).
            # Nếu từ viết thường (không phải Tên riêng viết hoa) và không thuộc từ mượn IT -> chắc chắn bị rò tiếng Anh!
            src_low = (source_text or "").lower()
            for raw_w, low_w in zip(ascii_words, ascii_low):
                if not raw_w.islower() or low_w in allowed or "'" in low_w:
                    continue
                if len(low_w) >= 7:
                    return True
                if len(low_w) >= 5 and (
                    low_w.endswith(("ing", "tion", "ment", "ness", "less", "able", "ible"))
                    or (any(ch in low_w for ch in "fjwz") and low_w in src_low)
                ):
                    return True

        # 3. Phát hiện câu nhại lại nguyên văn hoặc từ đơn lẻ chưa dịch
        s_clean = re.sub(r"[^\w\s]", "", source_text.lower()).strip()
        v_clean = re.sub(r"[^\w\s]", "", vi_text.lower()).strip()
        if not s_clean or not v_clean:
            return False
        if s_clean == v_clean and len(s_clean.split()) >= 2:
            return True
        if s_clean in {"fine", "whatever", "misfire", "never", "wait", "stop", "enough"} and s_clean == v_clean:
            return True
        return False

    @classmethod
    def _clean_translation_output(cls, raw_text: str, source_text: str) -> str:
        out = raw_text.strip()
        # Loại bỏ các thẻ XML/HTML hoàn chỉnh hoặc bị cắt cụt (vd: </ripsi, </source, <vi>)
        out = re.sub(r"</?[a-zA-Z0-9_:-]{1,20}>?", "", out).strip()
        # Xóa triệt để ký tự khoảng trắng Byte-level \u0120 nếu có
        out = out.replace("\u0120", " ")
        for prefix in ("Bản dịch:", "Tiếng Việt:", "Dịch:", "Translation:", "Vietnamese:"):
            if out.lower().startswith(prefix.lower()):
                out = out[len(prefix):].strip()
        # Loại bỏ dấu gạch đầu dòng thoại phim ("- Chào.") trước khi bóc dấu ngoặc kép bao ngoài
        if out.startswith("-") and not source_text.lstrip().startswith("-"):
            out = out.lstrip("-").strip()
        out = out.strip('"').strip("'").strip("“”").strip()
        if out.startswith("-") and not source_text.lstrip().startswith("-"):
            out = out.lstrip("-").strip()
        out = re.sub(r"\s{2,}", " ", out).strip()
        return cls._collapse_repetitions(out)

    def _strip_context_echo(self, cleaned: str, source_text: str) -> str:
        """Loại bỏ trường hợp LLM chép lại bản dịch của câu ngữ cảnh trước vào ĐẦU câu mới (không bao giờ đục giữa câu)"""
        if not cleaned or not self.context_history:
            return cleaned

        src_low = source_text.lower()
        # 1. Chỉ cắt khi bản dịch câu trước nằm ở ĐẦU (prefix) của câu mới và câu gốc không hề lặp lại câu trước
        for prev_src, prev_vi in list(self.context_history)[-2:]:
            if prev_src and prev_src.strip().lower() in src_low:
                continue
            pv = prev_vi.strip().rstrip(".!?,;-")
            if len(pv) >= 10 and len(cleaned) > len(pv) + 4:
                if cleaned.lower().startswith(pv.lower()):
                    cleaned = cleaned[len(pv):].lstrip(" .!?,;-")

        # 2. So khớp mờ câu đầu tiên (khi LLM diễn đạt lại câu ngữ cảnh trước đó ở câu mở đầu)
        sentences = [s.strip() for s in re.split(r"(?<=[\.\!\?])\s+", cleaned) if s.strip()]
        if len(sentences) >= 2:
            first_s = sentences[0]
            first_words = set(re.findall(r"\w+", first_s.lower()))
            if len(first_words) >= 4:
                for prev_src, prev_vi in list(self.context_history)[-2:]:
                    if prev_src and prev_src.lower() in src_low:
                        continue
                    prev_words = set(re.findall(r"\w+", prev_vi.lower()))
                    if not prev_words:
                        continue
                    overlap = len(first_words & prev_words) / float(len(first_words))
                    if overlap >= 0.62:
                        cleaned = " ".join(sentences[1:]).strip()
                        break

        return re.sub(r"\s*\n+\s*", " ", cleaned).strip()

    def _can_use_ollama_now(self) -> bool:
        if self._ollama_recovering:
            return False
        if self._ollama_cooldown_until > 0 and time.monotonic() < self._ollama_cooldown_until:
            return False
        if self._user_preferred_mode == "ollama" and self.translation_mode != "ollama":
            self.translation_mode = "ollama"
        return self.translation_mode == "ollama"

    def translate_to_vietnamese(self, source_text: str) -> tuple[str, float]:
        """Chiều Nghe: Dịch từ Ngoại ngữ -> Tiếng Việt"""
        if not source_text:
            return "", 0.0
        t0 = time.perf_counter()

        src_flores = NLLB_LANG_MAP.get(self.source_lang, "eng_Latn")
        if self._can_use_ollama_now():
            vi_text = self._translate_ollama_to_vi(source_text, fallback_src_flores=src_flores)
        else:
            vi_text = self._translate_nllb(source_text, src_lang=src_flores, tgt_lang="vie_Latn")
        trans_ms = (time.perf_counter() - t0) * 1000.0
        # Tuyệt đối không lưu câu nhiễm ký tự ngoại lai hoặc câu chưa dịch vào bộ nhớ ngữ cảnh để tránh lây nhiễm các câu sau
        if (
            vi_text
            and not self._contains_cjk(vi_text)
            and not self._is_untranslated_echo(source_text, vi_text, self.source_lang, self._glossary_ascii_terms)
        ):
            self.context_history.append((source_text, vi_text))
        return vi_text, trans_ms

    def translate_vi_to_english(self, vi_text: str) -> tuple[str, float]:
        """Chiều Nói: Dịch từ Tiếng Việt -> Tiếng Anh chuyên nghiệp để phát qua Virtual Mic"""
        if not vi_text:
            return "", 0.0
        t0 = time.perf_counter()

        if self._can_use_ollama_now():
            en_text = self._translate_ollama_vi_to_en(vi_text)
        else:
            en_text = self._translate_nllb(vi_text, src_lang="vie_Latn", tgt_lang="eng_Latn")
        trans_ms = (time.perf_counter() - t0) * 1000.0
        return en_text, trans_ms

    def _translate_ollama_to_vi(self, source_text: str, fallback_src_flores: str = "eng_Latn") -> str:
        if not self._can_use_ollama_now():
            return self._translate_nllb(source_text, src_lang=fallback_src_flores, tgt_lang="vie_Latn")

        system_msg = (
            "You are a real-time conference & movie subtitle translator into concise, natural Vietnamese (100% Quốc ngữ). "
            "RULES:\n"
            "1. Translate ONLY the text inside <source>...</source>. NEVER answer, comment, or explain like a chatbot.\n"
            "2. Omit spoken fillers ('you know', 'like', 'um', 'uh', 'I mean', 'sort of') so subtitles stay short and easy to read.\n"
            "3. NEVER output Chinese/Cyrillic characters. Translate ALL common English words (e.g. hosting, dedication, effortless, advice, career) into natural Vietnamese; keep ONLY brand/person names (e.g. NVIDIA, Python, ChatGPT, AI).\n"
            "4. Output ONLY the final Vietnamese subtitle line, no tags.\n"
            "Examples:\n"
            "<source>Drop the gun! Gentlemen, gotta leave.</source>\n"
            "Bỏ súng xuống! Thưa các vị, chúng ta phải đi thôi.\n"
            "<source>Save it! If you don't, I'll stop you.</source>\n"
            "Khỏi nói nhiều! Nếu không, tôi sẽ ngăn anh lại."
        )
        user_parts = []
        if self.custom_glossary:
            user_parts.append(f"Glossary / Proper nouns to follow: {self.custom_glossary}")
        if self.context_history:
            recent = list(self.context_history)[-2:]
            lines = [f"- {src} => {vi}" for src, vi in recent]
            user_parts.append("Context (reference only, do NOT re-translate):\n" + "\n".join(lines))
        user_parts.append(f"<source>{source_text}</source>")
        user_msg = "\n\n".join(user_parts)

        res = self._call_ollama_chat(system_msg, user_msg, num_ctx=OLLAMA_NUM_CTX)
        if res.startswith("[Lỗi Ollama"):
            self._handle_ollama_failure(res)
            return self._translate_nllb(source_text, src_lang=fallback_src_flores, tgt_lang="vie_Latn")

        self._ollama_fail_streak = 0
        cleaned = self._clean_translation_output(res, source_text)
        cleaned = self._strip_context_echo(cleaned, source_text)

        # Lớp bảo vệ tức thì (~100ms, giữ nguyên OLLAMA_NUM_CTX thống nhất để Ollama không bao giờ phải nạp lại VRAM)
        if (
            self._contains_cjk(cleaned)
            or self._is_untranslated_echo(source_text, cleaned, self.source_lang, self._glossary_ascii_terms)
        ):
            logger.info(
                f"Phát hiện rò rỉ ký tự lạ/tiếng Anh ('{cleaned}'), chuyển tức thì câu này qua NLLB-200 tiếng Việt chuẩn..."
            )
            return self._translate_nllb(source_text, src_lang=fallback_src_flores, tgt_lang="vie_Latn")

        return cleaned

    def _translate_ollama_vi_to_en(self, vi_text: str) -> str:
        if not self._can_use_ollama_now():
            return self._translate_nllb(vi_text, src_lang="vie_Latn", tgt_lang="eng_Latn")

        system_msg = (
            "You are a professional real-time business meeting interpreter. Translate the Vietnamese speech inside "
            "<source>...</source> into natural, clear, polite, professional spoken English. "
            "Return ONLY the English translation, no tags or explanation."
        )
        if self.custom_glossary:
            user_msg = f"Glossary / Proper nouns to follow: {self.custom_glossary}\n\n<source>{vi_text}</source>"
        else:
            user_msg = f"<source>{vi_text}</source>"
        res = self._call_ollama_chat(system_msg, user_msg, num_ctx=OLLAMA_NUM_CTX)
        if res.startswith("[Lỗi Ollama"):
            self._handle_ollama_failure(res)
            return self._translate_nllb(vi_text, src_lang="vie_Latn", tgt_lang="eng_Latn")
        self._ollama_fail_streak = 0
        return self._clean_translation_output(res, vi_text)

    @staticmethod
    def _normalize_lang_code(lang: str | None) -> str:
        raw = (lang or "").strip().lower()
        if not raw:
            return "en"
        if raw.startswith("a.") and len(raw) > 2:
            raw = raw[2:]
        base = raw.split("-")[0].split("_")[0]
        if base in ("en", "ja", "zh", "ko", "vi", "auto"):
            return base
        return base

    @classmethod
    def _resolve_flores_lang(cls, lang: str | None) -> str:
        base = cls._normalize_lang_code(lang)
        if base in NLLB_LANG_MAP:
            return NLLB_LANG_MAP[base]
        extra = {
            "fr": "fra_Latn",
            "de": "deu_Latn",
            "es": "spa_Latn",
            "pt": "por_Latn",
            "it": "ita_Latn",
            "ru": "rus_Cyrl",
            "id": "ind_Latn",
            "nl": "nld_Latn",
        }
        return extra.get(base, "eng_Latn")

    @staticmethod
    def _strip_echo_against_pairs(
        cleaned: str, source_text: str, prev_pairs: list[tuple[str, str]]
    ) -> str:
        if not cleaned or not prev_pairs:
            return cleaned
        src_low = (source_text or "").lower()
        for prev_src, prev_vi in prev_pairs[-2:]:
            if prev_src and prev_src.strip().lower() in src_low:
                continue
            pv = (prev_vi or "").strip().rstrip(".!?,;-")
            if len(pv) >= 10 and len(cleaned) > len(pv) + 4:
                if cleaned.lower().startswith(pv.lower()):
                    cleaned = cleaned[len(pv):].lstrip(" .!?,;-")
        return re.sub(r"\s*\n+\s*", " ", cleaned).strip()

    @staticmethod
    def _parse_caption_batch_output(raw_text: str, expected_count: int) -> dict[int, str]:
        """Giải mã linh hoạt JSON hoặc danh sách đánh số từ LLM về ánh xạ 1-based {1: vi_1, ...}."""
        out: dict[int, str] = {}
        txt = (raw_text or "").strip()
        if not txt or expected_count <= 0:
            return out

        parsed_obj = None
        try:
            parsed_obj = json.loads(txt)
        except Exception:
            first_brace = txt.find("{")
            last_brace = txt.rfind("}")
            if 0 <= first_brace < last_brace:
                try:
                    parsed_obj = json.loads(txt[first_brace : last_brace + 1])
                except Exception:
                    parsed_obj = None

        if isinstance(parsed_obj, dict):
            if (
                "0" in parsed_obj
                and str(expected_count) not in parsed_obj
                and all(str(i) in parsed_obj for i in range(expected_count))
            ):
                for i in range(expected_count):
                    val = parsed_obj.get(str(i))
                    if isinstance(val, str) and val.strip():
                        out[i + 1] = val.strip()
                return out

            for i in range(1, expected_count + 1):
                val = parsed_obj.get(str(i))
                if val is None and i in parsed_obj:
                    val = parsed_obj.get(i)
                if isinstance(val, str) and val.strip():
                    out[i] = val.strip()
            if out:
                return out

            for list_key in ("translations", "items", "lines", "cues", "result"):
                arr = parsed_obj.get(list_key)
                if isinstance(arr, list) and len(arr) == expected_count:
                    for idx, val in enumerate(arr, start=1):
                        if isinstance(val, str) and val.strip():
                            out[idx] = val.strip()
                    if out:
                        return out
        elif isinstance(parsed_obj, list) and len(parsed_obj) == expected_count:
            for idx, val in enumerate(parsed_obj, start=1):
                if isinstance(val, str) and val.strip():
                    out[idx] = val.strip()
            if out:
                return out

        for line in txt.splitlines():
            m = re.match(r"^\s*\[?(\d{1,2})\]?[\.\:\)\-]\s+(.+?)\s*$", line)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= expected_count and idx not in out:
                    out[idx] = m.group(2).strip()
        return out

    def translate_caption_batch(
        self,
        items: list[str],
        prev_context: list[tuple[str, str]] | None = None,
        next_context: list[str] | None = None,
        source_lang: str | None = None,
        urgent: bool = False,
    ) -> tuple[list[str], float, str]:
        """
        Dịch chuẩn ngữ cảnh trước/sau (bidirectional context) cho 1..N câu phụ đề YouTube.
        - Giữ cố định OLLAMA_NUM_CTX = 1024 (không bao giờ gây reload VRAM).
        - urgent=True (câu ngay tại playhead): có quyền preempt SummaryWorker để hiện tức thì (< 1s).
        - urgent=False (dịch đón đầu +90s): xếp hàng lịch sự trên _llm_lock, không cắt ngang SummaryWorker.
        - Đảm bảo 100% đầu ra khớp số phần tử 1-1 với items và lọc sạch CJK / rò tiếng Anh từng dòng.
        """
        if not items:
            return [], 0.0, ("Ollama" if self.translation_mode == "ollama" else "NLLB")

        t0 = time.perf_counter()
        eff_lang = self._normalize_lang_code(source_lang or self.source_lang)
        src_flores = self._resolve_flores_lang(eff_lang)
        prev_pairs = list(prev_context or [])[-2:]
        lookahead = [s.strip() for s in (next_context or []) if s and s.strip()][:2]

        if not self._can_use_ollama_now():
            vi_out = [
                self._translate_nllb(src_line, src_lang=src_flores, tgt_lang="vie_Latn") if src_line.strip() else ""
                for src_line in items
            ]
            return vi_out, (time.perf_counter() - t0) * 1000.0, "NLLB"

        # Fast-path 1 câu đơn (thường là câu khẩn cấp ngay tại playhead khi mới bật hoặc vừa tua video)
        if len(items) == 1:
            src_one = (items[0] or "").strip()
            if not src_one:
                return [""], 0.0, "Ollama"
            system_msg = (
                "You are a professional video subtitle translator into concise, natural Vietnamese (100% Quốc ngữ). "
                "RULES:\n"
                "1. Translate ONLY the text inside <source>...</source>. Use previous/upcoming context ONLY to understand pronouns and incomplete clauses — NEVER translate context lines.\n"
                "2. Omit spoken fillers ('you know', 'like', 'um', 'uh', 'I mean').\n"
                "3. NEVER output Chinese/Cyrillic characters. Translate ALL common English words into natural Vietnamese; keep ONLY brand/person/tech proper nouns.\n"
                "4. Output ONLY the final Vietnamese subtitle line, no tags."
            )
            user_parts = []
            if self.custom_glossary:
                user_parts.append(f"Glossary / Proper nouns to follow: {self.custom_glossary}")
            if prev_pairs:
                ctx_lines = [f"- {s} => {v}" for s, v in prev_pairs]
                user_parts.append("Previous lines (reference only, do NOT translate):\n" + "\n".join(ctx_lines))
            user_parts.append(f"<source>{src_one}</source>")
            if lookahead:
                nxt_lines = [f"- {s}" for s in lookahead]
                user_parts.append("Upcoming lines (lookahead only, do NOT translate):\n" + "\n".join(nxt_lines))
            user_msg = "\n\n".join(user_parts)

            res = self._call_ollama_chat(
                system_msg,
                user_msg,
                num_ctx=OLLAMA_NUM_CTX,
                num_predict=130,
                json_mode=False,
                urgent=urgent,
                timeout_sec=6.5,
            )
            if res.startswith("[Lỗi Ollama"):
                self._handle_ollama_failure(res)
                vi_fallback = self._translate_nllb(src_one, src_lang=src_flores, tgt_lang="vie_Latn")
                return [vi_fallback], (time.perf_counter() - t0) * 1000.0, "NLLB"

            self._ollama_fail_streak = 0
            cleaned = self._clean_translation_output(res, src_one)
            cleaned = self._strip_echo_against_pairs(cleaned, src_one, prev_pairs)
            echo_check_lang = "en" if eff_lang in ("en", "auto") else eff_lang
            if (
                not cleaned
                or self._contains_cjk(cleaned)
                or self._is_untranslated_echo(src_one, cleaned, echo_check_lang, self._glossary_ascii_terms)
            ):
                vi_fallback = self._translate_nllb(src_one, src_lang=src_flores, tgt_lang="vie_Latn")
                return [vi_fallback], (time.perf_counter() - t0) * 1000.0, "NLLB"
            return [cleaned], (time.perf_counter() - t0) * 1000.0, "Ollama"

        # Batch 2..N câu với JSON có khóa đánh số tường minh để giữ căn chỉnh mốc thời gian 1-1
        expected_keys = ", ".join(f'"{i}"' for i in range(1, len(items) + 1))
        system_msg = (
            "You are a professional video subtitle translator into concise, natural Vietnamese (100% Quốc ngữ). "
            "RULES:\n"
            f"1. Translate EACH numbered subtitle in <batch>...</batch> into natural Vietnamese and return a JSON object with EXACT keys {expected_keys}.\n"
            "2. NEVER merge two numbered items into one and NEVER omit any key. Each key must translate ONLY its corresponding numbered line, using surrounding lines and context only to resolve pronouns, grammar, and meaning.\n"
            "3. Omit spoken fillers ('you know', 'like', 'um', 'uh', 'I mean'). NEVER output Chinese/Cyrillic characters. Translate ALL common English words into natural Vietnamese; keep ONLY brand/person/tech proper nouns.\n"
            '4. Return ONLY valid JSON, e.g. {"1": "...", "2": "..."}.'
        )
        user_parts = []
        if self.custom_glossary:
            user_parts.append(f"Glossary / Proper nouns to follow: {self.custom_glossary}")
        if prev_pairs:
            ctx_lines = [f"- {s} => {v}" for s, v in prev_pairs]
            user_parts.append("Previous context (do NOT include in JSON):\n" + "\n".join(ctx_lines))
        batch_lines = [f"[{idx}] {line.strip()}" for idx, line in enumerate(items, start=1)]
        user_parts.append("<batch>\n" + "\n".join(batch_lines) + "\n</batch>")
        if lookahead:
            nxt_lines = [f"- {s}" for s in lookahead]
            user_parts.append("Upcoming lookahead (do NOT include in JSON):\n" + "\n".join(nxt_lines))
        user_msg = "\n\n".join(user_parts)

        res = self._call_ollama_chat(
            system_msg,
            user_msg,
            num_ctx=OLLAMA_NUM_CTX,
            num_predict=min(320, max(160, 72 * len(items))),
            json_mode=True,
            urgent=urgent,
            timeout_sec=9.5,
        )
        if res.startswith("[Lỗi Ollama"):
            self._handle_ollama_failure(res)
            vi_out = [
                self._translate_nllb(src_line, src_lang=src_flores, tgt_lang="vie_Latn") if src_line.strip() else ""
                for src_line in items
            ]
            return vi_out, (time.perf_counter() - t0) * 1000.0, "NLLB"

        self._ollama_fail_streak = 0
        parsed_map = self._parse_caption_batch_output(res, len(items))
        echo_check_lang = "en" if eff_lang in ("en", "auto") else eff_lang

        final_out: list[str] = []
        rolling_pairs = list(prev_pairs)
        ollama_ok_count = 0
        for idx, src_line in enumerate(items, start=1):
            src_stripped = (src_line or "").strip()
            if not src_stripped:
                final_out.append("")
                continue
            raw_vi = parsed_map.get(idx, "")
            cleaned = self._clean_translation_output(raw_vi, src_stripped) if raw_vi else ""
            if cleaned:
                cleaned = self._strip_echo_against_pairs(cleaned, src_stripped, rolling_pairs)

            duplicate_of_prev = (
                bool(cleaned)
                and bool(final_out)
                and len(cleaned.split()) >= 3
                and cleaned.lower() == final_out[-1].lower()
                and src_stripped.lower() != (items[idx - 2] or "").strip().lower()
            )
            invalid = (
                not cleaned
                or duplicate_of_prev
                or self._contains_cjk(cleaned)
                or self._is_untranslated_echo(
                    src_stripped, cleaned, echo_check_lang, self._glossary_ascii_terms
                )
            )
            if invalid:
                rescued = self._translate_nllb(src_stripped, src_lang=src_flores, tgt_lang="vie_Latn")
                final_out.append(rescued)
                if rescued and not self._contains_cjk(rescued):
                    rolling_pairs.append((src_stripped, rescued))
            else:
                final_out.append(cleaned)
                ollama_ok_count += 1
                rolling_pairs.append((src_stripped, cleaned))

        engine_label = "Ollama" if ollama_ok_count > 0 else "NLLB"
        return final_out, (time.perf_counter() - t0) * 1000.0, engine_label

    def _create_summary_session(self) -> requests.Session:
        session = requests.Session()
        try:
            adapter = session.get_adapter(self.ollama_chat_url)
            pool = adapter.poolmanager.connection_from_url(self.ollama_chat_url)
            orig_get_conn = pool._get_conn
            orig_new_conn = pool._new_conn

            def _bind_conn(conn):
                if not getattr(conn, "_summary_tracked", False):
                    orig_connect = conn.connect

                    def _tracked_connect():
                        res = orig_connect()
                        with self._summary_resp_lock:
                            self._active_summary_conn = conn
                        return res

                    conn.connect = _tracked_connect
                    conn._summary_tracked = True
                with self._summary_resp_lock:
                    self._active_summary_conn = conn
                return conn

            def _tracked_get_conn(timeout=None):
                return _bind_conn(orig_get_conn(timeout=timeout))

            def _tracked_new_conn():
                return _bind_conn(orig_new_conn())

            pool._get_conn = _tracked_get_conn
            pool._new_conn = _tracked_new_conn
        except Exception:
            pass
        return session

    def _abort_active_summary_stream(self) -> bool:
        """Đóng ngay kết nối stream tóm tắt đang chạy để nhường GPU/Ollama tức thì cho dòng phụ đề trực tiếp."""
        with self._summary_resp_lock:
            conn = self._active_summary_conn
            resp = self._active_summary_resp
            self._active_summary_conn = None
            self._active_summary_resp = None
            had_active = (conn is not None) or (resp is not None)
            if had_active:
                self._summary_stream_aborted = True

        if conn is not None:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

        if resp is not None:
            raw = getattr(resp, "raw", None)
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            try:
                resp.close()
            except Exception:
                pass

        return had_active

    def _call_ollama_chat(
        self,
        system_msg: str,
        user_msg: str,
        num_ctx: int = OLLAMA_NUM_CTX,
        *,
        num_predict: int = 120,
        json_mode: bool = False,
        urgent: bool = True,
        timeout_sec: float = 6.5,
    ) -> str:
        payload = {
            "model": self.ollama_model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.05,
                "top_p": 0.85,
                "top_k": 15,
                "repeat_penalty": 1.1,
                "num_predict": int(num_predict),
                "num_ctx": num_ctx,
            },
        }
        if json_mode:
            payload["format"] = "json"

        preempted_active = False
        if urgent:
            self._llm_preempt.set()
            preempted_active = self._abort_active_summary_stream()
        elif self._llm_preempt.is_set():
            # Nhường làn cho yêu cầu trực tiếp (F8 hoặc câu ngay tại playhead) vào trước
            time.sleep(0.05)

        with self._llm_lock:
            self.last_translate_preempted_summary = preempted_active
            if urgent:
                self._llm_preempt.clear()
            try:
                # Suy luận trực tiếp sau warmup chỉ tốn ~0.4-1.2s; giới hạn timeout 6.5s để không bao giờ treo phụ đề 20s
                resp = self._http.post(self.ollama_chat_url, json=payload, timeout=timeout_sec)
                if resp.status_code >= 500 and platform.system() == "Windows" and not self._ollama_vulkan_retried:
                    self._ollama_vulkan_retried = True
                    logger.warning("Phát hiện lỗi HTTP 500 từ Ollama CUDA, đang tự khởi động lại sang GPU Vulkan...")
                    if self._start_or_restart_ollama(use_vulkan=True):
                        resp = self._http.post(self.ollama_chat_url, json=payload, timeout=15)
                resp.raise_for_status()
                msg = resp.json().get("message", {})
                return msg.get("content", "").strip()
            except Exception as e:
                return f"[Lỗi Ollama ({self.ollama_model}): {e}]"

    def generate_summary_json(
        self,
        system_msg: str,
        user_msg: str,
        max_wall_sec: float = 8.0,
    ) -> tuple[dict | None, dict]:
        """Trả (data | None, meta). meta = {ok, preempted, busy, error, prompt_ms, total_ms, raw}"""
        meta = {
            "ok": False,
            "preempted": False,
            "busy": False,
            "error": "",
            "prompt_ms": 0.0,
            "total_ms": 0.0,
            "raw": "",
        }
        if not self._can_use_ollama_now():
            meta["error"] = "no_ollama"
            return None, meta

        if self._llm_preempt.is_set():
            meta["preempted"] = True
            meta["busy"] = True
            meta["error"] = "preempted"
            return None, meta

        if not self._llm_lock.acquire(blocking=False):
            meta["busy"] = True
            meta["error"] = "busy"
            return None, meta

        t0 = time.perf_counter()
        resp = None
        chunks: list[str] = []
        need_reset_session = False
        try:
            if self._llm_preempt.is_set():
                meta["preempted"] = True
                meta["error"] = "preempted"
                return None, meta

            with self._summary_resp_lock:
                self._summary_stream_aborted = False

            payload = {
                "model": self.ollama_model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "stream": True,
                "format": "json",
                "keep_alive": "30m",
                "options": {
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "top_k": 30,
                    "repeat_penalty": 1.1,
                    "num_predict": 220,
                    "num_ctx": OLLAMA_NUM_CTX,
                },
            }

            resp = self._http_summary.post(
                self.ollama_chat_url,
                json=payload,
                stream=True,
                timeout=(3.0, max_wall_sec),
            )
            with self._summary_resp_lock:
                self._active_summary_resp = resp

            if self._llm_preempt.is_set() or self._summary_stream_aborted:
                need_reset_session = True
                meta["preempted"] = True
                meta["error"] = "preempted"
                meta["total_ms"] = (time.perf_counter() - t0) * 1000.0
                return None, meta

            resp.raise_for_status()

            for line in resp.iter_lines(decode_unicode=True):
                now_el = time.perf_counter() - t0
                if self._llm_preempt.is_set() or self._summary_stream_aborted:
                    need_reset_session = True
                    meta["preempted"] = True
                    meta["error"] = "preempted"
                    meta["raw"] = "".join(chunks).strip()
                    meta["total_ms"] = now_el * 1000.0
                    return None, meta

                if now_el > max_wall_sec:
                    need_reset_session = True
                    meta["error"] = "timeout"
                    meta["raw"] = "".join(chunks).strip()
                    meta["total_ms"] = now_el * 1000.0
                    return None, meta

                if not line:
                    continue

                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    if meta["prompt_ms"] == 0.0:
                        meta["prompt_ms"] = (time.perf_counter() - t0) * 1000.0
                    chunks.append(piece)

                if chunk.get("done"):
                    break

            meta["total_ms"] = (time.perf_counter() - t0) * 1000.0
            if meta["prompt_ms"] == 0.0:
                meta["prompt_ms"] = meta["total_ms"]
            raw_text = "".join(chunks).strip()
            meta["raw"] = raw_text

            if self._llm_preempt.is_set() or self._summary_stream_aborted:
                need_reset_session = True
                meta["preempted"] = True
                meta["error"] = "preempted"
                return None, meta

            data = None
            try:
                data = json.loads(raw_text)
            except Exception:
                start_idx = raw_text.find("{")
                end_idx = raw_text.rfind("}")
                if 0 <= start_idx < end_idx:
                    try:
                        data = json.loads(raw_text[start_idx : end_idx + 1])
                    except Exception:
                        data = None

            if not isinstance(data, dict):
                meta["ok"] = False
                meta["error"] = "bad_json"
                return None, meta

            meta["ok"] = True
            meta["error"] = ""
            return data, meta

        except Exception as e:
            need_reset_session = True
            meta["total_ms"] = (time.perf_counter() - t0) * 1000.0
            meta["raw"] = "".join(chunks).strip()
            if self._llm_preempt.is_set() or self._summary_stream_aborted:
                meta["preempted"] = True
                meta["error"] = "preempted"
            elif isinstance(e, requests.Timeout) or (time.perf_counter() - t0) >= max_wall_sec:
                meta["error"] = "timeout"
            else:
                meta["error"] = f"http_error: {e}"
            return None, meta

        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            with self._summary_resp_lock:
                self._active_summary_resp = None
                self._active_summary_conn = None
                if self._summary_stream_aborted:
                    need_reset_session = True
                self._summary_stream_aborted = False
            if need_reset_session:
                try:
                    self._http_summary.close()
                except Exception:
                    pass
                self._http_summary = self._create_summary_session()
            self._llm_lock.release()

    def _call_ollama(self, prompt: str) -> str:
        """Giữ tương thích ngược cho các tiện ích mở rộng gọi trực tiếp _call_ollama(prompt)"""
        return self._call_ollama_chat("You are a concise, accurate translator.", prompt)

    def _ensure_nllb_loaded(self, lang_code: str = "eng_Latn"):
        import ctranslate2

        if self.nllb_translator is None or self._nllb_model_dir is None:
            logger.info("Đang tải/nạp model dịch NLLB-200-distilled-600M INT8...")
            self._nllb_model_dir = self._resolve_hf_repo_fast("entai2965/nllb-200-distilled-600M-ctranslate2")
            self.nllb_translator = ctranslate2.Translator(
                self._nllb_model_dir,
                device=self.profile.stt_device,
                compute_type="int8"
            )
            logger.info("Đã nạp xong NLLB-200-distilled-600M INT8.")

        # Ưu tiên SentencePiece nguyên bản của model CTranslate2 để tránh lỗi vỡ token 'Ġ' và không phải nạp transformers
        if self.nllb_sp is None and self._nllb_model_dir:
            sp_path = os.path.join(self._nllb_model_dir, "sentencepiece.bpe.model")
            if os.path.isfile(sp_path):
                try:
                    import sentencepiece as spm
                    self.nllb_sp = spm.SentencePieceProcessor(model_file=sp_path)
                    logger.info("Đã nạp SentencePiece model nguyên bản cho NLLB-200 (khắc phục 100% lỗi tokenizer Ġ).")
                except Exception as e:
                    logger.warning(f"Không nạp được sentencepiece ({e}), fallback sang AutoTokenizer.")

        if self.nllb_sp is None and lang_code not in self.nllb_tokenizers:
            import transformers
            try:
                try:
                    self.nllb_tokenizers[lang_code] = transformers.AutoTokenizer.from_pretrained(
                        self._nllb_model_dir, src_lang=lang_code, fix_mistral_regex=True
                    )
                except TypeError:
                    self.nllb_tokenizers[lang_code] = transformers.AutoTokenizer.from_pretrained(
                        self._nllb_model_dir, src_lang=lang_code
                    )
            except Exception:
                self.nllb_tokenizers[lang_code] = transformers.AutoTokenizer.from_pretrained(
                    "facebook/nllb-200-distilled-600M", src_lang=lang_code
                )

    @staticmethod
    def _split_sentences_for_nmt(text: str) -> list[str]:
        """Tách cụm nhiều câu thành danh sách câu đơn cho NLLB-200 để dịch batch song song không bị sót câu"""
        stripped = (text or "").strip()
        if not stripped:
            return []
        raw_parts = [p.strip() for p in _NMT_SENT_SPLIT_RE.split(stripped) if p.strip()]
        if len(raw_parts) <= 1:
            return [stripped]
        merged: list[str] = []
        for part in raw_parts:
            if merged and len(re.sub(r"[^\w]", "", part)) <= 1:
                merged[-1] = f"{merged[-1]} {part}".strip()
            else:
                merged.append(part)
        return merged or [stripped]

    def _translate_nllb(self, text: str, src_lang: str, tgt_lang: str) -> str:
        sentences = self._split_sentences_for_nmt(text)
        if not sentences:
            return ""

        with self.gpu_lock:
            self._ensure_nllb_loaded(src_lang)
            self._ensure_nllb_loaded(tgt_lang)

            if self.nllb_sp is not None:
                batch_tokens = [
                    [src_lang] + self.nllb_sp.encode_as_pieces(sent) + ["</s>"]
                    for sent in sentences
                ]
                results = self.nllb_translator.translate_batch(
                    batch_tokens,
                    target_prefix=[[tgt_lang]] * len(batch_tokens),
                    beam_size=2,
                    repetition_penalty=1.18,
                    no_repeat_ngram_size=3,
                )
                skip_toks = {src_lang, tgt_lang, "</s>", "<s>", "<unk>"}
                out_parts = []
                for sent_src, res in zip(sentences, results):
                    target_tokens = [tok for tok in res.hypotheses[0] if tok not in skip_toks]
                    raw_decoded = self.nllb_sp.decode_pieces(target_tokens).replace("\u0120", " ")
                    cleaned_part = self._clean_translation_output(raw_decoded, sent_src)
                    if cleaned_part:
                        out_parts.append(cleaned_part)
                return self._collapse_repetitions(" ".join(out_parts).strip())
            else:
                src_tok = self.nllb_tokenizers[src_lang]
                tgt_tok = self.nllb_tokenizers[tgt_lang]
                batch_tokens = [
                    src_tok.convert_ids_to_tokens(src_tok.encode(sent))
                    for sent in sentences
                ]
                results = self.nllb_translator.translate_batch(
                    batch_tokens,
                    target_prefix=[[tgt_lang]] * len(batch_tokens),
                    beam_size=2,
                    repetition_penalty=1.15,
                    no_repeat_ngram_size=3,
                )
                out_parts = []
                for sent_src, res in zip(sentences, results):
                    target_tokens = res.hypotheses[0][1:]
                    raw_decoded = tgt_tok.decode(
                        tgt_tok.convert_tokens_to_ids(target_tokens), skip_special_tokens=True
                    ).replace("\u0120", " ")
                    cleaned_part = self._clean_translation_output(raw_decoded, sent_src)
                    if cleaned_part:
                        out_parts.append(cleaned_part)
                return self._collapse_repetitions(" ".join(out_parts).strip())
