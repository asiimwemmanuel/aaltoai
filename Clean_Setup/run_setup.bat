@echo off
setlocal
echo ===================================================
echo Full Clean Setup Script (from WINDOWS_SETUP.md)
echo ===================================================
echo.

:: Ensure we are working in the directory of the script
cd /d "%~dp0"

echo --- 1. Python ---
python --version
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    pause
    exit /b 1
)
echo.

echo --- 2. Git ---
winget install --id Git.Git -e
git --version
echo.

echo --- 3. Get the repository ---
:: Cloning exactly as mentioned in the markdown
git clone https://github.com/asiimwemmanuel/aaltoai.git trustworthy-monitor
cd trustworthy-monitor
git checkout overnight
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
echo Copying te_process.csv from your OLD_2 folder...
copy "C:\Users\giorg\Desktop\aaltoai\OLD_2\data\te_process.csv" "data\te_process.csv"
echo.

echo --- 6. Backup artifacts ---
if not exist "%USERPROFILE%\DEMO_BACKUP" mkdir "%USERPROFILE%\DEMO_BACKUP"
copy artifacts\*.json "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
copy artifacts\*.jsonl "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
copy eval\validation_report.json "%USERPROFILE%\DEMO_BACKUP\" >nul 2>&1
echo Backup completed (if any files existed).
echo.

echo --- 7. Check the tree ---
.venv\Scripts\python.exe tools\gate_check.py
.venv\Scripts\python.exe tools\validate.py
echo.

echo --- 8. Run the pipeline ---
echo Running the pipeline...
.venv\Scripts\python.exe orchestrator.py --mode dev
echo.

echo --- 9. Choose a model ---
echo (Using the default fallback stub as per llm.yaml configuration)
echo.

echo --- 10. The operator interface ---
echo Starting the UI server... 
echo Please open http://localhost:8000/ui/ in your browser.
.venv\Scripts\python.exe ui\server.py

pause
