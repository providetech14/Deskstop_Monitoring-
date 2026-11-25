# Installs / updates the WorkLog SERVER on the HR laptop (no admin rights needed).
#   - copies the server to C:\WorkLog\server (your data in C:\WorkLog\server\data is never touched)
#   - starts it hidden in the background at every Windows logon (auto-restarts if it crashes)
#   - adds "WorkLog" shortcuts and Start / Stop / Restart controls
# The firewall part needs admin: tools\firewall_rules.ps1 (run as administrator).
$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$home_   = 'C:\WorkLog'
$srv     = Join-Path $home_ 'server'
$pyw     = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'

Write-Host 'Installing Python packages...'
python -m pip install --quiet -r (Join-Path $root 'server\requirements.txt')

Write-Host "Copying server to $srv ..."
New-Item -ItemType Directory -Force $srv | Out-Null
robocopy (Join-Path $root 'server') $srv /E /XD data __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copy failed (robocopy code $LASTEXITCODE)" }
Copy-Item (Join-Path $root 'SETUP GUIDE.md') $home_ -Force -ErrorAction SilentlyContinue

# --- control scripts -------------------------------------------------------
$runner = Join-Path $srv 'run_server.pyw'
$startVbs = 'CreateObject("WScript.Shell").Run """' + $pyw + '"" ""' + $runner + '""", 0, False'
Set-Content (Join-Path $home_ 'start-hidden.vbs') $startVbs -Encoding ASCII

$stop = @'
@echo off
if exist "C:\WorkLog\server\data\server.pid" for /f "usebackq delims=" %%p in ("C:\WorkLog\server\data\server.pid") do taskkill /PID %%p /T /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":5000 .*LISTENING"') do taskkill /PID %%p /T /F >nul 2>&1
'@
Set-Content (Join-Path $home_ 'Stop WorkLog Server.bat') ($stop + "`r`necho WorkLog server stopped.`r`ntimeout /t 3 >nul") -Encoding ASCII
Set-Content (Join-Path $home_ 'Start WorkLog Server.bat') "@echo off`r`nwscript ""C:\WorkLog\start-hidden.vbs""`r`necho WorkLog server started in the background.`r`ntimeout /t 3 >nul" -Encoding ASCII
Set-Content (Join-Path $home_ 'Restart WorkLog Server.bat') ($stop + "`r`ntimeout /t 2 >nul`r`nwscript ""C:\WorkLog\start-hidden.vbs""`r`necho WorkLog server restarted.`r`ntimeout /t 3 >nul") -Encoding ASCII
Set-Content (Join-Path $home_ 'Open WorkLog.url') "[InternetShortcut]`r`nURL=http://localhost:5000/`r`nIconFile=$srv\static\worklog.ico`r`nIconIndex=0" -Encoding ASCII
Copy-Item (Join-Path $root 'agent\worklog.ico') (Join-Path $srv 'static\worklog.ico') -Force

# --- auto-start at logon + shortcuts --------------------------------------------
$startup = [Environment]::GetFolderPath('Startup')
Copy-Item (Join-Path $home_ 'start-hidden.vbs') (Join-Path $startup 'WorkLogServer.vbs') -Force
Copy-Item (Join-Path $home_ 'Open WorkLog.url') (Join-Path ([Environment]::GetFolderPath('Desktop')) 'WorkLog (HR).url') -Force
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'WorkLog'
New-Item -ItemType Directory -Force $menu | Out-Null
Copy-Item (Join-Path $home_ 'Open WorkLog.url') (Join-Path $menu 'WorkLog (HR).url') -Force
$shell = New-Object -ComObject WScript.Shell
foreach ($n in 'Start WorkLog Server', 'Stop WorkLog Server', 'Restart WorkLog Server') {
    $s = $shell.CreateShortcut((Join-Path $menu "$n.lnk")); $s.TargetPath = Join-Path $home_ "$n.bat"; $s.WorkingDirectory = $home_; $s.Save()
}
$s = $shell.CreateShortcut((Join-Path $menu 'WorkLog folder.lnk')); $s.TargetPath = $home_; $s.Save()

# --- self-healing: Task Scheduler checks every 5 minutes and starts the server if it isn't running
# (the runner exits immediately if the server is already up). Works on battery, no time limit.
$action   = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$home_\start-hidden.vbs`""
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances Parallel -StartWhenAvailable
# (Parallel is safe: run_server.pyw exits at once if the server is already running.
#  IgnoreNew would block restarts while Task Scheduler still thinks an old copy is running.)
Stop-ScheduledTask -TaskName 'WorkLog Server' -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'WorkLog Server' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

# --- (re)start the server now, through Task Scheduler so it runs independently of this window
& cmd /c (Join-Path $home_ 'Stop WorkLog Server.bat') | Out-Null
Start-ScheduledTask -TaskName 'WorkLog Server'
$ok = $false
foreach ($i in 1..20) {
    Start-Sleep 1
    try { $r = Invoke-WebRequest 'http://127.0.0.1:5000/api/ping' -UseBasicParsing -TimeoutSec 2; $ok = $true; break } catch {}
}
if ($ok) { Write-Host 'WorkLog server is running: http://localhost:5000' -ForegroundColor Green }
else { Write-Host 'Server did not respond yet. See C:\WorkLog\server\data\logs' -ForegroundColor Yellow }
