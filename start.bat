@echo off
rem Starts Uncle Virgil. On the first run this also installs it into a private
rem Python environment in this folder (.venv), which takes a few minutes.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up Uncle Virgil for the first time. This takes a few minutes...
    py -3 -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install -e . || goto :error
)
start "" ".venv\Scripts\pythonw.exe" -m uncle_virgil
exit /b 0

:error
echo.
echo Setup failed. The messages above say why. Python 3.12 or 3.13 from python.org must be installed.
pause
exit /b 1
