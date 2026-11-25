# Removes the WorkLog Agent for the current Windows user.
Get-Process WorkLogAgent -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800
$paths = @(
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'WorkLogAgent.vbs'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'WorkLog.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'WorkLog.lnk'),
    (Join-Path $env:APPDATA 'WorkLog')
)
foreach ($p in $paths) { if (Test-Path $p) { Remove-Item $p -Recurse -Force } }
$dest = Join-Path $env:LOCALAPPDATA 'WorkLog'
# this script may be running from inside $dest, so delete the folder after it exits
Start-Process cmd.exe -WindowStyle Hidden -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$dest`""
Write-Host 'WorkLog Agent has been removed from this PC.' -ForegroundColor Green
