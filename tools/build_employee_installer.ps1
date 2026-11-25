# Builds the "Employee Installer" folder: WorkLogAgent.exe + install scripts + server.txt
# (server.txt = this laptop's current address, so employees only type username + password).
# Usage:  powershell -ExecutionPolicy Bypass -File tools\build_employee_installer.ps1 [-Port 5000]
param([int]$Port = 5000)
$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$out   = Join-Path $root 'Employee Installer'
$work  = Join-Path $env:TEMP 'worklog-build'

Write-Host 'Building WorkLogAgent.exe ...'
python -m pip install --quiet pyinstaller
python (Join-Path $root 'agent\make_icon.py') | Out-Null
python -m PyInstaller --noconfirm --clean --onefile --windowed --log-level WARN --name WorkLogAgent `
    --icon (Join-Path $root 'agent\worklog.ico') --add-data ((Join-Path $root 'agent\worklog.ico') + ';.') `
    --distpath $out --workpath "$work\build" --specpath $work (Join-Path $root 'agent\worklog_agent.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }

Copy-Item (Join-Path $root 'installer\*') $out -Force

# This PC's main network address (the one with a default gateway)
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
       Select-Object -First 1).IPv4Address.IPAddress
$lines = @()
if ($ip) { $lines += "http://${ip}:$Port" }
$lines += "http://$($env:COMPUTERNAME):$Port"
Set-Content -Path (Join-Path $out 'server.txt') -Value $lines -Encoding ASCII
Write-Host "Done: $out"
Write-Host "Server address written to server.txt: $($lines[0])"
