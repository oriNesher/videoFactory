# Starts Video Factory and opens it in Firefox.
#
# Starts the backend (port 8000) and the frontend (port 5173) in their own
# minimized windows if they are not already running, waits until both answer,
# then opens http://localhost:5173. Running it again while the app is up only
# opens the browser: it never starts a second backend (see README, "Run one
# backend process at a time").
#
# To stop the app, close the two "Video Factory" windows.

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$frontend = Join-Path $root 'frontend'
$backendUrl = 'http://127.0.0.1:8000/health'
$frontendUrl = 'http://localhost:5173'
$timeoutSeconds = 90

function Test-Url([string]$url) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Start-Server([string]$title, [string]$workingDir, [string]$command) {
    $script = "`$host.UI.RawUI.WindowTitle = '$title'; $command"
    Start-Process powershell.exe -WorkingDirectory $workingDir -WindowStyle Minimized `
        -ArgumentList '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $script
}

function Fail([string]$message) {
    Write-Host ''
    Write-Host $message -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}

Write-Host 'Video Factory' -ForegroundColor Cyan

if (-not (Test-Path $python)) { Fail "Python virtual environment not found: $python" }
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Fail "frontend\node_modules is missing. Run 'npm.cmd install' in $frontend first."
}

if (Test-Url $backendUrl) {
    Write-Host '  backend   already running'
} else {
    Write-Host '  backend   starting...'
    Start-Server 'Video Factory - backend' $root "& '$python' -m uvicorn backend.main:app --port 8000"
}

if (Test-Url $frontendUrl) {
    Write-Host '  frontend  already running'
} else {
    Write-Host '  frontend  starting...'
    Start-Server 'Video Factory - frontend' $frontend 'npm.cmd run dev'
}

$deadline = (Get-Date).AddSeconds($timeoutSeconds)
while (-not ((Test-Url $backendUrl) -and (Test-Url $frontendUrl))) {
    if ((Get-Date) -gt $deadline) {
        Fail "The app did not start within $timeoutSeconds seconds. Check the 'Video Factory' windows on the taskbar for errors."
    }
    Start-Sleep -Milliseconds 500
}

Write-Host '  ready, opening Firefox' -ForegroundColor Green

$firefox = @(
    "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
    "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe",
    "$env:LOCALAPPDATA\Mozilla Firefox\firefox.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($firefox) {
    Start-Process $firefox -ArgumentList $frontendUrl
} else {
    # Firefox not found: fall back to the default browser.
    Start-Process $frontendUrl
}
