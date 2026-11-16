#!/bin/bash
# macOS/Linux: run idf.py for this project (same as idf.ps1 on Windows).
# Examples:  ./idf.sh build      ./idf.sh -p /dev/cu.usbmodem101 flash monitor
# Needs ESP-IDF v5.5.x. Uses $IDF_PATH if set, otherwise looks in the usual places.
cd "$(dirname "$0")" || exit 1

if [ -z "$IDF_PATH" ] || [ ! -f "$IDF_PATH/export.sh" ]; then
  for candidate in "$HOME"/esp/esp-idf "$HOME"/esp/v5.5*/esp-idf "$HOME"/.espressif/v5.5*/esp-idf; do
    if [ -f "$candidate/export.sh" ]; then IDF_PATH="$candidate"; break; fi
  done
fi
if [ -z "$IDF_PATH" ] || [ ! -f "$IDF_PATH/export.sh" ]; then
  echo "ESP-IDF v5.5 not found. Install it (https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/get-started/)"
  echo "or set IDF_PATH to its folder, then run this again."
  exit 1
fi

# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" >/dev/null || exit 1
exec idf.py "$@"
