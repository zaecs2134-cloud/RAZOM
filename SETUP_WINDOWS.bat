@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (echo Python 3 is required. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo Installation failed. & pause & exit /b 1)
".venv\Scripts\python.exe" -c "import os,secrets,pathlib; p=pathlib.Path('.env'); s=pathlib.Path('.env.example').read_text(encoding='utf-8'); s=s.replace('JWT_SECRET=', 'JWT_SECRET='+secrets.token_hex(32), 1); p.write_text(s,encoding='utf-8') if not p.exists() else None; print('Configuration ready:',p.resolve())"
echo.
echo SETUP COMPLETE. Open .env and fill GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, SITE_URL.
echo Then run START_WINDOWS.bat
pause
