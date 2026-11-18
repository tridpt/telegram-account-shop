@echo off
title Telegram Shop - Giam sat bot va ngrok
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Khong tim thay .venv\Scripts\python.exe. Hay cai dat bot truoc.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" supervise_shop.py
if errorlevel 1 pause
