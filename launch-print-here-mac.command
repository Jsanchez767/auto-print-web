#!/bin/bash
# Auto-Print — open the "Print Here" receiver in Chrome kiosk-printing mode.
# Jobs sent from the website then print SILENTLY on this Mac's default printer.
#
# 1) Set PRINT_HERE_URL below to your server's /print-here page.
# 2) Double-click this file (you may need: right-click → Open the first time).

PRINT_HERE_URL="https://auto-print-jnpe.onrender.com/print-here"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

if [ -x "$CHROME" ]; then
  BROWSER="$CHROME"
elif [ -x "$EDGE" ]; then
  BROWSER="$EDGE"
else
  echo "Google Chrome or Microsoft Edge is required for silent printing."
  echo "Install Chrome, then run this again."
  read -r -p "Press Return to close."
  exit 1
fi

# A dedicated profile keeps the kiosk-printing setting isolated.
PROFILE="$HOME/.config/auto-print-kiosk"

exec "$BROWSER" \
  --kiosk-printing \
  --user-data-dir="$PROFILE" \
  --app="$PRINT_HERE_URL"
