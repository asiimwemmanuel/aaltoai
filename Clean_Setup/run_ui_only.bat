@echo off
setlocal
cd /d "%~dp0trustworthy-monitor"

:: Enable Gemini strictly for UI assistant use
set GEMINI_API_KEY=AQ.Ab8RN6Iw9OLi437Pq5q9kzsDZaZfyeoRxVJWz6nbqHF4CvkC-A

echo ============================================================
echo Starting the UI server...
echo Please open http://localhost:8000/ui/ in your browser.
echo ============================================================
.venv\Scripts\python.exe ui\server.py

pause
