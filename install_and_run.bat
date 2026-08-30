@echo off
setlocal
cd /d "%~dp0"

py -3 --version >nul 2>&1
if errorlevel 1 goto use_python
set "BOOT_PYTHON=py -3"
goto install

:use_python
python --version >nul 2>&1
if errorlevel 1 goto no_python
set "BOOT_PYTHON=python"

:install
%BOOT_PYTHON% -c "import sys; sys.exit(not (sys.version_info.major == 3 and sys.version_info.minor in range(10, 100)))"
if errorlevel 1 goto old_python
if not exist ".venv-windows\Scripts\python.exe" %BOOT_PYTHON% -m venv ".venv-windows"
if errorlevel 1 goto failed

".venv-windows\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv-windows\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv-windows\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
".venv-windows\Scripts\python.exe" schoology_downloader.py %*
if errorlevel 1 goto failed
goto end

:no_python
echo Python 3.10 or newer was not found. Install it from python.org and enable "Add Python to PATH".
goto failed

:old_python
echo Python 3.10 or newer is required.
goto failed

:failed
echo.
echo Installation or startup failed. Copy the error above when asking for help.
pause

:end
endlocal
