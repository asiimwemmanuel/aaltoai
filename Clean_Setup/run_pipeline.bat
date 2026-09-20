@echo off
setlocal
cd /d "%~dp0trustworthy-monitor"

:: Set your provided Gemini API key
set GEMINI_API_KEY=AQ.Ab8RN6Iw9OLi437Pq5q9kzsDZaZfyeoRxVJWz6nbqHF4CvkC-A

echo Running gate_check.py (fixed for Windows)...
.venv\Scripts\python.exe tools\gate_check.py

echo.
echo Running orchestrator.py in prod mode (Full dataset)...
.venv\Scripts\python.exe orchestrator.py --mode prod

echo.
echo Starting the UI server...
echo Please open http://localhost:8000/ui/ in your browser.
.venv\Scripts\python.exe ui\server.py

pause
