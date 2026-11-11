import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import wave
import aiohttp
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from PyQt6.QtCore import QCoreApplication
from audio_capture import _StreamingResampler
from hardware_profile import detect_hardware_profile
from ai_engine import LocalAIEngine
from browser_bridge import BrowserBridgeServer, load_or_create_bridge_token
from incoming_pipeline import MEETING_LOGS_DIR, setup_logging

logger = setup_logging()


def prepare_48k_wav_from_media(src_media: str, out_wav: str, min_duration_sec: float = 61.0) -> float:
    """Giải mã file media (VD: meeting_logs/a.mp4) thành file WAV 48kHz Int16 mono dài >= 60s."""
    import av

    container = av.open(src_media)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    target_samples = int(min_duration_sec * 48000)
    collected = 0
    chunks: list[bytes] = []

    for frame in container.decode(audio=0):
        out_frames = resampler.resample(frame)
        for of in out_frames:
            raw = bytes(of.planes[0])
            n_samples = of.samples
            raw = raw[: n_samples * 2]
            chunks.append(raw)
            collected += n_samples
        if collected >= target_samples:
            break
    container.close()

    pcm_bytes = b"".join(chunks)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(pcm_bytes)

    return collected / 48000.0


def verify_resampler_rates() -> None:
    """Kiểm tra _StreamingResampler hoạt động chính xác với 16000Hz (identity), 44100Hz và 48000Hz."""
    t_1s = np.linspace(0, 1.0, 16000, endpoint=False, dtype=np.float32)
    sig_16k = (0.3 * np.sin(2 * np.pi * 440.0 * t_1s)).astype(np.float32)
    r_16k = _StreamingResampler(orig_rate=16000, target_rate=16000)
    out_16k = r_16k(sig_16k)
    assert out_16k.dtype == np.float32 and len(out_16k) == 16000, "Resampler 16kHz identity lỗi"
    assert np.allclose(out_16k, sig_16k, atol=1e-6), "Resampler 16kHz không giữ nguyên mẫu"

    for rate in (44100, 48000):
        r = _StreamingResampler(orig_rate=rate, target_rate=16000)
        t = np.linspace(0, 1.0, rate, endpoint=False, dtype=np.float32)
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        outs = []
        step = rate // 25  # 40ms chunks
        for i in range(0, len(sig), step):
            c = r(sig[i : i + step])
            if len(c) > 0:
                outs.append(c)
        merged = np.concatenate(outs)
        assert merged.dtype == np.float32
        assert 15000 <= len(merged) <= 16100, f"Resampler {rate}Hz -> 16kHz sai độ dài: {len(merged)}"
    logger.info("[Test] _StreamingResampler đạt 100% cho 16000Hz, 44100Hz, 48000Hz.")


async def run_ws_client_test(
    port: int,
    token: str,
    wav_path: str,
    realtime_speed: float = 1.0,
    early_stop_after_results: int = 0,
) -> None:
    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    ext_headers = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}

    async with aiohttp.ClientSession() as session:
        # 1. Kiểm tra /health
        async with session.get(f"{base_url}/health", headers=ext_headers) as resp:
            assert resp.status == 200, f"/health trả về {resp.status}"
            health = await resp.json()
            logger.info(f"[Test] GET /health OK: {health}")
            assert health["ok"] is True
            assert health["app"] == "local-live-translator"
            assert health["version"] == "1.0"

        # 2. Kiểm tra bảo mật: Origin lạ bị chặn 403
        async with session.get(
            f"{base_url}/health", headers={"Origin": "https://malicious.example.com"}
        ) as resp:
            assert resp.status == 403, f"/health với Origin lạ phải trả 403, nhưng nhận {resp.status}"

        # 3. Kiểm tra bảo mật: Token sai trên /ws phải trả 403
        async with session.get(f"{ws_url}?token=wrong_secret_token", headers=ext_headers) as resp:
            assert resp.status == 403, f"/ws với token sai phải trả 403, nhưng nhận {resp.status}"
            logger.info("[Test] Kiểm tra bảo mật token sai -> 403 Forbidden: ĐẠT.")

        # 4. Kiểm tra bảo mật: Origin không phải chrome-extension:// trên /ws phải trả 403
        async with session.get(
            f"{ws_url}?token={token}", headers={"Origin": "https://youtube.com"}
        ) as resp:
            assert resp.status == 403, f"/ws với Origin web thường phải trả 403, nhưng nhận {resp.status}"
            logger.info("[Test] Kiểm tra bảo mật Origin web thường -> 403 Forbidden: ĐẠT.")

        # 5. Kết nối WebSocket hợp lệ & stream WAV 48kHz
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            total_frames = wf.getnframes()
            raw_pcm = wf.readframes(total_frames)

        assert sampwidth == 2, "File WAV test phải là Int16 (16-bit PCM)"
        if channels > 1:
            arr = np.frombuffer(raw_pcm, dtype=np.int16).reshape(-1, channels).mean(axis=1).astype(np.int16)
            raw_pcm = arr.tobytes()

        dur_sec = (len(raw_pcm) // 2) / float(sr)
        logger.info(f"[Test] Chuẩn bị stream file WAV: {dur_sec:.1f}s @ {sr}Hz mono Int16...")

        sources_received: list[str] = []
        results_received: list[dict] = []
        statuses_received: list[dict] = []
        ready_event = asyncio.Event()
        stopped_event = asyncio.Event()

        async with session.ws_connect(f"{ws_url}?token={token}", headers=ext_headers) as ws:
            # Gửi hello
            await ws.send_json({"type": "hello", "client": "chrome-ext", "version": "1.0.0"})
            hello_msg = await ws.receive_json(timeout=5.0)
            assert hello_msg.get("type") == "hello_ack", f"Kỳ vọng hello_ack, nhận: {hello_msg}"
            logger.info(f"[Test] Nhận hello_ack: {hello_msg}")

            # Gửi ping
            await ws.send_json({"type": "ping", "t": 123456})
            pong_msg = await ws.receive_json(timeout=5.0)
            assert pong_msg.get("type") == "pong" and pong_msg.get("t") == 123456

            # Bắt đầu phiên dịch
            await ws.send_json(
                {
                    "type": "start",
                    "session_id": "test-session-p1",
                    "sample_rate": sr,
                    "tab_title": "Test Video P1 - English Speech",
                    "tab_url": "https://www.youtube.com/watch?v=test_p1",
                    "source_lang": "en",
                    "save_transcript": True,
                }
            )

            async def _receiver():
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        mtype = data.get("type")
                        if mtype == "status":
                            statuses_received.append(data)
                            logger.info(f"[Test][WS status ({data.get('level')})] {data.get('message')}")
                            if data.get("level") == "ready":
                                ready_event.set()
                        elif mtype == "source":
                            src_txt = data.get("text", "")
                            sources_received.append(src_txt)
                            logger.info(f"[Test][WS source] {src_txt}")
                        elif mtype == "result":
                            results_received.append(data)
                            logger.info(
                                f"[Test][WS result ({data.get('engine')})] "
                                f"EN ({data.get('stt_ms')}ms): {data.get('src')} => "
                                f"VI ({data.get('trans_ms')}ms): {data.get('vi')}"
                            )
                        elif mtype == "stopped":
                            logger.info(f"[Test][WS stopped] {data}")
                            stopped_event.set()
                            break

            recv_task = asyncio.create_task(_receiver())

            # Đợi model nạp xong trước khi đẩy âm thanh
            logger.info("[Test] Đang chờ worker báo trạng thái ready (nạp model AI)...")
            await asyncio.wait_for(ready_event.wait(), timeout=120.0)

            # Stream các khung 40ms (1920 mẫu @ 48kHz = 3840 bytes) ở tốc độ thời gian thực
            frame_samples = int(sr * 0.04)
            frame_bytes = frame_samples * 2
            sleep_sec = 0.04 / max(0.1, float(realtime_speed))
            t_start = time.perf_counter()

            for offset in range(0, len(raw_pcm), frame_bytes):
                chunk = raw_pcm[offset : offset + frame_bytes]
                await ws.send_bytes(chunk)
                await asyncio.sleep(sleep_sec)
                if early_stop_after_results > 0 and len(results_received) >= early_stop_after_results:
                    break

            elapsed_stream = time.perf_counter() - t_start
            logger.info(f"[Test] Đã gửi xong luồng PCM sau {elapsed_stream:.1f}s. Chờ xử lý câu cuối...")

            # Chờ thêm tối đa 8s để câu cuối cùng được VAD xả và dịch xong
            for _ in range(16):
                if len(results_received) >= 1 and len(sources_received) >= 1:
                    await asyncio.sleep(1.0)
                    break
                await asyncio.sleep(0.5)

            await ws.send_json({"type": "stop", "session_id": "test-session-p1"})
            try:
                await asyncio.wait_for(stopped_event.wait(), timeout=6.0)
            except asyncio.TimeoutError:
                pass
            recv_task.cancel()

        assert len(sources_received) >= 1, "Chưa nhận được sự kiện 'source' nào!"
        assert len(results_received) >= 1, "Chưa nhận được sự kiện 'result' nào!"
        first_res = results_received[0]
        assert first_res.get("src") and first_res.get("vi"), f"Sự kiện result thiếu nội dung: {first_res}"
        logger.info(
            f"[Test] KẾT QUẢ P1 ĐẠT 100%: Nhận {len(sources_received)} sự kiện 'source' "
            f"và {len(results_received)} sự kiện 'result'."
        )


def main():
    parser = argparse.ArgumentParser(description="Kiểm thử tự động P1 — BrowserBridgeServer WebSocket")
    parser.add_argument("--audio", type=str, default=None, help="Đường dẫn file WAV/MP4 dùng để test")
    parser.add_argument("--port", type=int, default=47869, help="Cổng test cục bộ (mặc định 47869)")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Tốc độ phát PCM (1.0 = thời gian thực đúng chuẩn nghiệm thu)",
    )
    args = parser.parse_args()

    verify_resampler_rates()

    # Kiểm tra bảo mật bind 0.0.0.0 phải bị từ chối
    app = QCoreApplication(sys.argv)
    profile = detect_hardware_profile()
    engine = LocalAIEngine(profile)

    try:
        BrowserBridgeServer(engine, host="0.0.0.0", port=args.port)
        raise AssertionError("BrowserBridgeServer không được phép bind 0.0.0.0!")
    except ValueError:
        logger.info("[Test] Kiểm tra chặn bind 0.0.0.0: ĐẠT.")

    token = load_or_create_bridge_token()
    server = BrowserBridgeServer(
        engine=engine,
        host="127.0.0.1",
        port=args.port,
        token=token,
        mode="headless",
    )
    assert server.start(wait_ready=True, timeout=5.0), f"Không thể khởi động server trên cổng {args.port}"

    tmp_wav = None
    try:
        wav_path = args.audio
        if not wav_path:
            default_mp4 = os.path.join(MEETING_LOGS_DIR, "a.mp4")
            if os.path.isfile(default_mp4):
                fd, tmp_wav = tempfile.mkstemp(suffix="_48k_test.wav")
                os.close(fd)
                dur = prepare_48k_wav_from_media(default_mp4, tmp_wav, min_duration_sec=61.0)
                logger.info(f"[Test] Đã trích xuất file WAV 48kHz ({dur:.1f}s) từ meeting_logs/a.mp4 -> {tmp_wav}")
                wav_path = tmp_wav
            else:
                raise FileNotFoundError("Không tìm thấy meeting_logs/a.mp4, hãy truyền --audio <path.wav>")

        asyncio.run(
            run_ws_client_test(
                port=args.port,
                token=token,
                wav_path=wav_path,
                realtime_speed=args.speed,
            )
        )
    finally:
        server.stop()
        if tmp_wav and os.path.isfile(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass


if __name__ == "__main__":
    main()

