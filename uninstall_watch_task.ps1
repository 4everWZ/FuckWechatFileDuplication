$TaskName = "Fuck Wechat File Duplication Watch"
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
} else {
    Write-Host "Task not found: $TaskName"
}
if (Test-Path $StartupShortcut) {
    Remove-Item -LiteralPath $StartupShortcut -Force
    Write-Host "Removed Startup shortcut: $StartupShortcut"
} else {
    Write-Host "Startup shortcut not found: $StartupShortcut"
}
