$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Fuck Wechat File Duplication"
$Bat = Join-Path $ScriptDir "run_once.bat"

if (-not (Test-Path $Bat)) {
    throw "run_once.bat not found: $Bat"
}

$Action = New-ScheduledTaskAction `
    -Execute $Bat `
    -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:30AM

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Deduplicate WeChat files with xxhash and NTFS hardlinks." `
    -Force

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Weekly run time: Sunday 03:30"
Write-Host "Script: $Bat"
