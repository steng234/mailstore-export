' Windows launcher (no console window) for the MailStore Export GUI.
' Double-click this file. To give it the green icon, create a shortcut to it and
' set the shortcut icon to app_icon.ico.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "pythonw """ & dir & "\mailstore_gui.py""", 0, False
