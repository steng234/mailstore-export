@echo off
REM Windows launcher for the MailStore Export GUI.
REM Requires Python 3 for Windows (includes Tkinter): https://www.python.org/downloads/
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw mailstore_gui.py) || (start "" python mailstore_gui.py)
