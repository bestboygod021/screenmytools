@echo off
REM ==========================================================================
REM  Digitally sign dist\FullPageCaptureBot.exe so Windows SmartScreen trusts it.
REM
REM  Usage:
REM     tools\sign_windows_exe.bat  path\to\certificate.pfx
REM
REM  Requires the Windows SDK "signtool" (installed with Visual Studio /
REM  Windows SDK) and a code-signing certificate (.pfx).
REM ==========================================================================
setlocal

set "CERT=%~1"
if "%CERT%"=="" set "CERT=cert.pfx"
set "EXE=%~dp0..\dist\FullPageCaptureBot.exe"

if not exist "%EXE%" (
    echo ERROR: %EXE% not found. Run build_windows_exe.bat first.
    exit /b 1
)

where signtool >nul 2>nul
if errorlevel 1 (
    echo ERROR: signtool not on PATH. Open a "Developer Command Prompt for VS".
    exit /b 1
)

set "PWS="
set /p "PWS=Certificate password: "

echo Signing %EXE% ...
signtool sign /f "%CERT%" /p "%PWS%" ^
    /tr http://timestamp.sectigo.com /td sha256 ^
    /fd sha256 /v "%EXE%"
if errorlevel 1 (
    echo ERROR: signing failed.
    exit /b 1
)

echo Verifying signature ...
signtool verify /pa /v "%EXE%"

echo Done. The exe is now signed.
endlocal
