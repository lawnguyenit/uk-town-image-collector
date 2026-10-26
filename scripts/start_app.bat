@echo off
title UK Town Image Collector
setlocal enabledelayedexpansion

cd /d "%~dp0.."

echo =========================================================
echo    UK Town Image ^& Attribution Collector - Desktop UI
echo =========================================================
echo.

:: Detect Python interpreter (.venv-clean, .venv, or system python)
if exist ".venv-clean\Scripts\python.exe" (
    set "PY_EXE=.venv-clean\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

echo Starting application using !PY_EXE!...
"!PY_EXE!" app.py

if errorlevel 1 (
    echo.
    echo Application exited with an error.
    pause
)
