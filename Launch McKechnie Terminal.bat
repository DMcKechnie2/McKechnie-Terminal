@echo off
echo Starting McKechnie Terminal...
cd /d "%~dp0"
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
