$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Fuck Wechat File Duplication Watch"
$Bat = Join-Path $ScriptDir "run_watch.bat"
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"

if (-not (Test-Path $Bat)) {
    throw "run_watch.bat not found: $Bat"
}

$Action = New-ScheduledTaskAction `
    -Execute $Bat `
    -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "Watch WeChat files and hardlink new copies to configured source_roots." `
        -Force `
        -ErrorAction Stop

    Write-Host "Installed scheduled task: $TaskName"
    Write-Host "Run trigger: at user logon"
    Write-Host "Script: $Bat"
} catch {
    Write-Warning "Scheduled task registration failed: $($_.Exception.Message)"
    Write-Warning "Falling back to current-user Startup shortcut."

    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($StartupShortcut)
    $Shortcut.TargetPath = $Bat
    $Shortcut.WorkingDirectory = $ScriptDir
    $Shortcut.WindowStyle = 7
    $Shortcut.Description = "Watch WeChat files and hardlink new copies to configured source_roots."
    $Shortcut.Save()

    Write-Host "Installed Startup shortcut: $StartupShortcut"
    Write-Host "Run trigger: current user login"
    Write-Host "Script: $Bat"
}
