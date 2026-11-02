import asyncio
import os
import tempfile
import threading
import time
import sounddevice as sd
import soundfile as sf


class VirtualMicTTSRouter:
    def __init__(self):
        self.virtual_mic_index = self.auto_detect_virtual_cable()
        self.tts_engine_mode = "neural_edge"  # "neural_edge" (giọng cực tự nhiên) hoặc "offline_native" (100% offline)
        self.voice_name = "en-US-AndrewNeural"
        self.monitor_to_speaker = False       # Nếu True, phát cho cả bạn nghe thấy giọng tiếng Anh của mình
        self._is_playing = False

    @staticmethod
    def list_output_devices() -> list[tuple[int, str]]:
        devices = sd.query_devices()
        out = []
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] > 0:
                out.append((idx, dev["name"]))
        return out

    @classmethod
    def auto_detect_virtual_cable(cls) -> int | None:
        """Tự động tìm VB-Audio Virtual Cable (Windows) hoặc BlackHole (macOS)"""
        for idx, name in cls.list_output_devices():
            low = name.lower()
            if "cable input" in low or "vb-audio" in low or "blackhole" in low:
                return idx
        return None

    def stop_playback(self):
        """Ngắt ngay lập tức âm thanh TTS đang phát vào Virtual Mic khi người dùng bấm Esc"""
        if self._is_playing:
            try:
                sd.stop()
            except Exception:
                pass
            self._is_playing = False

    def speak_to_virtual_mic(self, english_text: str, cancel_event: threading.Event = None) -> float:
        """
        Tổng hợp giọng nói tiếng Anh và đẩy vào Virtual Mic.
        Hỗ trợ hủy tức thì bằng phím Esc (cancel_event) ngay cả khi đang tổng hợp hoặc đang phát âm thanh.
        Trả về thời gian tổng hợp TTS (ms).
        """
        if not english_text:
            return 0.0
        if cancel_event is not None and cancel_event.is_set():
            return 0.0

        t0 = time.perf_counter()
        fd, tmp_file = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            if self.tts_engine_mode == "neural_edge":
                try:
                    self._synthesize_edge_tts(english_text, tmp_file)
                except Exception:
                    # Tự động fallback về Offline Native TTS nếu mất mạng
                    self._synthesize_offline_tts(english_text, tmp_file)
            else:
                self._synthesize_offline_tts(english_text, tmp_file)

            tts_ms = (time.perf_counter() - t0) * 1000.0
            if cancel_event is not None and cancel_event.is_set():
                return tts_ms

            # Đọc file âm thanh và đẩy vào thiết bị Virtual Cable theo chế độ có thể ngắt bằng Esc
            data, sr = sf.read(tmp_file, dtype="float32")
            if len(data) == 0 or sr <= 0:
                return tts_ms

            target_device = self.virtual_mic_index  # Nếu None sẽ phát ra loa mặc định để test
            duration_sec = len(data) / float(sr)

            self._is_playing = True
            try:
                sd.play(data, samplerate=sr, device=target_device, blocking=False)
                play_start = time.perf_counter()
                while (time.perf_counter() - play_start) < duration_sec:
                    if cancel_event is not None and cancel_event.is_set():
                        sd.stop()
                        break
                    time.sleep(0.04)
                if cancel_event is None or not cancel_event.is_set():
                    sd.wait()
            finally:
                self._is_playing = False

            return tts_ms
        finally:
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass

    def _synthesize_edge_tts(self, text: str, out_path: str):
        import edge_tts
        async def _run():
            communicate = edge_tts.Communicate(text, self.voice_name, rate="+8%")
            await communicate.save(out_path)
        asyncio.run(_run())

    def _synthesize_offline_tts(self, text: str, out_path: str):
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        # Chọn giọng tiếng Anh nếu hệ điều hành có cài sẵn
        for v in engine.getProperty("voices"):
            if "en" in v.id.lower() or "english" in v.name.lower() or "david" in v.name.lower() or "zira" in v.name.lower():
                engine.setProperty("voice", v.id)
                break
        engine.save_to_file(text, out_path)
        engine.runAndWait()

