@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"


REM ============================================================
REM                     CUT SETTINGS
REM ============================================================

REM רגישות לזיהוי דיבור.
REM 0.02 = 2%.
REM נמוך יותר = שומר יותר אודיו.
REM גבוה יותר = חותך יותר.
set "AUDIO_THRESHOLD=0.04"

REM כמה זמן להשאיר לפני תחילת הדיבור המזוהה.
set "MARGIN_BEFORE=0.00s"

REM כמה זמן להשאיר אחרי סוף הדיבור המזוהה.
set "MARGIN_AFTER=0.50s"

REM נמוך יותר = חיתוך אגרסיבי יותר כי שקט קצר יותר גם ייחתך
set "MIN_SILENCE=0.10s"

REM גבוה יותר = חיתוך אגרסיבי יותר כי קטעי דיבור קצרים יותר לא ייחשבו משמעותיים
set "MIN_SPEECH=0.60s"


REM ============================================================
REM                     FILE SETTINGS
REM ============================================================

set "TAKES=takes"
set "OUTPUT=output"
set "TRIMMED=output\trimmed"
set "FINAL_DIR=output\final"
set "FINAL=output\final\_video.mp4"

set "LIST=output\clips.txt"
set "SORTED_LIST=output\sorted_takes.txt"


REM ============================================================
REM                         START
REM ============================================================

echo.
echo ========================================
echo Video Auto Editor
echo ========================================
echo.

echo Script folder:
echo %CD%
echo.

echo Cut settings:
echo Threshold:     %AUDIO_THRESHOLD%
echo Margin before: %MARGIN_BEFORE%
echo Margin after:  %MARGIN_AFTER%
echo Min silence:   %MIN_SILENCE%
echo Min speech:    %MIN_SPEECH%
echo.


REM ============================================================
REM                     CHECK PROGRAMS
REM ============================================================

where auto-editor.exe >nul 2>&1

if errorlevel 1 (
    echo ERROR:
    echo auto-editor.exe was not found.
    echo.
    goto ERROR_END
)

where ffmpeg.exe >nul 2>&1

if errorlevel 1 (
    echo ERROR:
    echo ffmpeg.exe was not found.
    echo.
    goto ERROR_END
)


REM ============================================================
REM                      CHECK TAKES
REM ============================================================

if not exist "%TAKES%" (
    echo ERROR:
    echo The "%TAKES%" folder does not exist.
    echo.
    goto ERROR_END
)


REM ============================================================
REM                    CREATE FOLDERS
REM ============================================================

if not exist "%OUTPUT%" mkdir "%OUTPUT%"

if exist "%TRIMMED%" (
    rmdir /s /q "%TRIMMED%"
)

mkdir "%TRIMMED%"

if not exist "%FINAL_DIR%" (
    mkdir "%FINAL_DIR%"
)


if exist "%FINAL%" del /q "%FINAL%"
if exist "%LIST%" del /q "%LIST%"
if exist "%SORTED_LIST%" del /q "%SORTED_LIST%"


REM ============================================================
REM                    SORT INPUT FILES
REM ============================================================

echo ========================================
echo Finding and sorting MKV files
echo ========================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%TAKES%' -Filter '*.mkv' | Sort-Object { if ($_.BaseName -match '^\d+$') { [int]$_.BaseName } else { [int]::MaxValue } }, Name | ForEach-Object { $_.FullName }" > "%SORTED_LIST%"

if errorlevel 1 (
    echo ERROR:
    echo PowerShell failed while sorting the files.
    echo.
    goto ERROR_END
)


REM Check if list is empty

for %%A in ("%SORTED_LIST%") do (
    if %%~zA EQU 0 (
        echo ERROR:
        echo No MKV files were found inside "%TAKES%".
        echo.
        goto ERROR_END
    )
)


REM ============================================================
REM                       AUTO EDITOR
REM ============================================================

echo ========================================
echo Processing clips
echo ========================================
echo.

set /a COUNT=0

for /f "usebackq delims=" %%F in ("%SORTED_LIST%") do (

    set /a COUNT+=1

    set "NUMBER=0000!COUNT!"
    set "NUMBER=!NUMBER:~-4!"

    echo ----------------------------------------
    echo Processing: %%~nxF
    echo ----------------------------------------

    auto-editor.exe "%%F" ^
        --edit audio:%AUDIO_THRESHOLD% ^
        --margin %MARGIN_BEFORE%,%MARGIN_AFTER% ^
        --smooth %MIN_SILENCE%,%MIN_SPEECH% ^
        -o "%TRIMMED%\!NUMBER!_%%~nF_trimmed.mp4"

    if errorlevel 1 (
        echo.
        echo ERROR:
        echo Auto-Editor failed while processing:
        echo %%F
        echo.
        goto ERROR_END
    )

    echo.
)


REM ============================================================
REM                  CHECK GENERATED CLIPS
REM ============================================================

dir /b "%TRIMMED%\*.mp4" >nul 2>&1

if errorlevel 1 (
    echo ERROR:
    echo Auto-Editor did not create any MP4 files.
    echo.
    goto ERROR_END
)


REM ============================================================
REM                  CREATE CONCAT LIST
REM ============================================================

echo.
echo ========================================
echo Creating concatenation list
echo ========================================
echo.

for /f "delims=" %%F in ('dir /b /a-d /on "%TRIMMED%\*.mp4"') do (

    for %%G in ("%TRIMMED%\%%F") do (
        echo file '%%~fG'>>"%LIST%"
    )

)


REM ============================================================
REM                       JOIN CLIPS
REM ============================================================

echo.
echo ========================================
echo Joining clips with FFmpeg
echo ========================================
echo.

ffmpeg.exe -y ^
    -f concat ^
    -safe 0 ^
    -i "%LIST%" ^
    -c copy ^
    "%FINAL%"


if errorlevel 1 (

    echo.
    echo Direct joining failed.
    echo Trying again with re-encoding...
    echo.

    ffmpeg.exe -y ^
        -f concat ^
        -safe 0 ^
        -i "%LIST%" ^
        -c:v libx264 ^
        -preset medium ^
        -crf 18 ^
        -c:a aac ^
        -b:a 192k ^
        -movflags +faststart ^
        "%FINAL%"

    if errorlevel 1 (
        echo.
        echo ERROR:
        echo FFmpeg could not join the clips.
        echo.
        goto ERROR_END
    )
)


REM ============================================================
REM                         SUCCESS
REM ============================================================

echo.
echo ========================================
echo FINISHED SUCCESSFULLY
echo ========================================
echo.

echo Final video:
echo %FINAL%
echo.

pause
exit /b 0


REM ============================================================
REM                          ERROR
REM ============================================================

:ERROR_END

echo.
echo ========================================
echo SCRIPT FAILED
echo ========================================
echo.
echo The window will stay open so you can
echo read the error above.
echo.

pause
exit /b 1