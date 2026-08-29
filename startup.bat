@echo off
REM Company OS Startup Script for Windows
REM Double-click this file to start the server and open the CEO console

setlocal enabledelayedexpansion

echo.
echo ================================
echo Company OS Startup
echo ================================
echo.

REM Find a Python that actually has this project's dependencies. A bare "python" on PATH can
REM resolve to a different, empty install (this has bitten this exact project before) - the
REM py launcher lets us pin a specific version explicitly instead of guessing what PATH gives us.
set PYEXE=
for %%V in (3.11 3.12 3.13 3.10) do (
    if not defined PYEXE (
        py -%%V --version >nul 2>&1
        if not errorlevel 1 set PYEXE=py -%%V
    )
)
if not defined PYEXE (
    python --version >nul 2>&1
    if not errorlevel 1 set PYEXE=python
)
if not defined PYEXE (
    echo ERROR: No Python installation found. Install Python 3.11+ from:
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/4] Using Python: %PYEXE%
%PYEXE% --version
echo.

REM Check if venv exists and activate it
if exist "venv\Scripts\activate.bat" (
    echo [2/4] Activating virtual environment...
    call venv\Scripts\activate.bat
    echo Virtual environment activated.
) else (
    echo [2/4] No virtual environment found ^(optional^).
)
echo.

echo [3/4] Checking dependencies...
%PYEXE% -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies from requirements.txt...
    %PYEXE% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    echo Dependencies already installed.
)
echo.

REM Pick a free port starting at 8000 (8000/8001 are often taken by other projects on this
REM machine). A real .py file, not an inline one-liner - embedding Python's own string quoting
REM inside a batch FOR/F command substitution is exactly what breaks cmd.exe's parser.
set PORT=8000
for /f %%P in ('%PYEXE% "%~dp0scripts\pick_free_port.py"') do set PORT=%%P

set SERVER_URL=http://127.0.0.1:%PORT%/console
set API_DOCS=http://127.0.0.1:%PORT%/docs

echo [4/4] Starting Company OS server...
if not "%PORT%"=="8000" echo Port 8000 was busy - using port %PORT% instead.
echo.
echo ================================
echo Server is running!
echo ================================
echo.
echo CEO Console:  %SERVER_URL%
echo API Docs:     %API_DOCS%
echo.
echo Press Ctrl+C to stop the server.
echo.

REM Start the server
%PYEXE% -m uvicorn app.main:app --reload --host 127.0.0.1 --port %PORT%

REM If we get here, the server stopped
echo.
echo Server stopped.
pause
