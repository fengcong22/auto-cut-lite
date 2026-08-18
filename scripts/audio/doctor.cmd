@echo off
setlocal
for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
set "AUDIO_PYTHON=%REPO_ROOT%\.venv-audio\Scripts\python.exe"
pushd "%REPO_ROOT%"
python scripts\audio\audio_cleanup.py doctor --python-executable "%AUDIO_PYTHON%" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
