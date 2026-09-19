@echo off
color 0B
echo =======================================================
echo     AaltoAI - UI Dashboard Launcher
echo =======================================================
echo.
echo Launching the Operator Dashboard using pre-computed data...

start http://localhost:8000/ui/
python ui/server.py
pause
