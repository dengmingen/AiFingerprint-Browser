@echo off
title FPWorkbench Autostart OFF
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench.ps1" -Action autostart-off
pause
