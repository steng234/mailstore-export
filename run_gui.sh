#!/bin/sh
# Unix launcher (macOS + Linux) for the MailStore Export GUI.
# On macOS it needs Homebrew's python3.12 (the system 3.9 Tk is broken); on Linux
# the distro python3 with python3-tk works fine.
DIR="$(cd "$(dirname "$0")" && pwd)"
for PY in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.12 python3; do
  if command -v "$PY" >/dev/null 2>&1; then
    exec "$PY" "$DIR/mailstore_gui.py"
  fi
done
echo "Python 3 con Tkinter non trovato."
echo "  macOS: brew install python-tk@3.12"
echo "  Linux: sudo apt install python3 python3-tk   (o equivalente)"
exit 1
