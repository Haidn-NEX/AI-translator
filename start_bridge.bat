@echo off
chcp 65001 >nul
title Local AI Live Translator — Chrome Bridge Server (127.0.0.1:47863)
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [LỖI] Chưa tìm thấy môi trường ảo venv\Scripts\python.exe!
    echo Hãy chạy setup_and_run.bat một lần trước để cài đặt môi trường.
    pause
    exit /b 1
)

set "PATH=%~dp0venv\Lib\site-packages\torch\lib;%LOCALAPPDATA%\Programs\Ollama;%PATH%"

echo ============================================================================
echo   Local AI Live Translator — Máy chủ Cầu nối Chrome Extension
echo   Địa chỉ: http://127.0.0.1:47863
echo   Nhấn Alt+Shift+T trên tab Chrome đang phát video để Bật/Tắt phụ đề.
echo   Nhấn Ctrl+C tại cửa sổ này nếu muốn dừng máy chủ thủ công.
echo ============================================================================
echo.

"venv\Scripts\python.exe" bridge_server.py %*
if errorlevel 1 (
    echo.
    echo [CẢNH BÁO] Máy chủ cầu nối kết thúc với mã lỗi %errorlevel%.
    pause
)

