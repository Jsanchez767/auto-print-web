@echo off
REM Auto-Print - open the "Print Here" receiver in Chrome/Edge kiosk-printing mode.
REM Jobs sent from the website then print SILENTLY on this PC's default printer.
REM
REM 1) Set PRINT_HERE_URL below to your server's /print-here page.
REM 2) Double-click this file. (No install or admin rights needed.)

set "PRINT_HERE_URL=https://maticslab.com/print-here"
set "PROFILE=%LOCALAPPDATA%\auto-print-kiosk"

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

if exist "%CHROME%" (
  start "" "%CHROME%" --kiosk-printing --user-data-dir="%PROFILE%" --app=%PRINT_HERE_URL%
) else if exist "%EDGE%" (
  start "" "%EDGE%" --kiosk-printing --user-data-dir="%PROFILE%" --app=%PRINT_HERE_URL%
) else (
  echo Google Chrome or Microsoft Edge is required for silent printing.
  echo Install Chrome, then run this again.
  pause
)
