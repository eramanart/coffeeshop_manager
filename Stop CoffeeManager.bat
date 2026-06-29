@echo off
title CoffeeManager - Stopping
echo Stopping CoffeeManager (server + tunnel)...
taskkill /F /IM cloudflared.exe >nul 2>&1
taskkill /F /IM python.exe      >nul 2>&1
echo.
echo Done - everything is stopped.
timeout /t 3 >nul
