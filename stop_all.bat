@echo off
title CoffeeManager-OS Stop All
echo ============================================================
echo  CoffeeManager-OS - STOP EVERYTHING
echo ============================================================
echo  Stops the server and the tunnel so you can start fresh.
echo  Use this if you see "port already in use", if the bot acts
echo  stuck, or just to be sure nothing is left running.
echo.
echo  NOTE: this closes ALL Python programs on this PC. That is
echo  fine for this coffee-shop machine (the app is the only one),
echo  but don't run it if you're using Python for something else.
echo ============================================================
echo.
taskkill /F /IM cloudflared.exe >nul 2>&1 && echo  Tunnel stopped. || echo  (no tunnel was running)
taskkill /F /IM python.exe >nul 2>&1 && echo  Server stopped.  || echo  (no server was running)
echo.
echo Done - everything is stopped. You can now start fresh:
echo   1) double-click start_server.bat  (wait for "Uvicorn running")
echo   2) double-click start_tunnel.bat  (wait for "webhook registered")
echo.
pause
