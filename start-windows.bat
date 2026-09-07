@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -m venv .venv
    if errorlevel 1 goto error
    .venv\Scripts\python.exe -m pip install ".[desktop,tdata]"
    if errorlevel 1 goto error
    type nul > .venv\parser-0.2.0-installed
)
if not exist ".venv\parser-0.2.0-installed" (
    .venv\Scripts\python.exe -m pip install ".[desktop,tdata]"
    if errorlevel 1 goto error
    type nul > .venv\parser-0.2.0-installed
)
.venv\Scripts\pythonw.exe -m parser_app
exit /b %errorlevel%
:error
echo Setup failed. Install Python 3.11-3.13 x64, check your internet connection, then retry.
pause
exit /b 1
