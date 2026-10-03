@echo off
REM Launches Chrome with remote debugging for recaptcha solving
REM Users should NOT change this — it auto-creates profile

set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
    echo Chrome not found. Install Google Chrome first.
    pause
    exit /b 1
)

echo Starting Chrome CDP on port 9228...
start "" "%CHROME%" --remote-debugging-port=9228 --user-data-dir="%~dp0chrome_cdp_profile" --no-first-run --no-default-browser-check
