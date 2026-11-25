@echo off
title Uninstall WorkLog Agent
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
pause
