# Daftarkan backend agar otomatis berjalan setiap kali login Windows (tanpa jendela terminal).
#
#   Pasang & jalankan sekarang : powershell -ExecutionPolicy Bypass -File autostart.ps1
#   Hapus                      : powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
#
# Butuh venv Windows di folder ini:  python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'KosMonitor Backend'
$Root = $PSScriptRoot

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Task '$TaskName' dihapus."
    exit 0
}

$Python = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $Python)) {
    throw "Venv belum ada di $Root\.venv. Jalankan dulu: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$(Join-Path $Root 'serve.py')`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description 'KosMonitor backend (validasi data IoT) - http://127.0.0.1:8000/docs' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Output "Task '$TaskName' terpasang dan dijalankan. Buka http://127.0.0.1:8000/docs"
Write-Output "Log: $Root\logs\backend.log"
