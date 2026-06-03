#!/bin/zsh
# Launcher per la GUI MailStore (doppio click da Finder).
# Usa il Python di Homebrew con Tcl/Tk funzionante (il Tk di sistema 3.9 è rotto
# su questo macOS). Lo script di export gira come subprocess dello stesso Python.
cd "$(dirname "$0")" || exit 1
PY=/opt/homebrew/bin/python3.12
if [ ! -x "$PY" ]; then
  echo "Python 3.12 di Homebrew non trovato in $PY"
  echo "Installa con: brew install python-tk@3.12"
  exit 1
fi
exec "$PY" mailstore_gui.py
