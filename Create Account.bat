@echo off
:: Double-click this to create an account.
::
:: It has to be a console window rather than something the app can do over HTTP:
:: the password is read with getpass, which reads the console directly, so it is
:: never echoed, never lands in a command line, and never reaches a log. A setup
:: page reachable before anyone has signed in would be public signup with extra
:: steps, which is the thing this app does not have.
::
:: The first account created also inherits the pre-accounts portfolio and API keys.
cd /d "%~dp0"
echo.
echo  McKechnie Terminal - create an account
echo  ======================================
echo.
echo  You will be asked for a username, then a password twice.
echo  The password will NOT appear as you type it. That is deliberate.
echo.

python app.py create-admin

echo.
echo  ------------------------------------------------------------
echo  Done. You can close this window and sign in with:
echo      Launch McKechnie Terminal.bat
echo.
pause
