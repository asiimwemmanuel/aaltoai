@echo off
setlocal
cd /d "%~dp0trustworthy-monitor"

:: If .venv doesn't exist, create it and install dependencies (for the judge's fresh download)
if not exist ".venv\Scripts\python.exe" (
    echo ============================================================
    echo Setting up Python environment for the first time...
    echo ============================================================
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
)

:: Enable Gemini strictly for UI assistant use
set GEMINI_API_KEY=AQ.Ab8RN6Iw9OLi437Pq5q9kzsDZaZfyeoRxVJWz6nbqHF4CvkC-A

echo ============================================================
echo Starting the UI server...
echo Please open http://localhost:8000/ui/ in your browser.
echo ============================================================
.venv\Scripts\python.exe ui\server.py

pause
