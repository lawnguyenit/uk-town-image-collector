@echo off
title Build UKTownImageCollector Standalone Executable
setlocal enabledelayedexpansion

cd /d "%~dp0.."

echo =========================================================
echo   Building UKTownImageCollector Standalone EXE
echo =========================================================
echo.

if exist ".venv-clean\Scripts\pyinstaller.exe" (
    set "PYI=.venv-clean\Scripts\pyinstaller.exe"
) else (
    set "PYI=pyinstaller"
)

echo Building standalone single-file binary with PyInstaller...
"!PYI!" --onefile --noconsole --name "UKTownImageCollector" --paths "src" --add-data "src/uk_town_image_collector/web;uk_town_image_collector/web" --add-data "input.csv;." app.py

if errorlevel 1 (
    echo.
    echo Build failed!
    pause
    exit /b 1
)

echo.
echo =========================================================
echo [SUCCESS] Standalone EXE generated in dist\UKTownImageCollector.exe
echo =========================================================
pause
