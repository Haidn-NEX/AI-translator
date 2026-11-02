import logging
import math
import platform
import queue
import threading
import time
from collections import deque
import numpy as np
import sounddevice as sd
import torch

logger = logging.getLogger("LiveTranslator")

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # Cửa sổ 32ms chuẩn cho Silero VAD ở 16kHz


class _StreamingResampler:
    """
    Bộ hạ tần số âm thanh (ví dụ 48kHz / 44.1kHz -> 16kHz) theo phương pháp Overlap-Save có bộ lọc
    chống răng cưa (Anti-aliasing Kaiser-windowed sinc qua torchaudio).
    Giữ nguyên pha và biên độ liên tục qua ranh giới từng chunk, giúp phụ âm đầu/cuối ('b', 't', 'c', 'f', 's')
    sắc nét, không bị méo tần số cao (aliasing) như nội suy tuyến tính np.interp.
    """
    def __init__(self, orig_rate: int, target_rate: int = SAMPLE_RATE):
        self.orig_rate = int(orig_rate)
        self.target_rate = int(target_rate)
        self._passthrough = (self.orig_rate == self.target_rate)
        self._resampler = None

        if not self._passthrough:
            g = math.gcd(self.orig_rate, self.target_rate)
            self._step_in = self.orig_rate // g
            self._step_out = self.target_rate // g
            # Đệm ngữ cảnh ~10ms hai đầu mỗi khối để triệt tiêu hoàn toàn nhiễu rìa bộ lọc FIR
            pad_blocks = max(1, math.ceil(0.010 * self.orig_rate / self._step_in))
            self._pad_in = pad_blocks * self._step_in
            self._pad_out = pad_blocks * self._step_out
            self._left_ctx = np.zeros(self._pad_in, dtype=np.float32)
            self._pending = np.empty(0, dtype=np.float32)
            try:
                import torchaudio.transforms as T
                self._resampler = T.Resample(orig_freq=self.orig_rate, new_freq=self.target_rate)
            except Exception as e:
                logger.warning(f"Không nạp được torchaudio.transforms.Resample ({e}), dùng bộ lọc fallback.")

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        if len(pcm) == 0:
            return np.empty(0, dtype=np.float32)
        pcm_f32 = pcm.astype(np.float32, copy=False)
        if self._passthrough:
            return pcm_f32

        if self._resampler is None:
            target_len = int(round(len(pcm_f32) * self.target_rate / self.orig_rate))
            if target_len <= 0:
                return np.empty(0, dtype=np.float32)
            return np.interp(
                np.linspace(0, len(pcm_f32), target_len, endpoint=False),
                np.arange(len(pcm_f32)),
                pcm_f32,
            ).astype(np.float32)

        self._pending = np.concatenate((self._pending, pcm_f32))
        if len(self._pending) < self._step_in + self._pad_in:
            return np.empty(0, dtype=np.float32)

        n_blocks = (len(self._pending) - self._pad_in) // self._step_in
        core_in = n_blocks * self._step_in
        core_out = n_blocks * self._step_out

        window = np.concatenate((self._left_ctx, self._pending[:core_in + self._pad_in]))
        with torch.no_grad():
            resampled = self._resampler(torch.from_numpy(window))
        out = resampled[self._pad_out:self._pad_out + core_out].numpy().astype(np.float32, copy=False)

        self._left_ctx = window[core_in:self._pad_in + core_in].copy()
        self._pending = self._pending[core_in:]
        return np.clip(out, -1.0, 1.0)


class _StreamingAGC:
    """
    Bộ tự động cân bằng âm lượng (Streaming AGC + Instant Limiter) cho âm thanh hệ thống.
    Giúp bắt nhạy cả các đoạn thoại phim thì thầm / mở loa nhỏ (RMS ~0.0005 - 0.01) bằng cách nâng mượt
    lên mức chuẩn ~0.065 (-24 dBFS) cho Silero VAD & Whisper, đồng thời hạ gain tức thì chống vỡ tiếng khi hét/nổ súng.
    """
    def __init__(self, target_rms: float = 0.065, noise_floor: float = 0.00025, max_gain: float = 22.0):
        self.target_rms = target_rms
        self.noise_floor = noise_floor
        self.max_gain = max_gain
        self._gain = 1.0

    def __call__(self, frame: np.ndarray) -> np.ndarray:
        if len(frame) == 0:
            return frame
        rms = float(np.sqrt(np.mean(np.square(frame))))
        peak = float(np.max(np.abs(frame)))

        if rms > self.noise_floor and peak > 1e-5:
            desired = min(self.max_gain, max(0.8, self.target_rms / rms))
            if desired * peak > 0.92:
                desired = 0.92 / peak
            alpha = 0.65 if desired < self._gain else 0.28
            self._gain = (1.0 - alpha) * self._gain + alpha * desired
            # Brickwall limiter tức thì chống méo tiếng khi chuyển đột ngột từ thì thầm sang tiếng hét/súng nổ
            if self._gain * peak > 0.95:
                self._gain = 0.95 / peak
        else:
            self._gain = 0.92 * self._gain + 0.08 * 1.0

        return np.clip(frame * self._gain, -1.0, 1.0).astype(np.float32, copy=False)


class _VoiceBandHPF:
    """
    Bộ lọc thông cao pha tuyến tính (65-tap Hanning FIR ~155 Hz, ~16 micro-giây/khung) dành riêng cho nhánh Silero VAD.
    Loại bỏ năng lượng trầm/sub-bass (40-130 Hz) của nhạc nền phim/cello/organ để nhạc nền không chèn ép bộ AGC
    và không làm mù Silero VAD khi nhân vật nói nhỏ hoặc nói qua loa TV/camcorder trên nền nhạc lớn.
    """
    def __init__(self, taps: int = 65, cutoff_hz: float = 155.0, sr: int = SAMPLE_RATE):
        half = taps // 2
        n = np.arange(-half, half + 1, dtype=np.float32)
        fc = cutoff_hz / float(sr)
        lp = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hanning(taps).astype(np.float32)
        lp /= np.sum(lp)
        hp = -lp
        hp[half] += 1.0
        self._kernel = hp[::-1].astype(np.float32)
        self._ctx = np.zeros(taps - 1, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> np.ndarray:
        if len(frame) == 0:
            return frame
        buf = np.concatenate((self._ctx, frame))
        self._ctx = buf[-(len(self._kernel) - 1):].copy()
        return np.convolve(buf, self._kernel, mode="valid").astype(np.float32, copy=False)


class _StreamingSileroVAD:
    """
    Bộ lọc giọng nói Silero VAD thời gian thực.
    Ưu tiên nạp trực tiếp model ONNX có sẵn trong faster_whisper (0ms tải mạng, không khóa GUI),
    fallback về torch.hub nếu không có faster_whisper.
    """
    def __init__(self):
        self._onnx_session = None
        self._torch_model = None
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, 64), dtype=np.float32)

        try:
            import faster_whisper.vad as fw_vad
            self._onnx_session = fw_vad.get_vad_model().session
            logger.info("Đã nạp nhanh Silero VAD ONNX tích hợp sẵn trong faster_whisper.")
        except Exception as e:
            logger.warning(f"Không nạp được faster_whisper VAD ({e}), chuyển sang torch.hub...")
            self._torch_model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False
            )
            self._torch_model.eval()

    def reset_states(self):
        if self._onnx_session is not None:
            self._h.fill(0.0)
            self._c.fill(0.0)
            self._ctx.fill(0.0)
        elif self._torch_model is not None:
            self._torch_model.reset_states()

    def __call__(self, frame_512: np.ndarray) -> float:
        if self._onnx_session is not None:
            frame_2d = frame_512.astype(np.float32, copy=False).reshape(1, CHUNK_SAMPLES)
            inp = np.concatenate([self._ctx, frame_2d], axis=1)
            out, self._h, self._c = self._onnx_session.run(
                None, {"input": inp, "h": self._h, "c": self._c}
            )
            self._ctx = frame_2d[:, -64:]
            return float(np.squeeze(out))
        else:
            with torch.no_grad():
                return float(self._torch_model(torch.from_numpy(frame_512), SAMPLE_RATE).item())


class SystemAudioVADWorker(threading.Thread):
    def __init__(self, sentence_queue: queue.Queue, status_callback=None, loopback_device_index: int = None):
        super().__init__(daemon=True)
        self.sentence_queue = sentence_queue
        self.status_callback = status_callback
        self.loopback_device_index = loopback_device_index
        self.running = False
        self.is_capturing_speech = False
        self.vad_model = None

        self.speech_threshold = 0.34         # Nhạy hơn cho thoại phim nhỏ / có nhạc nền (kèm AGC tự động)
        self.speech_end_threshold = 0.20     # Ngưỡng duy trì Hysteresis giữ trọn âm cuối nhỏ
        self.pre_speech_sec = 0.30           # Đệm trước ~300ms chống mất phụ âm đầu ('but', 'critical', 'for')
        self.post_speech_sec = 0.20          # Đệm sau ~200ms giữ trọn âm cuối câu
        self.short_pause_silence_sec = 0.62  # Cụm rất ngắn (<1.2s): chờ 0.62s để nối liên từ mà vẫn phản hồi nhanh thoại phim
        self.min_silence_sec = 0.48          # Câu thoại chuẩn (1.2s - 3.8s): ngắt ở 0.48s theo nhịp hội thảo tự nhiên
        self.long_silence_sec = 0.32         # Câu dài (>=3.8s): ngắt mượt ở 0.32s
        self.soft_max_sentence_sec = 4.4     # Từ 4.4s: cắt tại nhịp ngắt hơi/dấu phẩy gần nhất
        self.max_sentence_sec = 6.0          # Ngưỡng tối đa 6.0s: cắt tại khe giữa 2 từ, gối sang đoạn sau
        self.phrase_cut_min_sec = 3.2        # Từ 3.2s: chỉ cần nhịp ngắt rõ (VAD < 0.36) là tách cụm đi dịch
        self.phrase_dip_prob = 0.36
        self.phrase_dip_frames = 4           # 4 khung (~128ms) khe nghỉ rõ giữa 2 vế câu
        self.min_speech_sec = 0.22           # Bắt được cả câu thoại/cảm thán ngắn ("Fine.", "Save it!", "Run!") >= 220ms
        self.rescue_window_sec = 4.5         # Cứu hộ thoại bị nhạc nền/camcorder đè (khi loa phát âm liên tục 4.5s mà VAD bị mù)

    @staticmethod
    def list_loopback_devices() -> list[tuple[int, str, bool]]:
        """Trả về danh sách [(device_index, name, is_default_speaker)] trên Windows WASAPI Loopback"""
        if platform.system() != "Windows":
            return []
        out = []
        try:
            import pyaudiowpatch as pyaudio
            p = pyaudio.PyAudio()
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_out = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                default_name = default_out.get("name", "")
                for lb in p.get_loopback_device_info_generator():
                    is_def = bool(default_name and default_name in lb["name"])
                    out.append((int(lb["index"]), lb["name"], is_def))
            finally:
                p.terminate()
        except Exception as e:
            logger.warning(f"Không thể liệt kê thiết bị WASAPI Loopback: {e}")
        return out

    def log(self, msg: str):
        logger.info(f"[SystemAudioVAD] {msg}")
        if self.status_callback:
            self.status_callback(msg)

    def stop(self):
        self.running = False
        self.is_capturing_speech = False

    def run(self):
        self.running = True
        self.is_capturing_speech = False
        try:
            # Khởi tạo VAD trong luồng worker (không bao giờ chạy trên luồng GUI chính)
            self.vad_model = _StreamingSileroVAD()
        except Exception as e:
            self.log(f"Lỗi khởi tạo Silero VAD: {e}")
            self.running = False
            return

        try:
            os_name = platform.system()
            if os_name == "Windows":
                self._capture_windows_wasapi()
            else:
                self._capture_macos_or_linux()
        finally:
            self.is_capturing_speech = False

    def _process_audio_stream(self, float32_16k_generator):
        pre_roll_maxlen = max(2, int(round(self.pre_speech_sec * SAMPLE_RATE / CHUNK_SAMPLES)))
        post_keep_frames = max(2, int(round(self.post_speech_sec * SAMPLE_RATE / CHUNK_SAMPLES)))
        rescue_max_frames = max(60, int(round(self.rescue_window_sec * SAMPLE_RATE / CHUNK_SAMPLES)))
        rescue_soft_frames = max(50, rescue_max_frames - 10)

        audio_agc = _StreamingAGC()
        vad_hpf = _VoiceBandHPF(taps=65, cutoff_hz=155.0)
        vad_agc = _StreamingAGC()

        pre_speech_frames = deque(maxlen=pre_roll_maxlen)
        audio_buffer = []
        buffer_probs = []
        dip_frames = 0
        rescue_frames = []
        rescue_hint_frames = 0
        rescue_quiet_frames = 0
        idle_vad_frames = 0

        speech_detected = False
        onset_streak = 0
        voiced_frames = 0
        silence_frames = 0
        remainder = np.array([], dtype=np.float32)

        for raw_chunk in float32_16k_generator:
            if not self.running:
                break
            if len(raw_chunk) == 0:
                continue
            combined = np.concatenate((remainder, raw_chunk))
            offset = 0
            while offset + CHUNK_SAMPLES <= len(combined):
                raw_frame = combined[offset:offset + CHUNK_SAMPLES]
                offset += CHUNK_SAMPLES

                # Nhánh 1: Giữ nguyên dải tần tự nhiên đã cân bằng AGC cho Faster-Whisper
                frame = audio_agc(raw_frame)
                # Nhánh 2 (Side-Chain VAD): Lọc bỏ sub-bass nhạc nền phim (<155Hz) trước khi đưa vào Silero VAD
                hp_raw = vad_hpf(raw_frame)
                vb_rms = float(np.sqrt(np.mean(np.square(hp_raw))))
                vad_frame = vad_agc(hp_raw)
                speech_prob = self.vad_model(vad_frame)

                if not speech_detected:
                    pre_speech_frames.append(frame)
                    idle_vad_frames += 1
                    # Làm mới định kỳ trạng thái LSTM khi chưa vào câu để tránh bão hòa cell state bởi nhạc nền dài
                    if idle_vad_frames >= 125:
                        self.vad_model.reset_states()
                        idle_vad_frames = 0

                    if speech_prob >= self.speech_threshold:
                        onset_streak += 1
                    else:
                        onset_streak = 0

                    # Yêu cầu 2 khung liên tiếp (64ms) vượt ngưỡng hoặc 1 khung rõ (>=0.52) để lọc nhiễu click
                    if onset_streak >= 2 or speech_prob >= 0.52:
                        speech_detected = True
                        self.is_capturing_speech = True
                        audio_buffer.extend(pre_speech_frames)
                        # Khung đệm trước chưa lưu xác suất VAD -> gán 0.0 (không ảnh hưởng vì vùng tìm điểm cắt bắt đầu từ 2.4s)
                        buffer_probs.extend([0.0] * (len(audio_buffer) - len(buffer_probs)))
                        pre_speech_frames.clear()
                        dip_frames = 0
                        rescue_frames.clear()
                        rescue_hint_frames = 0
                        rescue_quiet_frames = 0
                        idle_vad_frames = 0
                        voiced_frames = max(1, onset_streak)
                        silence_frames = 0
                        onset_streak = 0
                        if self.status_callback:
                            self.status_callback("🔊 Đang thu lời thoại... (Tự động cân bằng âm lượng & lọc nhạc nền)")
                        continue

                    # Cơ chế cứu hộ (Active-Audio Rescue): Khi video/cuộc gọi có âm thanh dải giọng nói liên tục
                    # nhưng nhạc nền trung âm hoặc giọng camcorder/thì thầm đè khiến Silero VAD không vượt ngưỡng
                    if vb_rms >= 0.0008:
                        rescue_frames.append(frame)
                        rescue_quiet_frames = 0
                        if speech_prob >= 0.04:
                            rescue_hint_frames += 1

                        should_rescue = (
                            (len(rescue_frames) >= rescue_soft_frames and speech_prob < 0.14)
                            or len(rescue_frames) >= rescue_max_frames
                        )
                        if should_rescue:
                            if rescue_hint_frames >= 6 and self.sentence_queue.empty():
                                rescue_audio = np.concatenate(rescue_frames).astype(np.float32, copy=False)
                                if float(np.sqrt(np.mean(np.square(rescue_audio)))) >= 0.0005:
                                    self.sentence_queue.put(rescue_audio)
                                overlap = rescue_frames[-min(6, len(rescue_frames)):]
                                rescue_frames = list(overlap)
                                pre_speech_frames.clear()
                                pre_speech_frames.extend(overlap)
                                rescue_hint_frames = 0
                                self.vad_model.reset_states()
                                idle_vad_frames = 0
                            else:
                                rescue_frames = rescue_frames[-min(60, len(rescue_frames)):]
                                rescue_hint_frames = min(rescue_hint_frames, 4)
                    else:
                        rescue_quiet_frames += 1
                        if rescue_quiet_frames >= 15:
                            if (
                                len(rescue_frames) >= 40
                                and rescue_hint_frames >= 10
                                and self.sentence_queue.empty()
                            ):
                                rescue_audio = np.concatenate(rescue_frames).astype(np.float32, copy=False)
                                if float(np.sqrt(np.mean(np.square(rescue_audio)))) >= 0.0005:
                                    self.sentence_queue.put(rescue_audio)
                            rescue_frames.clear()
                            rescue_hint_frames = 0
                            rescue_quiet_frames = 0
                    continue

                # Đang trong trạng thái thu câu nói (Hysteresis với ngưỡng thấp hơn speech_end_threshold)
                audio_buffer.append(frame)
                buffer_probs.append(speech_prob)
                if speech_prob >= self.speech_end_threshold:
                    voiced_frames += 1
                    silence_frames = 0
                else:
                    silence_frames += 1
                # Đếm nhịp ngắt hơi ngắn giữa các cụm từ (VAD tụt nhẹ < 0.45 dù chưa im lặng hẳn)
                if speech_prob < self.phrase_dip_prob:
                    dip_frames += 1
                else:
                    dip_frames = 0

                active_frames = max(1, len(audio_buffer) - silence_frames)
                active_sec = (active_frames * CHUNK_SAMPLES) / SAMPLE_RATE
                voiced_sec = (voiced_frames * CHUNK_SAMPLES) / SAMPLE_RATE
                silence_sec = (silence_frames * CHUNK_SAMPLES) / SAMPLE_RATE
                total_sec = (len(audio_buffer) * CHUNK_SAMPLES) / SAMPLE_RATE

                # Ngưỡng im lặng thích ứng tối ưu tốc độ thực tế cho cả phim ảnh & hội thoại
                if active_sec < 1.2:
                    required_silence_sec = self.short_pause_silence_sec
                elif active_sec < 3.8:
                    required_silence_sec = self.min_silence_sec
                else:
                    required_silence_sec = self.long_silence_sec

                pause_cut = silence_sec >= required_silence_sec
                # Podcast/hội thoại nói liền mạch: cắt tại nhịp ngắt hơi giữa các cụm từ thay vì chờ hết câu dài
                phrase_cut = (
                    active_sec >= self.phrase_cut_min_sec
                    and dip_frames >= self.phrase_dip_frames
                )
                soft_max_cut = (
                    active_sec >= self.soft_max_sentence_sec
                    and dip_frames >= 3
                )
                hard_max_cut = total_sec >= self.max_sentence_sec

                if pause_cut or phrase_cut or soft_max_cut or hard_max_cut:
                    carry_frames = []
                    carry_probs = []
                    if pause_cut and silence_frames > post_keep_frames:
                        trim_count = silence_frames - post_keep_frames
                        emit_frames = audio_buffer[:-trim_count]
                        for tail_f in audio_buffer[-min(trim_count, pre_roll_maxlen):]:
                            pre_speech_frames.append(tail_f)
                    elif hard_max_cut and not (phrase_cut or soft_max_cut or pause_cut):
                        # Cắt tại khung VAD thấp nhất (khe hở giữa 2 từ) trong vùng 2.4s..cuối, phần còn lại gối sang đoạn sau
                        search_from = max(1, int(self.phrase_cut_min_sec * SAMPLE_RATE / CHUNK_SAMPLES))
                        window = np.asarray(buffer_probs[search_from:], dtype=np.float32)
                        if len(window) >= 5:
                            # Làm mượt 3 khung (mode="valid" tránh hiệu ứng biên), chọn khe giữa từ MUỘN NHẤT có VAD thấp nhất
                            smooth = np.convolve(window, np.ones(3, dtype=np.float32) / 3.0, mode="valid")
                            near_min = np.flatnonzero(smooth <= float(smooth.min()) + 0.03)
                            cut_idx = search_from + int(near_min[-1]) + 2
                        else:
                            cut_idx = len(audio_buffer)
                        cut_idx = min(max(cut_idx, search_from), len(audio_buffer))
                        emit_frames = audio_buffer[:cut_idx]
                        carry_frames = audio_buffer[cut_idx:]
                        carry_probs = buffer_probs[cut_idx:]
                        # Gối đầu 1 khung (~32ms) đủ để không mất phụ âm đầu của từ kế tiếp mà không lặp lại từ biên
                        lead = audio_buffer[max(0, cut_idx - 1):cut_idx]
                        carry_frames = list(lead) + list(carry_frames)
                        carry_probs = list(buffer_probs[max(0, cut_idx - 1):cut_idx]) + list(carry_probs)
                    else:
                        emit_frames = audio_buffer
                        # Khi cắt tại nhịp ngắt giữa bài nói, giữ lại 1 khung (~32ms) gối đầu cho cụm kế tiếp
                        for overlap_f in audio_buffer[-min(1, len(audio_buffer)):]:
                            pre_speech_frames.append(overlap_f)

                    min_req = self.min_speech_sec if pause_cut else 0.18
                    if voiced_sec >= min_req and len(emit_frames) > 0:
                        full_sentence = np.concatenate(emit_frames).astype(np.float32, copy=False)
                        rms = float(np.sqrt(np.mean(np.square(full_sentence))))
                        if rms >= 0.0004:
                            self.sentence_queue.put(full_sentence)

                    rescue_frames.clear()
                    rescue_hint_frames = 0
                    rescue_quiet_frames = 0
                    idle_vad_frames = 0
                    onset_streak = 0
                    silence_frames = 0
                    dip_frames = 0
                    if carry_frames:
                        # Người nói vẫn đang nói tiếp: giữ nguyên trạng thái thu, nạp phần dư làm đầu đoạn mới
                        audio_buffer = list(carry_frames)
                        buffer_probs = list(carry_probs)
                        voiced_frames = sum(1 for p in buffer_probs if p >= self.speech_end_threshold)
                        speech_detected = True
                        self.is_capturing_speech = True
                        continue

                    audio_buffer = []
                    buffer_probs = []
                    speech_detected = False
                    self.is_capturing_speech = False
                    voiced_frames = 0
                    # Reset LSTM state khi dứt câu hoặc khi cell state tích lũy quá cao sau câu dài có nhạc nền
                    c_state = getattr(self.vad_model, "_c", None)
                    if pause_cut or (c_state is not None and float(np.max(np.abs(c_state))) > 15.0):
                        self.vad_model.reset_states()

            remainder = combined[offset:]

        # Xả nốt câu cuối nếu luồng kết thúc tự nhiên khi đang nói dở
        if self.running and speech_detected and len(audio_buffer) > 0:
            voiced_sec = (voiced_frames * CHUNK_SAMPLES) / SAMPLE_RATE
            if voiced_sec >= self.min_speech_sec:
                full_sentence = np.concatenate(audio_buffer).astype(np.float32, copy=False)
                if float(np.sqrt(np.mean(np.square(full_sentence)))) >= 0.0004:
                    self.sentence_queue.put(full_sentence)

    def _capture_windows_wasapi(self):
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        stream = None
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            if self.loopback_device_index is not None:
                default_speakers = p.get_device_info_by_index(self.loopback_device_index)
            else:
                default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                if not default_speakers["isLoopbackDevice"]:
                    for loopback in p.get_loopback_device_info_generator():
                        if default_speakers["name"] in loopback["name"]:
                            default_speakers = loopback
                            break

            self.log(f"🎧 Đang thu từ loa: {default_speakers['name']} (Hãy phát video/cuộc gọi ra đúng loa này)")
            native_rate = int(default_speakers["defaultSampleRate"])
            channels = int(default_speakers["maxInputChannels"])
            chunk_frames = max(512, native_rate // 50)

            resampler = _StreamingResampler(orig_rate=native_rate, target_rate=SAMPLE_RATE)
            silent_native = np.zeros(chunk_frames, dtype=np.float32)
            pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)

            def _wasapi_callback(in_data, frame_count, time_info, status):
                if self.running and in_data:
                    try:
                        pcm_queue.put_nowait(in_data)
                    except queue.Full:
                        try:
                            pcm_queue.get_nowait()
                            pcm_queue.put_nowait(in_data)
                        except queue.Empty:
                            pass
                return (None, pyaudio.paContinue)

            use_callback = True
            try:
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=native_rate,
                    frames_per_buffer=chunk_frames,
                    input=True,
                    input_device_index=default_speakers["index"],
                    stream_callback=_wasapi_callback,
                )
                stream.start_stream()
            except Exception as cb_err:
                logger.warning(f"WASAPI callback không khả dụng ({cb_err}), chuyển sang chế độ đọc polling an toàn.")
                use_callback = False
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=native_rate,
                    frames_per_buffer=chunk_frames,
                    input=True,
                    input_device_index=default_speakers["index"],
                )

            def generator():
                if use_callback:
                    while self.running:
                        try:
                            first = pcm_queue.get(timeout=0.12)
                        except queue.Empty:
                            # Chỉ khi toàn bộ Windows thực sự không phát gói âm thanh nào suốt 120ms
                            # mới đẩy khung tĩnh 20ms để xả câu cuối cùng (tuyệt đối không chèn 0 vào giữa lời thoại!)
                            yield resampler(silent_native)
                            continue

                        blobs = [first]
                        while len(blobs) < 6:
                            try:
                                blobs.append(pcm_queue.get_nowait())
                            except queue.Empty:
                                break
                        data = b"".join(blobs) if len(blobs) > 1 else first
                        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        if channels > 1:
                            pcm = pcm.reshape(-1, channels).mean(axis=1)
                        yield resampler(pcm)
                else:
                    empty_ticks = 0
                    while self.running:
                        avail = stream.get_read_available()
                        if avail <= 0:
                            time.sleep(0.020)
                            empty_ticks += 1
                            if empty_ticks >= 6:
                                empty_ticks = 0
                                yield resampler(silent_native)
                            continue
                        empty_ticks = 0
                        read_count = min(avail, chunk_frames * 6)
                        data = stream.read(read_count, exception_on_overflow=False)
                        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        if channels > 1:
                            pcm = pcm.reshape(-1, channels).mean(axis=1)
                        yield resampler(pcm)

            self._process_audio_stream(generator())
        except Exception as e:
            logger.exception("Lỗi thu âm WASAPI")
            self.log(f"Lỗi thu âm WASAPI: {e}")
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()

    def _capture_macos_or_linux(self):
        import soundcard as sc
        try:
            mics = sc.all_microphones(include_loopback=True)
            selected_mic = None
            for m in mics:
                name_lower = m.name.lower()
                if "blackhole" in name_lower or "loopback" in name_lower or "monitor" in name_lower:
                    selected_mic = m
                    break
            if selected_mic is None:
                selected_mic = sc.default_microphone()
                self.log(f"Chú ý macOS: Đang thu từ '{selected_mic.name}'. Hãy cài BlackHole 2ch để thu riêng âm thanh hệ thống.")
            else:
                self.log(f"Đang bắt âm thanh hệ thống: {selected_mic.name}")

            with selected_mic.recorder(samplerate=SAMPLE_RATE, channels=1) as recorder:
                def generator():
                    while self.running:
                        data = recorder.record(numframes=1024)
                        yield data.reshape(-1).astype(np.float32)
                self._process_audio_stream(generator())
        except Exception as e:
            logger.exception("Lỗi thu âm macOS/Linux")
            self.log(f"Lỗi thu âm macOS/Linux: {e}")


class MicPushToTalkRecorder:
    """Thu giọng nói tiếng Việt từ Micro thật khi người dùng giữ phím Push-to-Talk (F8)"""
    def __init__(self, outgoing_queue: queue.Queue, status_callback=None):
        self.outgoing_queue = outgoing_queue
        self.status_callback = status_callback
        self.is_recording = False
        self.frames = []
        self.stream = None
        self.mic_device_index = None  # None = Default Physical Mic

    @staticmethod
    def has_input_microphone() -> bool:
        try:
            devices = sd.query_devices()
            return any(d.get("max_input_channels", 0) > 0 for d in devices)
        except Exception:
            return False

    def _audio_callback(self, indata, frames, time_info, status):
        if self.is_recording:
            self.frames.append(indata.copy().reshape(-1))

    def start_recording(self):
        if self.is_recording:
            return
        if not self.has_input_microphone():
            if self.status_callback:
                self.status_callback("⚠️ Không tìm thấy Micro vật lý trên máy! Hãy cắm Mic/Tai nghe để nói tiếng Việt (F8).")
            return

        self.frames.clear()
        self.is_recording = True
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self.mic_device_index,
                callback=self._audio_callback
            )
            self.stream.start()
            if self.status_callback:
                self.status_callback("🎙️ ĐANG THU TIẾNG VIỆT... (Hãy nói vào Mic, thả F8 để dịch & phát tiếng Anh)")
        except Exception as e:
            self.is_recording = False
            logger.exception("Lỗi mở Micro")
            if self.status_callback:
                self.status_callback(f"⚠️ Lỗi mở Micro: {e}")

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass

        if len(self.frames) > 0:
            audio_np = np.concatenate(self.frames).astype(np.float32)
            duration = len(audio_np) / SAMPLE_RATE
            if duration >= 0.3:
                self.outgoing_queue.put(audio_np)
                if self.status_callback:
                    self.status_callback(f"⚡ Đang dịch câu nói của bạn ({duration:.1f}s) sang tiếng Anh...")
            else:
                if self.status_callback:
                    self.status_callback("Câu nói quá ngắn (< 0.3s), đã bỏ qua.")
