@echo off
echo ================================
echo  McKechnie Terminal - Build
echo ================================
echo.

echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building executable...
pyinstaller --onefile --noconsole --name "McKechnie Terminal" --add-data "templates;templates" app.py

echo.
echo ================================
echo  Build complete!
echo  Your .exe is in the dist/ folder
echo ================================
pause
