@echo off
title FPWorkbench Launcher
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\workbench.ps1" -Action start %*
pause
