@echo off
:: VARA BBS — Stop ntfy server

echo Stopping ntfy server ...
taskkill /im ntfy.exe /f >nul 2>&1
if %errorlevel%==0 (
    echo ntfy server stopped.
) else (
    echo ntfy server was not running.
)
pause
