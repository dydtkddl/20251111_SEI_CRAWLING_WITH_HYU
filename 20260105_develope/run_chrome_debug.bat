@echo off
setlocal

echo [INFO] Looking for Google Chrome...

set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_PATH%" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)

if not exist "%CHROME_PATH%" (
    echo [ERROR] Chrome not found! Please edit this script to set CHROME_PATH manually.
    pause
    exit /b 1
)

echo [INFO] Found Chrome at: "%CHROME_PATH%"
echo.
echo ===============================================================================
echo [IMPORTANT] 
echo 1. A new Chrome window will open.
echo 2. Please LOGIN to ScienceDirect (or your library proxy) in this new window.
echo 3. DO NOT CLOSE this Chrome window. Keep it open while running the Python script.
echo ===============================================================================
echo.
pause

REM Create a directory for a fresh user profile to avoid conflicts
if not exist "chrome_debug_profile" mkdir "chrome_debug_profile"

REM Launch Chrome with remote debugging enabled on port 9222
"%CHROME_PATH%" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome_debug_profile"

endlocal
