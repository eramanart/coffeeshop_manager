@echo off
title CoffeeManager - Starting
echo ============================================================
echo   CoffeeManager - one-click start
echo ============================================================
echo.
echo Clearing anything left running from before...
taskkill /F /IM cloudflared.exe >nul 2>&1
taskkill /F /IM python.exe      >nul 2>&1
timeout /t 2 >nul

echo Starting the agent (server)...
start "CoffeeManager Server" /D "C:\Users\eligi\Desktop\coffee_agent\python" "C:\Users\eligi\Desktop\coffee_agent\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8000

echo Waiting a few seconds for the server...
timeout /t 5 >nul

echo Starting the connection (tunnel)...
start "CoffeeManager Tunnel" "C:\Users\eligi\Desktop\coffee_agent\start_tunnel.bat"

echo Opening the dashboard...
timeout /t 2 >nul
start "" http://127.0.0.1:8000

echo.
echo ============================================================
echo  CoffeeManager is starting.
echo   - Two windows opened (Server + Tunnel) - LEAVE THEM OPEN.
echo   - The tunnel may take 1-3 minutes to connect (normal).
echo     The dashboard works immediately; the bot works once the
echo     tunnel window says "webhook registered".
echo   - Dashboard login: owner / your dashboard password.
echo ============================================================
timeout /t 8 >nul
