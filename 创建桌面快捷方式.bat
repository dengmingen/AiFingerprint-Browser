@echo off
title FPWorkbench Shortcut
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\workbench.ps1" -Action shortcut
pause
