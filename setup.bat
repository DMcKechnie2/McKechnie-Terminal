@echo off
setlocal enabledelayedexpansion
echo ================================
echo  McKechnie Terminal - Setup
echo ================================
echo.

:: ── Python packages ──────────────────────────────────────────────────────────
echo [1/1] Installing Python packages...
pip install flask yfinance pandas requests beautifulsoup4 groq
if %ERRORLEVEL% neq 0 (
    echo ERROR: pip install failed. Make sure Python is installed and in your PATH.
    pause & exit /b 1
)
echo Done.
echo.

:: ─────────────────────────────────────────────────────────────────────────────
echo ================================
echo  Setup complete!
echo  Run: Launch McKechnie Terminal.bat
echo ================================
pause
