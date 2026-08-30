#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer was not found. Install Python 3, then run this file again."
  read -r -p "Press Return to close..."
  exit 1
fi

fail() {
  STATUS="$1"
  echo
  echo "Installation or startup failed. Copy the error above when asking for help."
  read -r -p "Press Return to close..."
  exit "$STATUS"
}

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required."
  fail 1
}

if [ ! -x ".venv-macos/bin/python" ]; then
  python3 -m venv .venv-macos || {
    echo "Could not create the local Python environment."
    fail 1
  }
fi

.venv-macos/bin/python -m pip install --upgrade pip || fail $?
.venv-macos/bin/python -m pip install -r requirements.txt || fail $?
.venv-macos/bin/python -m playwright install chromium || fail $?
.venv-macos/bin/python schoology_downloader.py
STATUS=$?

echo
read -r -p "Press Return to close..."
exit "$STATUS"
