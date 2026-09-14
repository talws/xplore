' start-collector.vbs — launch the X bookmark collector server with no console window.
'
' The server has to be running before the browser extension can deliver anything;
' when it is not, the extension's panel says "server not running (port 8502)" and
' the day's bookmarks are simply never captured. So it starts at login rather
' than depending on someone remembering.
'
' Run via cmd rather than pythonw directly so that stdout and stderr can be
' redirected: a server that dies silently is worse than one that dies loudly,
' and server.log is the only place the reason would appear.

Dim sh, fso, dir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve the project directory from this script's own location, so moving the
' folder does not break startup.
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' 0 = hidden window, False = do not wait for it to exit.
sh.Run "cmd /c python """ & dir & "\server.py"" >> """ & dir & "\server.log"" 2>&1", 0, False
