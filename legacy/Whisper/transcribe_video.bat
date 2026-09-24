@echo off
setlocal
cd /d "%~dp0"

REM ==============================
REM Subtitle settings
REM ==============================

set "MODEL=medium"
set "DEVICE=cpu"
set "LANGUAGE=he"

REM Maximum characters displayed in one subtitle
set "MAX_CHARS=16"

REM Maximum number of words in one subtitle
set "MAX_WORDS=5"

REM ==============================

if "%~1"=="" (
    echo Drag a video or audio file onto this BAT file.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Whisper is not installed yet.
    echo Run install_whisper.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" transcribe.py "%~1" ^
    --model %MODEL% ^
    --device %DEVICE% ^
    --language %LANGUAGE% ^
    --max-characters %MAX_CHARS% ^
    --max-words %MAX_WORDS%

echo.
pause