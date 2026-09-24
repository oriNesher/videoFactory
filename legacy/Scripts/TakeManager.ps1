param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "Review",
        "Approve",
        "Reject",
        "Retake",
        "NextScene",
        "PreviousScene",
        "OpenPrompter"
    )]
    [string]$Action
)

$ErrorActionPreference = "Stop"

$Root          = "C:\OBS-Takes"
$Pending       = Join-Path $Root "Pending"
$Approved      = Join-Path $Root "Approved"
$Rejected      = Join-Path $Root "Rejected"
$Prompter      = Join-Path $Root "Prompter"
$SceneFile     = Join-Path $Root "scene.txt"
$StateFile     = Join-Path $Root "state.txt"

function Get-SceneNumber {
    if (-not (Test-Path $SceneFile)) {
        Set-Content -Path $SceneFile -Value "1"
    }

    $value = (Get-Content $SceneFile -Raw).Trim()
    $number = 0

    if (-not [int]::TryParse($value, [ref]$number)) {
        $number = 1
        Set-Content -Path $SceneFile -Value "1"
    }

    return [Math]::Max(1, $number)
}

function Get-SceneName {
    $number = Get-SceneNumber
    return "S{0:D2}" -f $number
}

function Wait-ForStableFile {
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.FileInfo]$File
    )

    $previousSize = -1

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Test-Path $File.FullName)) {
            throw "The recording file no longer exists."
        }

        $currentFile = Get-Item $File.FullName
        $currentSize = $currentFile.Length

        $canOpen = $false

        try {
            $stream = [System.IO.File]::Open(
                $currentFile.FullName,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::None
            )

            $stream.Close()
            $canOpen = $true
        }
        catch {
            $canOpen = $false
        }

        if ($canOpen -and $currentSize -gt 0 -and $currentSize -eq $previousSize) {
            return $currentFile
        }

        $previousSize = $currentSize
        Start-Sleep -Milliseconds 500
    }

    throw "The last recording is still being written by OBS."
}

function Get-LastPendingTake {
    $extensions = @(".mkv", ".mp4", ".mov")

    $file = Get-ChildItem -Path $Pending -File |
        Where-Object {
            $extensions -contains $_.Extension.ToLowerInvariant()
        } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1

    if (-not $file) {
        throw "No recording was found in Pending."
    }

    return Wait-ForStableFile -File $file
}

function Get-UniqueDestination {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory,

        [Parameter(Mandatory = $true)]
        [string]$BaseName,

        [Parameter(Mandatory = $true)]
        [string]$Extension
    )

    $candidate = Join-Path $Directory ($BaseName + $Extension)

    if (-not (Test-Path $candidate)) {
        return $candidate
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    return Join-Path $Directory (
        "{0}_{1}{2}" -f $BaseName, $timestamp, $Extension
    )
}

function Open-Prompter {
    $scene = Get-SceneName
    $file = Join-Path $Prompter ($scene + ".txt")

    if (-not (Test-Path $file)) {
        Set-Content -Path $file -Value "Add the script for $scene here."
    }

    Start-Process "notepad.exe" -ArgumentList "`"$file`""
    Set-Content -Path $StateFile -Value "Current scene: $scene"
}

function Show-ErrorMessage {
    param([string]$Message)

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $Message,
        "OBS Take Manager",
        "OK",
        "Error"
    ) | Out-Null
}

try {
    switch ($Action) {
        "Review" {
            $file = Get-LastPendingTake
            Start-Process $file.FullName
        }

        "Approve" {
            $file = Get-LastPendingTake
            $scene = Get-SceneName

            $destination = Get-UniqueDestination `
                -Directory $Approved `
                -BaseName ($scene + "_approved") `
                -Extension $file.Extension

            Move-Item -LiteralPath $file.FullName -Destination $destination
            Set-Content -Path $StateFile -Value "$scene approved"
        }

        "Reject" {
            $file = Get-LastPendingTake
            $scene = Get-SceneName
            $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

            $destination = Join-Path $Rejected (
                "{0}_rejected_{1}{2}" -f $scene, $timestamp, $file.Extension
            )

            Move-Item -LiteralPath $file.FullName -Destination $destination
            Set-Content -Path $StateFile -Value "$scene rejected"
        }

        "Retake" {
            $file = Get-LastPendingTake
            $scene = Get-SceneName
            $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

            $destination = Join-Path $Rejected (
                "{0}_retake_{1}{2}" -f $scene, $timestamp, $file.Extension
            )

            Move-Item -LiteralPath $file.FullName -Destination $destination
            Set-Content -Path $StateFile -Value "$scene ready for retake"
        }

        "NextScene" {
            $number = Get-SceneNumber
            $number++
            Set-Content -Path $SceneFile -Value $number
            Open-Prompter
        }

        "PreviousScene" {
            $number = Get-SceneNumber
            $number = [Math]::Max(1, $number - 1)
            Set-Content -Path $SceneFile -Value $number
            Open-Prompter
        }

        "OpenPrompter" {
            Open-Prompter
        }
    }
}
catch {
    Show-ErrorMessage -Message $_.Exception.Message
    exit 1
}