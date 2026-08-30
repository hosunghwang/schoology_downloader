@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv-windows\Scripts\python.exe" goto not_installed
".venv-windows\Scripts\python.exe" schoology_downloader.py %*
if errorlevel 1 pause
goto end

:not_installed
echo The local environment is not installed yet.
echo Run install_and_run.bat first.
pause

:end
endlocal
