@echo off
REM One-click launcher for the SFH ROI Analyzer.
REM First run: creates a venv and installs deps. Subsequent runs: just launches.

setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Python is not installed or not on PATH. Install Python 3.10+ from python.org.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

REM Install/update deps if requirements changed
if not exist .venv\.installed (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
    type nul > .venv\.installed
)

echo.
echo Launching SFH ROI Analyzer...
echo Open http://localhost:8501 in your browser (will open automatically).
echo Press Ctrl+C in this window to stop.
echo.
streamlit run app.py
