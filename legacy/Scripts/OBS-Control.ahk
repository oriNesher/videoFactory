#Requires AutoHotkey v2.0
#SingleInstance Force

psScript := "C:\OBS-Takes\Scripts\TakeManager.ps1"

RunPowerShell(action) {
    global psScript

    command := 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "'
        . psScript
        . '" -Action '
        . action

    RunWait(command, , "Hide")
}

; F8 — Start recording
F8::{
    Send("^!{F8}")
}

; F9 — Stop recording
F9::{
    Send("^!{F9}")
}

; F10 — Review last take
F10::{
    RunPowerShell("Review")
}

; F11 — Approve
F11::{
    RunPowerShell("Approve")
}

; F12 — Reject
F12::{
    RunPowerShell("Reject")
}

; Ctrl+F8 — Reject last take and start recording again
^F8::{
    RunPowerShell("Retake")
    Sleep(500)
    Send("^!{F8}")
}

; Page Down — Next scene
PgDn::{
    RunPowerShell("NextScene")
}

; Page Up — Previous scene
PgUp::{
    RunPowerShell("PreviousScene")
}

; Ctrl+F10 — Open current prompter
^F10::{
    RunPowerShell("OpenPrompter")
}