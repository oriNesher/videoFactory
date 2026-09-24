@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Drag either:
    echo 1. The folder containing the separate trimmed clips and zoom_plan.json
    echo OR
    echo 2. zoom_plan.json when it is stored beside the clips
    echo.
    pause
    exit /b 1
)

py -3 render_zoom_plan.py "%~1"

echo.
pause
