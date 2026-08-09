#!/bin/bash
# Company OS Startup Script for macOS/Linux
# Run: chmod +x startup.sh && ./startup.sh

set -e

echo ""
echo "================================"
echo "Company OS Startup"
echo "================================"
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found. Please install Python 3.11+ and try again."
    echo "https://www.python.org/downloads/"
    exit 1
fi

echo "[1/4] Checking Python version..."
PYTHON_VERSION=$(python3 --version)
echo "Python: $PYTHON_VERSION"
echo ""

# Activate venv if it exists
if [ -f "venv/bin/activate" ]; then
    echo "[2/4] Activating virtual environment..."
    source venv/bin/activate
    echo "Virtual environment activated."
else
    echo "[2/4] No virtual environment found (optional)."
fi
echo ""

echo "[3/4] Checking dependencies..."
if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "Installing dependencies from requirements.txt..."
    pip install -r requirements.txt
else
    echo "Dependencies already installed."
fi
echo ""

echo "[4/4] Starting Company OS server..."
echo ""
echo "================================"
echo "Server is running!"
echo "================================"
echo ""
echo "CEO Console:  http://127.0.0.1:8000/console"
echo "API Docs:     http://127.0.0.1:8000/docs"
echo ""
echo "Press Ctrl+C to stop the server."
echo ""

# Start the server
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

echo ""
echo "Server stopped."
