@echo off
cd /d "%~dp0"
ngrok http 8080 --url https://mud-sinless-net.ngrok-free.dev
if errorlevel 1 pause
