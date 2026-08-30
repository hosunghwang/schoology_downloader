#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if [ ! -x ".venv-macos/bin/python" ]; then
  echo "The local environment is not installed yet."
  echo "Run install_and_run.command first."
  read -r -p "Press Return to close..."
  exit 1
fi

.venv-macos/bin/python schoology_downloader.py "$@"
STATUS=$?

echo
read -r -p "Press Return to close..."
exit "$STATUS"
