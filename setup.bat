@echo off
setlocal enabledelayedexpansion
echo ================================
echo  McKechnie Terminal - Setup
echo ================================
echo.

:: ── Python packages ──────────────────────────────────────────────────────────
echo [1/3] Installing Python packages...
pip install flask yfinance pandas requests beautifulsoup4 groq
if %ERRORLEVEL% neq 0 (
    echo ERROR: pip install failed. Make sure Python is installed and in your PATH.
    pause & exit /b 1
)
echo Done.
echo.

:: ── Ollama install ────────────────────────────────────────────────────────────
echo [2/3] Checking Ollama...
set OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe

if exist "%OLLAMA_EXE%" (
    echo Ollama already installed.
) else (
    echo Ollama not found. Downloading installer...
    powershell -Command "Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile '%TEMP%\OllamaSetup.exe'"
    if not exist "%TEMP%\OllamaSetup.exe" (
        echo ERROR: Download failed. Check your internet connection.
        pause & exit /b 1
    )
    echo Installing Ollama silently...
    "%TEMP%\OllamaSetup.exe" /S
    :: Wait for install to complete
    :wait_loop
    if not exist "%OLLAMA_EXE%" (
        timeout /T 2 /NOBREAK >nul
        goto wait_loop
    )
    echo Ollama installed.
)
echo.

:: ── Pull AI model ─────────────────────────────────────────────────────────────
echo [3/3] Pulling AI model (qwen2-math:7b ~4GB)...
echo This may take several minutes depending on your connection.
echo.

:: Start ollama serve in background so pull works
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %ERRORLEVEL% neq 0 (
    start /B "" "%OLLAMA_EXE%" serve >nul 2>&1
    timeout /T 3 /NOBREAK >nul
)

"%OLLAMA_EXE%" pull qwen2-math:7b
if %ERRORLEVEL% == 0 (
    echo Model ready.
) else (
    echo WARNING: Model pull failed. You can retry by running:
    echo   "%OLLAMA_EXE%" pull qwen2-math:7b
)
echo.

:: ─────────────────────────────────────────────────────────────────────────────
echo ================================
echo  Setup complete!
echo  Run: Launch McKechnie Terminal.bat
echo ================================
pause
