@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

echo [1/4] Kiem tra moi truong Python venv...
if not exist "venv\Scripts\python.exe" (
    python -m venv venv
)
call "venv\Scripts\activate.bat"

python -c "import torch, PyQt6, faster_whisper, ctranslate2, pyaudiowpatch" >nul 2>nul
if %errorlevel% neq 0 (
    echo [2/4] Cai dat PyTorch CUDA va cac thu vien cho GTX 1070...
    pip install --upgrade pip
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    pip install -r requirements_win.txt
) else (
    echo [2/4] Cac thu vien Python da duoc cai dat day du trong venv.
)

echo [3/4] Kiem tra va tu dong cai dat Ollama + Model LLM...
call :EnsureOllama

echo [4/4] Khoi chay Local AI 2-Way Live Translator...
python main.py
if %errorlevel% neq 0 (
    echo [Loi] Ung dung ket thuc voi ma loi %errorlevel%. Xem chi tiet trong app_debug.log
    pause
)
exit /b 0


:EnsureOllama
:: Bo sung duong dan cai dat mac dinh cua Ollama tren Windows vao PATH phien hien tai
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
)

where ollama >nul 2>nul
if %errorlevel%==0 goto :EnsureOllamaServer

echo       Chua tim thay Ollama tren may. Bat dau tu dong tai va cai dat Ollama...

:: Uu tien 1: Cai dat ngam qua Windows Package Manager (winget)
where winget >nul 2>nul
if %errorlevel%==0 (
    echo       [3.1] Dang cai dat Ollama tu dong qua winget...
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent
)

if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
)
where ollama >nul 2>nul
if %errorlevel%==0 goto :EnsureOllamaServer

:: Uu tien 2: Fallback tai truc tiep bo cai OllamaSetup.exe tu ollama.com
echo       [3.1] Dang tai bo cai chinh thuc OllamaSetup.exe tu https://ollama.com ...
curl.exe -fL --progress-bar "https://ollama.com/download/OllamaSetup.exe" -o "%TEMP%\OllamaSetup.exe"
if exist "%TEMP%\OllamaSetup.exe" (
    echo       [3.2] Dang chay cai dat ngam OllamaSetup.exe ^(vui long doi 15-30 giay^)...
    "%TEMP%\OllamaSetup.exe" /SILENT /NORESTART
    del /q "%TEMP%\OllamaSetup.exe" >nul 2>nul
)

if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
)
where ollama >nul 2>nul
if %errorlevel%==0 goto :EnsureOllamaServer

echo       [Canh bao] Khong the tu dong cai dat Ollama ^(vui long kiem tra ket noi Internet^).
echo       Ung dung van se khoi chay binh thuong voi model NLLB-200 INT8 sieu nhe.
exit /b 0


:EnsureOllamaServer
:: Kiem tra xem dich vu Ollama server (127.0.0.1:11434) da chay chua; neu chua thi tu khoi dong ngam
:: Tren GPU Pascal (GTX 1070 SM 6.1), tu dong bat OLLAMA_VULKAN=1 cho tien trinh ollama serve de tranh loi xung dot CUDA PTX voi driver cu
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=1)" >nul 2>nul
if %errorlevel% neq 0 (
    echo       Dang khoi dong dich vu ngam Ollama server ^(127.0.0.1:11434^)...
    python -c "import os, subprocess; from hardware_profile import detect_hardware_profile; env = os.environ.copy(); prof = detect_hardware_profile(); is_pascal = ('SM 6.' in prof.device_name); env.update({'OLLAMA_VULKAN': '1', 'CUDA_VISIBLE_DEVICES': '-1'} if is_pascal else {}); subprocess.Popen(['ollama', 'serve'], env=env, creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)" >nul 2>nul
    for /L %%I in (1,1,15) do (
        python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=1)" >nul 2>nul
        if not errorlevel 1 goto :PullOllamaModel
        timeout /t 1 /nobreak >nul
    )
)

:PullOllamaModel
set "REC_LLM=qwen2.5:3b"
for /f "usebackq delims=" %%M in (`python -c "from hardware_profile import detect_hardware_profile; print(detect_hardware_profile().recommended_llm)" 2^>nul`) do (
    set "REC_LLM=%%M"
)

python -c "import json, sys, urllib.request; data = json.loads(urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2).read()); models = [m.get('name','') for m in data.get('models',[])]; sys.exit(0 if any(m == '%REC_LLM%' or m.startswith('%REC_LLM%:') for m in models) else 1)" >nul 2>nul
if %errorlevel%==0 (
    echo       [OK] Ollama server va model %REC_LLM% da san sang!
) else (
    echo       Dang tai model dich thuat %REC_LLM% ^(chi tai 1 lan dau^)...
    ollama pull %REC_LLM%
)
exit /b 0
