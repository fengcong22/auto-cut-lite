@echo off
setlocal
title Auto-Cut Lite Uninstall
chcp 65001 >nul

set "PACKAGE_ROOT=%~dp0"
set "UNINSTALL_SCRIPT=%PACKAGE_ROOT%installer\uninstall_auto_cut_lite.ps1"

if not exist "%UNINSTALL_SCRIPT%" (
    echo.
    echo Auto-Cut Lite uninstaller is missing.
    echo Use the complete Auto-Cut Lite package and retry.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%UNINSTALL_SCRIPT%"
set "UNINSTALL_EXIT=%ERRORLEVEL%"

echo.
if not "%UNINSTALL_EXIT%"=="0" (
    echo Uninstall did not complete. Keep this window open and send the error text to Codex.
) else (
    echo Auto-Cut Lite uninstall completed. The report path is shown above.
)
echo.
pause
exit /b %UNINSTALL_EXIT%
