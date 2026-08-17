@echo off
title FPWorkbench Autostart ON
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench.ps1" -Action autostart-on
pause
