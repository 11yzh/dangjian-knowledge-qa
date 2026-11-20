@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".runtime\python\python.exe" (
    ".runtime\python\python.exe" -m scripts.launch
) else (
    powershell -NoProfile -File "%~dp0scripts\start.ps1"
)
if errorlevel 1 (pause & exit /b 1)
if not defined DANGJIAN_PORT set "DANGJIAN_PORT=8768"
start "" "http://127.0.0.1:%DANGJIAN_PORT%/"
