@echo off
title Install / update WorkLog server
echo Installing or updating the WorkLog server in C:\WorkLog ...
echo (Your data in C:\WorkLog\server\data is kept.)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_hr_laptop.ps1"
echo.
echo Adding firewall rules (click YES on the Windows prompt)...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0tools\firewall_rules.ps1\"'"
echo.
echo Rebuilding the Employee Installer with this laptop's current address...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_employee_installer.ps1" >nul
echo Done. Open WorkLog with the "WorkLog (HR)" icon on your desktop.
pause
