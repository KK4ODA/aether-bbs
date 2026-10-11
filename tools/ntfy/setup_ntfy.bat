@echo off
setlocal EnableDelayedExpansion

REM =====================================================================
REM Aether BBS — ntfy Server Setup
REM =====================================================================

set "VER=2.17.0"
set "TASK=Aether-BBS-ntfy"

REM Use delayed expansion for all paths to survive (parentheses) and spaces
set "D=%~dp0"
set "EXE=!D!ntfy.exe"
set "YML=!D!server.yml"

echo.
echo ============================================
echo   Aether BBS - ntfy Push Notification Setup
echo ============================================
echo.

if exist "!EXE!" goto configure

echo [1/4] Downloading and extracting ntfy v!VER! ...
echo.
echo       This may take a minute ...
echo.

REM Do ALL file work inside a single PowerShell call to avoid
REM CMD path-parsing issues with spaces and parentheses.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; " ^
  "$ProgressPreference='SilentlyContinue'; " ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; " ^
  "$dir = '!D!'; " ^
  "$zip = Join-Path $dir 'ntfy.zip'; " ^
  "$tmp = Join-Path $dir '_extract'; " ^
  "$exe = Join-Path $dir 'ntfy.exe'; " ^
  "$url = 'https://github.com/binwiederhier/ntfy/releases/download/v!VER!/ntfy_!VER!_windows_amd64.zip'; " ^
  "Write-Host \"Downloading from: $url\"; " ^
  "try { " ^
  "  Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing; " ^
  "  Write-Host 'Download OK'; " ^
  "  Expand-Archive -Path $zip -DestinationPath $tmp -Force; " ^
  "  Write-Host 'Extraction OK'; " ^
  "  $found = Get-ChildItem -Path $tmp -Recurse -Filter 'ntfy.exe' | Select-Object -First 1; " ^
  "  if ($found) { Copy-Item $found.FullName $exe -Force; Write-Host 'Copy OK' } " ^
  "  else { Write-Host 'ERROR: ntfy.exe not found in archive'; exit 1 }; " ^
  "  Remove-Item $zip -Force -ErrorAction SilentlyContinue; " ^
  "  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue; " ^
  "} catch { " ^
  "  Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 " ^
  "}"

if not exist "!EXE!" goto dl_failed

echo.
echo [OK] ntfy.exe installed successfully.
goto configure

:dl_failed
echo.
echo [ERROR] Download or extraction failed.
echo.
echo   You can download ntfy manually:
echo   1. Go to https://github.com/binwiederhier/ntfy/releases
echo   2. Download ntfy_!VER!_windows_amd64.zip
echo   3. Extract ntfy.exe into this folder
echo   4. Run this script again
goto done

:configure
echo.
echo [3/4] Writing server configuration ...

echo listen-http: ":8080"> "!YML!"
echo auth-default-access: "read-write">> "!YML!"
echo cache-duration: "12h">> "!YML!"

if not exist "!YML!" goto cfg_failed
echo [OK] Config written.
goto do_task

:cfg_failed
echo [ERROR] Failed to write config file.
goto done

:do_task
echo.
echo [4/4] Creating auto-start task "!TASK!" ...

REM Remove the task created under the old VARA BBS name, if present
schtasks /query /tn "VARA-BBS-ntfy" >nul 2>&1
if !errorlevel!==0 schtasks /delete /tn "VARA-BBS-ntfy" /f >nul 2>&1
schtasks /query /tn "!TASK!" >nul 2>&1
if !errorlevel!==0 schtasks /delete /tn "!TASK!" /f >nul 2>&1

schtasks /create /tn "!TASK!" /tr "\"!EXE!\" serve --config \"!YML!\"" /sc ONLOGON /rl HIGHEST /f >nul 2>&1

if !errorlevel!==0 echo [OK] Scheduled task created.
if not !errorlevel!==0 echo [WARN] Could not create task. Try running as Administrator.

echo.
echo Starting ntfy server ...
start "" /min "!EXE!" serve --config "!YML!"

REM Determine LAN IP
set "LAN_IP=YOUR-PC-IP"
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "LAN_IP=%%A"
    set "LAN_IP=!LAN_IP: =!"
)

echo.
echo ============================================
echo   SUCCESS - ntfy is running on port 8080
echo ============================================
echo.
echo   Server:  http://127.0.0.1:8080
echo   LAN:     http://!LAN_IP!:8080
echo.
echo   PHONE SETUP:
echo   1. Install "ntfy" app on your phone
echo      Android: Google Play / iPhone: App Store
echo.
echo   2. Open the app, tap "+"
echo.
echo   3. Subscribe to:
echo      http://!LAN_IP!:8080/aether-bbs-alerts
echo.
echo   4. In Aether BBS: Settings, Notifications, Enable
echo.
echo   5. Click "Send Test Notification" to verify
echo.
echo ============================================

:done
echo.
echo Press any key to close ...
pause >nul
