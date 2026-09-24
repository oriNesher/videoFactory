@echo off
setlocal
cd /d "%~dp0"
echo Creating a private Python environment for ZoomPlanner...
py -m venv .venv
if errorlevel 1 goto :python_error
call .venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
call .venv\Scripts\python.exe -m pip install faster-whisper av
if errorlevel 1 goto :error
echo.
echo Installation completed successfully.
pause
exit /b 0
:python_error
echo ERROR: Python was not found. Install Python 3.10 or newer and enable Add Python to PATH.
pause
exit /b 1
:error
echo Installation failed. Copy the error message and send it to ChatGPT.
pause
exit /b 1
