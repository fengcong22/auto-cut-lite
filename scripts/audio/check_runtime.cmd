@echo off
setlocal
call "%~dp0doctor.cmd" %*
exit /b %ERRORLEVEL%
