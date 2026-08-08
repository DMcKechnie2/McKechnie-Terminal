@echo off
echo Starting McKechnie Terminal...
cd /d "%~dp0"
:: API keys live in settings.json, which is gitignored. This file is committed,
:: so a key set here is a key published to GitHub — which is what happened to the
:: Groq key that used to be on this line. _resolve_groq_key() reads settings.json
:: first and the environment only as a fallback, so there is nothing to set here:
:: paste the key into the Settings tab instead.
set GROQ_API_KEY=
set OLLAMA_MODEL=qwen2-math:7b

:: Start Ollama in the background if not already running
set OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %ERRORLEVEL% == 0 (
    echo Ollama already running.
) else if exist "%OLLAMA_EXE%" (
    echo Starting Ollama...
    start /B "" "%OLLAMA_EXE%" serve >nul 2>&1
    timeout /T 2 /NOBREAK >nul
) else (
    echo Ollama not found - AI chat will be unavailable.
)

python app.py

:: The app exits non-zero when it refuses to start — most likely "no accounts
:: exist yet", which prints the command that fixes it. Without a pause the
:: console closes on the message and the launcher just looks broken.
if %ERRORLEVEL% neq 0 (
    echo.
    pause
)
