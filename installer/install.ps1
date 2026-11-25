# Installs the WorkLog Agent for the current Windows user (no administrator rights needed).
$ErrorActionPreference = 'Stop'
$src  = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA 'WorkLog'
$exe  = Join-Path $dest 'WorkLogAgent.exe'

Write-Host ''
Write-Host '  Installing WorkLog Agent...' -ForegroundColor Cyan

Get-Process WorkLogAgent -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800

New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $src 'WorkLogAgent.exe') $exe -Force
if (Test-Path (Join-Path $src 'server.txt')) { Copy-Item (Join-Path $src 'server.txt') (Join-Path $dest 'server.txt') -Force }
Copy-Item (Join-Path $src 'uninstall.ps1') (Join-Path $dest 'uninstall.ps1') -Force

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $dest
    $s.IconLocation = "$exe,0"
    $s.Description = 'WorkLog - Time In / Break / Time Out'
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'WorkLog.lnk')
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'WorkLog.lnk')

# Start automatically when Windows starts
$startup = [Environment]::GetFolderPath('Startup')
Set-Content -Path (Join-Path $startup 'WorkLogAgent.vbs') -Encoding ASCII `
    -Value ('CreateObject("WScript.Shell").Run """' + $exe + '""", 0, False')

Start-Process $exe
Write-Host '  Done! WorkLog is open. Log in with the username and password from HR.' -ForegroundColor Green
Write-Host '  It will start automatically every time this PC starts.'
Write-Host ''
