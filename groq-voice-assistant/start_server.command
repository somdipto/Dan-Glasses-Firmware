#!/bin/bash
# macOS: double-click to start the glasses server (same as start_server.bat on Windows).
# First time only, make it double-clickable:  chmod +x start_server.command
# Keep this window open while you use the glasses.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/macos/"
  read -r -p "Press Enter to close..." _
  exit 1
fi

# A Mac needs its own virtual environment (a Windows .venv can't run here).
if [ ! -x .venv/bin/python ]; then
  echo "Creating the Python environment (first run only)..."
  python3 -m venv .venv || { read -r -p "Press Enter to close..." _; exit 1; }
fi

echo "Installing/checking Python packages..."
.venv/bin/python -m pip install -q -r requirements.txt || {
  read -r -p "Press Enter to close..." _; exit 1; }

echo "When macOS asks whether Python may accept incoming connections, click Allow."
.venv/bin/python server/server.py
read -r -p "Server stopped. Press Enter to close..." _
