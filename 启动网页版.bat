@echo off
rem Double-click to start the mini coding agent web UI.
rem This window IS the server: keep it open while using the web page.
rem To stop: close this window, or press Ctrl+C here.

cd /d "%~dp0"

echo ============================================
echo   mini coding agent - starting web UI...
echo   Browser will open at http://127.0.0.1:8000
echo   Keep this window open. Close = stop server.
echo ============================================
echo.

py -3 main.py --web

echo.
echo Server stopped.
pause
