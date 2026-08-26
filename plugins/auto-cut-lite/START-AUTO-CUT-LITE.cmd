@echo off
setlocal
title Auto-Cut Lite Setup
chcp 65001 >nul

set "PACKAGE_ROOT=%~dp0"
set "ONE_CLICK_SCRIPT=%PACKAGE_ROOT%installer\one_click_deploy.ps1"

if not exist "%PACKAGE_ROOT%PACKAGE-MANIFEST.json" (
    echo.
    echo Auto-Cut Lite package files are incomplete.
    echo Use "Extract All" on the ZIP, then run this file from the extracted Auto-cut-lite folder.
    echo.
    pause
    exit /b 1
)

if not exist "%ONE_CLICK_SCRIPT%" (
    echo.
    echo Auto-Cut Lite one-click installer is missing.
    echo Extract the complete ZIP again and retry.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ONE_CLICK_SCRIPT%"
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if not "%INSTALL_EXIT%"=="0" (
    echo Deployment did not complete. Keep this window open and send the error text to Codex.
) else (
    echo Deployment completed. The workspace path is shown above and copied to the clipboard.
)
echo.
pause
exit /b %INSTALL_EXIT%
