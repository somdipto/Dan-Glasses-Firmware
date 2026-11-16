@echo off
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Python not found. Install it from python.org and tick "Add to PATH".
  pause
  exit /b 1
)
echo.
echo === Step 1: flashing the firmware ===
python tools\device.py flash
if errorlevel 1 goto end
echo.
echo === Step 2: sending this laptop's Wi-Fi to the ESP32 ===
echo (make sure start_server.bat is running in another window)
python tools\device.py setup
:end
pause
