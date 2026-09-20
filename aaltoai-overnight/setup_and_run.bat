@echo off
setlocal
echo ===================================================
echo Windows Setup Script for AaltoAI Trustworthy Monitor
echo ===================================================
echo.

:: Ensure we are working in the directory of the script
cd /d "%~dp0"

echo --- 1. Python ---
python --version
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH. 
    echo Please install Python 3.11 or 3.12 from python.org and check "Add python.exe to PATH".
    pause
    exit /b 1
)
echo.

echo --- 2. Git ---
winget install --id Git.Git -e
echo.

echo --- 3. Get the repository ---
:: Skipping git clone because the files have already been uploaded by the user.
echo Skipping git clone as the new version is already in this folder.
echo.

echo --- 4. The virtual environment ---
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install jsonschema pyyaml duckdb polars pandas numpy pyarrow
.venv\Scripts\python.exe -c "import duckdb, polars, pandas, numpy, yaml, jsonschema; print('all imports fine')"
if %errorlevel% neq 0 (
    echo ERROR: Virtual environment setup failed.
    pause
    exit /b 1
)
echo.

echo --- 5. The dataset ---
if not exist data mkdir data
echo NOTE: The pipeline expects data\te_process.csv.
echo Please ensure you place te_process.csv inside the data\ folder or create a symbolic link before running if it's missing.
echo.

echo --- 6. Backup artifacts ---
if not exist "%USERPROFILE%\DEMO_BACKUP" mkdir "%USERPROFILE%\DEMO_BACKUP"
copy artifacts\*.json "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
copy artifacts\*.jsonl "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
copy eval\validation_report.json "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
echo Backup completed to %USERPROFILE%\DEMO_BACKUP (if files existed).
echo.

echo --- 7. Check the tree ---
.venv\Scripts\python.exe tools\gate_check.py
.venv\Scripts\python.exe tools\validate.py
echo.

echo --- 8. Run the pipeline ---
echo Running the pipeline in dev mode...
.venv\Scripts\python.exe orchestrator.py --mode dev
echo.

echo --- 9. Choose a model ---
echo Using the default model defined in config\llm.yaml (provider: stub by default).
echo (Edit config\llm.yaml if you need to set up Gemini or Ollama locally).
echo.

echo --- 10. The operator interface ---
echo Starting the UI server... 
echo Please open http://localhost:8000/ui/ in your browser (use Ctrl+Shift+R to hard refresh).
.venv\Scripts\python.exe ui\server.py

pause
