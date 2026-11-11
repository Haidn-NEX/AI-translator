import json
import logging
import os
import platform
import secrets
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HOST_DIR)
LOG_FILE = os.path.join(HOST_DIR, "launcher.log")
TOKEN_FILE = os.path.join(APP_DIR, "bridge_token.txt")
DEFAULT_PORT = 47863
SCHEDULED_TASK_NAME = "LocalTranslatorBridge"

# Tuyệt đối KHÔNG ghi log ra stdout vì stdout dành riêng cho giao thức nhị phân Native Messaging của Chrome
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("NativeLauncher")


def _set_binary_stdio() -> None:
    if platform.system() == "Windows":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def _read_native_message() -> dict | None:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len <= 0 or msg_len > 1024 * 1024:
        return None
    payload = sys.stdin.buffer.read(msg_len)
    if not payload or len(payload) < msg_len:
        return None
    return json.loads(payload.decode("utf-8"))


def _send_native_message(data: dict) -> None:
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _load_or_create_token() -> str:
    if os.path.isfile(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if len(tok) >= 16:
                return tok
        except Exception as e:
            logger.warning(f"Không đọc được {TOKEN_FILE}: {e}")

    tok = secrets.token_urlsafe(24)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    except Exception as e:
        logger.warning(f"Không ghi được {TOKEN_FILE}: {e}")
    return tok


def _check_backend_health(port: int = DEFAULT_PORT, timeout: float = 0.45) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                return bool(body.get("ok") and body.get("app") == "local-live-translator")
    except Exception:
        pass
    return False


def _spawn_headless_backend(port: int = DEFAULT_PORT, idle_exit: int = 900) -> str:
    """
    Khởi chạy bridge_server.py hoàn toàn tách rời khỏi cây tiến trình của Native Host:
    1. Thử Popen với CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW.
    2. Nếu Job Object của Chrome chặn breakaway (PermissionError / WinError 5), gọi Scheduled Task
       'LocalTranslatorBridge' (chạy dưới svchost Task Scheduler ngoài Job Object của Chrome) + fallback DETACHED_PROCESS.
    """
    pythonw_exe = os.path.join(APP_DIR, "venv", "Scripts", "pythonw.exe")
    python_exe = os.path.join(APP_DIR, "venv", "Scripts", "python.exe")
    exe = pythonw_exe if os.path.isfile(pythonw_exe) else (python_exe if os.path.isfile(python_exe) else sys.executable)
    bridge_script = os.path.join(APP_DIR, "bridge_server.py")

    cmd = [exe, bridge_script, "--port", str(port), "--idle-exit", str(idle_exit)]

    child_env = os.environ.copy()
    torch_lib = os.path.join(APP_DIR, "venv", "Lib", "site-packages", "torch", "lib")
    ollama_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama")
    extra_paths = [p for p in (torch_lib, ollama_dir) if os.path.isdir(p)]
    if extra_paths:
        child_env["PATH"] = os.pathsep.join(extra_paths + [child_env.get("PATH", "")])

    if platform.system() != "Windows":
        subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return "posix_new_session"

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    CREATE_NO_WINDOW = 0x08000000

    # Bước 1: Thử breakaway trực tiếp khỏi Job Object
    try:
        subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW,
            close_fds=True,
        )
        logger.info(f"Đã spawn bridge_server.py bằng CREATE_BREAKAWAY_FROM_JOB (port={port}).")
        return "breakaway_job"
    except OSError as e:
        logger.warning(f"CREATE_BREAKAWAY_FROM_JOB bị từ chối ({e}), chuyển sang Scheduled Task / Detached fallback...")

    # Bước 2: Thử kích hoạt Scheduled Task (chạy ngoài cây tiến trình Chrome)
    if port == DEFAULT_PORT:
        try:
            res = subprocess.run(
                ["schtasks", "/Run", "/TN", SCHEDULED_TASK_NAME],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=2.5,
            )
            if res.returncode == 0:
                logger.info(f"Đã kích hoạt Scheduled Task '{SCHEDULED_TASK_NAME}' thành công.")
                return "scheduled_task"
        except Exception as st_err:
            logger.warning(f"Không gọi được Scheduled Task '{SCHEDULED_TASK_NAME}': {st_err}")

    # Bước 3: Fallback DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        cmd,
        cwd=APP_DIR,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )
    logger.info(f"Đã spawn bridge_server.py bằng DETACHED_PROCESS (port={port}).")
    return "detached_process"


def handle_command(req: dict) -> dict:
    cmd = req.get("cmd")
    port = int(req.get("port") or DEFAULT_PORT)
    token = _load_or_create_token()

    if cmd == "get_token":
        return {"ok": True, "port": port, "token": token}

    if cmd == "ensure_running":
        if _check_backend_health(port=port, timeout=0.45):
            logger.info(f"ensure_running: Backend đã chạy sẵn trên cổng {port}.")
            return {
                "ok": True,
                "already_running": True,
                "spawned": False,
                "port": port,
                "token": token,
            }

        method = _spawn_headless_backend(port=port, idle_exit=int(req.get("idle_exit") or 900))
        return {
            "ok": True,
            "already_running": False,
            "spawned": True,
            "method": method,
            "port": port,
            "token": token,
        }

    return {"ok": False, "error": f"Lệnh không hợp lệ: {cmd!r}"}


def main() -> int:
    t0 = time.perf_counter()
    try:
        _set_binary_stdio()
        req = _read_native_message()
        if req is None:
            return 0
        logger.info(f"Nhận lệnh từ Chrome Extension: {req}")
        resp = handle_command(req)
        _send_native_message(resp)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(f"Đã phản hồi trong {elapsed_ms:.1f}ms: ok={resp.get('ok')}")
        return 0
    except Exception as e:
        logger.exception("Lỗi trong Native Messaging Launcher")
        try:
            _send_native_message({"ok": False, "error": str(e)})
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

