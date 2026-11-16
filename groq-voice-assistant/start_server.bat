@echo off
cd /d "%~dp0"
echo Installing/checking Python packages...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Python not found. Install it from python.org and tick "Add to PATH".
  pause
  exit /b 1
)
python server\server.py
pause
