@echo off
title Install WorkLog Agent
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo   Installation failed. Please send a photo of this window to HR.
)
pause
