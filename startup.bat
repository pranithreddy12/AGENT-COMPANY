@echo off
REM Company OS Startup Script for Windows
REM Double-click this file to start the server and open the CEO console

setlocal enabledelayedexpansion

echo.
echo ================================
echo Company OS Startup
echo ================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.11+ and try again.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] Checking Python version...
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo Python version: %PYTHON_VERSION%
echo.

REM Check if venv exists and activate it
if exist "venv\Scripts\activate.bat" (
    echo [2/4] Activating virtual environment...
    call venv\Scripts\activate.bat
    echo Virtual environment activated.
) else (
    echo [2/4] No virtual environment found (optional).
)
echo.

echo [3/4] Checking dependencies...
pip show uvicorn >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies from requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    echo Dependencies already installed.
)
echo.

REM Pick a free port: start at 8000, bump until nothing is bound (8000/8001 are often taken
REM by other projects on this machine). Ask PowerShell which port is actually usable.
set PORT=8000
for /f %%p in ('powershell -NoProfile -Command "$p=8000; while (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) { $p++ }; Write-Output $p" 2^>nul') do set PORT=%%p

REM Determine the URL
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
uvicorn app.main:app --reload --host 127.0.0.1 --port %PORT%

REM If we get here, the server stopped
echo.
echo Server stopped.
pause
