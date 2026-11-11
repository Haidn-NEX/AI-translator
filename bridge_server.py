import argparse
import os
import signal
import sys
import threading
import requests
from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal
from hardware_profile import detect_hardware_profile
from ai_engine import LocalAIEngine
from incoming_pipeline import GLOSSARY_FILE, setup_logging
from browser_bridge import (
    BrowserBridgeServer,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    load_or_create_bridge_token,
)

logger = setup_logging()


def is_backend_already_running(host: str = DEFAULT_BRIDGE_HOST, port: int = DEFAULT_BRIDGE_PORT) -> bool:
    """Kiểm tra nhanh xem cổng đã có backend hợp lệ (app == local-live-translator) đang chạy chưa."""
    try:
        resp = requests.get(f"http://{host}:{port}/health", timeout=0.8)
        if resp.status_code == 200:
            data = resp.json()
            return bool(data.get("ok") and data.get("app") == "local-live-translator")
    except Exception:
        pass
    return False


class _QuitRelay(QObject):
    quit_requested = pyqtSignal()


def _preload_models_async(engine: LocalAIEngine) -> None:
    """Nạp sẵn model STT + Ollama + NLLB trong thread nền ngay khi khởi động Headless Bridge."""
    def _worker():
        try:
            logger.info("[Bridge] Bắt đầu nạp sẵn các model AI trên luồng nền (Headless Preload)...")
            engine.load_stt_model()
            if engine.translation_mode == "ollama":
                engine.warmup_ollama()
            engine._ensure_nllb_loaded("eng_Latn")
            engine._ensure_nllb_loaded("vie_Latn")
            logger.info("[Bridge] Đã nạp sẵn toàn bộ model AI lên VRAM (Headless sẵn sàng 100%).")
        except Exception as e:
            logger.exception(f"[Bridge] Lỗi khi nạp sẵn model ở chế độ Headless: {e}")

    threading.Thread(target=_worker, daemon=True, name="BridgePreloader").start()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local AI Live Translator — Headless Browser Bridge Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_BRIDGE_PORT,
        help=f"Cổng lắng nghe trên 127.0.0.1 (mặc định: {DEFAULT_BRIDGE_PORT})",
    )
    parser.add_argument(
        "--idle-exit",
        type=float,
        default=900.0,
        help="Tự động thoát sau N giây không có phiên dịch để giải phóng VRAM (mặc định: 900s = 15 phút)",
    )
    parser.add_argument(
        "--no-idle-exit",
        action="store_true",
        help="Tắt cơ chế tự thoát khi rảnh",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="Không tự nạp model ngay khi khởi động (chỉ nạp khi có phiên đầu tiên)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    port = int(args.port)

    # Nếu đã có backend hợp lệ đang chạy trên cổng này -> thoát ngay mã 0 (tránh tốn gấp đôi VRAM)
    if is_backend_already_running(DEFAULT_BRIDGE_HOST, port):
        logger.info(
            f"[Bridge] Phát hiện backend 'local-live-translator' đã chạy tại 127.0.0.1:{port}. "
            f"Tiến trình Headless mới tự thoát (code 0)."
        )
        return 0

    app = QCoreApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    quit_relay = _QuitRelay()
    quit_relay.quit_requested.connect(app.quit)

    logger.info(f"[Bridge] Khởi động Headless Bridge Server trên 127.0.0.1:{port}...")
    profile = detect_hardware_profile()
    logger.info(f"[Bridge] Cấu hình phần cứng: {profile}")
    engine = LocalAIEngine(profile)

    if os.path.isfile(GLOSSARY_FILE):
        try:
            with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                saved_glossary = f.read().strip()
            if saved_glossary:
                engine.set_custom_glossary(saved_glossary)
        except Exception as e:
            logger.warning(f"[Bridge] Không đọc được custom_glossary.txt: {e}")

    token = load_or_create_bridge_token()
    idle_sec = None if args.no_idle_exit else float(args.idle_exit)

    server = BrowserBridgeServer(
        engine=engine,
        host=DEFAULT_BRIDGE_HOST,
        port=port,
        token=token,
        mode="headless",
        on_idle_timeout=lambda: quit_relay.quit_requested.emit(),
        idle_exit_sec=idle_sec,
    )

    if not server.start(wait_ready=True, timeout=5.0):
        logger.warning(f"[Bridge] Không thể bind cổng 127.0.0.1:{port}. Thoát Headless Server.")
        return 0 if is_backend_already_running(DEFAULT_BRIDGE_HOST, port) else 1

    if not args.no_preload:
        _preload_models_async(engine)

    # Cho phép Ctrl+C trên console Windows đánh thức vòng lặp Qt để thoát sạch
    sig_timer = QTimer()
    sig_timer.start(500)
    sig_timer.timeout.connect(lambda: None)

    def _sig_handler(signum, frame):
        logger.info(f"[Bridge] Nhận tín hiệu dừng ({signum}), đang đóng Headless Server...")
        quit_relay.quit_requested.emit()

    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)

    exit_code = app.exec()
    server.stop()
    logger.info("[Bridge] Headless Bridge Server đã thoát hoàn toàn.")
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())

