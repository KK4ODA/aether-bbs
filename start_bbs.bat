@echo off
chcp 65001 >nul
title Aether BBS

:: -- Aether BBS Launcher ---------------------------------------------------------
:: This script sets up the Python environment and launches the Aether BBS.
:: Place this file in the same folder as main.py.
:: -----------------------------------------------------------------------------

setlocal EnableDelayedExpansion

:: Change to the directory where this bat file lives
cd /d "%~dp0"

echo.
echo     ###   ######  ######  ##  ##  ######  #####      #####  #####   ####
echo    ## ##  ##        ##    ##  ##  ##      ##  ##     ##  ## ##  ## ##
echo   ##   ## ####      ##    ######  ####    #####      #####  #####   ###
echo   ####### ##        ##    ##  ##  ##      ## ##      ##  ## ##  ##    ##
echo   ##   ## ######    ##    ##  ##  ######  ##  ##     #####  #####  ####
echo.
set "APPVER="
if exist VERSION set /p APPVER=<VERSION
echo        Amateur radio BBS for VARA FM/HF, Aether HF and AX.25 packet  v!APPVER!
echo  -------------------------------------------------------------------------------------
echo.

:: -- Check Python is available -------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found on PATH.
    echo.
    echo  Please install Python 3.11 or later from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: Check Python version is 3.11+
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  Python version: %PY_VER%

:: -- Check / install dependencies ---------------------------------------------
echo  Checking dependencies...
echo.

set MISSING=0

python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo  [MISSING] PyQt6
    set MISSING=1
)

python -c "import pydantic" >nul 2>&1
if errorlevel 1 (
    echo  [MISSING] pydantic
    set MISSING=1
)

python -c "import tomli_w" >nul 2>&1
if errorlevel 1 (
    echo  [MISSING] tomli-w
    set MISSING=1
)

python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo  [MISSING] pyserial
    set MISSING=1
)

python -c "import win32com.client" >nul 2>&1
if errorlevel 1 (
    echo  pywin32 is not installed. It is required for OmniRig PTT control.
    echo  Do you plan to use OmniRig for shared radio control? [Y/N]
    set /p OMNIRIG_CHOICE="  > "
    if /i "!OMNIRIG_CHOICE!"=="Y" (
        echo.
        echo  Installing pywin32...
        python -m pip install pywin32
        if errorlevel 1 (
            echo  [ERROR] Failed to install pywin32. You can install it manually:
            echo    python -m pip install pywin32
        ) else (
            echo  pywin32 installed successfully. OmniRig PTT is now available.
        )
    ) else (
        echo  Skipping pywin32. You can install it later with: pip install pywin32
    )
    echo.
)

if !MISSING!==1 (
    echo.
    echo  One or more required packages are missing.
    echo  Would you like to install them now? [Y/N]
    set /p INSTALL_CHOICE="  > "
    if /i "!INSTALL_CHOICE!"=="Y" (
        echo.
        echo  Installing dependencies...
        python -m pip install PyQt6 pydantic tomli-w pyserial
        if errorlevel 1 (
            echo.
            echo  [ERROR] pip install failed. Please run manually:
            echo    python -m pip install PyQt6 pydantic tomli-w pyserial
            pause
            exit /b 1
        )
        echo.
        echo  Dependencies installed successfully.
    ) else (
        echo.
        echo  Cannot start without required packages. Exiting.
        pause
        exit /b 1
    )
) else (
    echo  All dependencies satisfied.
)

echo.
echo  -------------------------------------------------------------------------------------
echo  Starting Aether BBS...
echo  -------------------------------------------------------------------------------------
echo.

:: -- Launch the application ----------------------------------------------------
python main.py

:: Catch non-zero exit
if errorlevel 1 (
    echo.
    echo  -------------------------------------------------------------------------------------
    echo  [ERROR] Aether BBS exited with an error (code %errorlevel%).
    echo  Check the logs folder for details.
    echo  -------------------------------------------------------------------------------------
    echo.
    pause
)

endlocal
