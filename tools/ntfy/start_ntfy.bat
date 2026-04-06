@echo off
:: VARA BBS — Start ntfy server manually
:: Use this if the auto-start task isn't working or you prefer manual control.

set "NTFY_DIR=%~dp0"
set "NTFY_EXE=%NTFY_DIR%ntfy.exe"
set "NTFY_YML=%NTFY_DIR%server.yml"

if not exist "%NTFY_EXE%" (
    echo ntfy.exe not found. Run setup_ntfy.bat first.
    pause
    exit /b 1
)

echo Starting ntfy server ...
echo Press Ctrl+C to stop.
echo.
"%NTFY_EXE%" serve --config "%NTFY_YML%"
