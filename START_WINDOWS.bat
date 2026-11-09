@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP_WINDOWS.bat first. & pause & exit /b 1)
if not exist ".env" (echo Missing .env. Run SETUP_WINDOWS.bat first. & pause & exit /b 1)
echo Open http://127.0.0.1:8000 in your browser
".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
