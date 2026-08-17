@echo off
title FPWorkbench Stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\workbench.ps1" -Action stop
pause
