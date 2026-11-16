# Wi-Fi setup for a dual-band router whose 2.4 GHz side shares the laptop's
# network name: answers "y" to device.py's "looks 5 GHz only, continue?" prompt.
# The Wi-Fi password is read by device.py from Windows and never printed.
Set-Location (Split-Path $PSScriptRoot -Parent)
'y' | & .\.venv\Scripts\python.exe tools\device.py --port COM6 setup
