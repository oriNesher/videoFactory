@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag the folder containing the SEPARATE TRIMMED CLIPS onto this file.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Run install_zoom_planner.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" create_zoom_input.py "%~1" --model medium --device cpu --language he
echo.
pause
