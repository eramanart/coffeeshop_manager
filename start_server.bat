@echo off
title CoffeeManager-OS Server
echo ============================================================
echo  CoffeeManager-OS - SERVER
echo ============================================================
echo  Starting the server at http://127.0.0.1:8000
echo.
echo  Leave this window OPEN. Close it (or press Ctrl+C) to stop.
echo ============================================================
echo.
cd /d C:\Users\eligi\Desktop\coffee_agent\python
C:\Users\eligi\Desktop\coffee_agent\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
echo.
echo Server stopped.
pause
