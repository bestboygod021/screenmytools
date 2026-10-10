@echo off
REM ==========================================================================
REM  One-click Windows build for FullPage Capture Bot.
REM  Produces dist\FullPageCaptureBot.exe  (single, standalone file).
REM
REM  Prerequisites (run once):
REM     1. Python 3.10+ on PATH  -> https://www.python.org/downloads/
REM     2. pip install -r requirements.txt
REM ==========================================================================
setlocal

echo [1/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [2/3] Building the single-file executable...
python -m PyInstaller FullPageCaptureBot.spec --noconfirm --clean
if errorlevel 1 goto :fail

echo [3/3] Done.
echo.
echo   Your app is ready:  %~dp0dist\FullPageCaptureBot.exe
echo.
echo   NOTE: on first run, click "Install browser" in the app (or run
echo   "playwright install chromium") once so Playwright downloads Chromium.
goto :end

:fail
echo.
echo BUILD FAILED. See the log above.
exit /b 1

:end
endlocal
