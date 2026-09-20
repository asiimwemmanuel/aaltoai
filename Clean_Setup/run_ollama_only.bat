@echo off
setlocal
cd /d "%~dp0trustworthy-monitor"

:: Enable Gemini strictly for UI assistant use (S4 and S7 will still use Ollama per config)
set GEMINI_API_KEY=AQ.Ab8RN6Iw9OLi437Pq5q9kzsDZaZfyeoRxVJWz6nbqHF4CvkC-A

echo ============================================================
echo Running S4: Semantic Inference (with Local Ollama)
echo ============================================================
.venv\Scripts\python.exe pipeline\s4_semantics.py

echo.
echo ============================================================
echo Running S7: LLM Diagnosis (with Local Ollama)
echo ============================================================
.venv\Scripts\python.exe pipeline\s7_diagnosis.py --drift artifacts\drift_events.json --semantics artifacts\semantics.json --out artifacts\diagnosis.json

echo.
echo ============================================================
echo Starting the UI server...
echo Please open http://localhost:8000/ui/ in your browser.
echo ============================================================
.venv\Scripts\python.exe ui\server.py

pause
