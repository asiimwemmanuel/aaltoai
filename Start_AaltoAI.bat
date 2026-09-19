@echo off
color 0A
echo =======================================================
echo     AaltoAI - The Deterministic Industrial AI
echo =======================================================
echo.
echo [1/3] Ensuring all dependencies are installed...
pip install uv >nul 2>&1
uv pip install duckdb polars pandas numpy jsonschema pyyaml fastapi uvicorn pydantic >nul 2>&1

echo.
echo [2/3] Checking Ollama AI model (llama3.1:latest)...
echo (This may take a minute if it needs to download for the first time)
ollama pull llama3.1:latest >nul 2>&1

echo.
echo [3/3] Launching Pipeline and UI!
echo The browser will open automatically once the server is ready.
echo.

python orchestrator.py --skip-to S4 --start-ui

pause
