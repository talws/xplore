# create-shortcut.ps1 - put an Xplore icon on the desktop.
#
#   powershell -ExecutionPolicy Bypass -File create-shortcut.ps1
#
# The shortcut runs xplore.pyw with the real pythonw.exe. The one on PATH under
# WindowsApps is an app-execution alias, and shortcuts that target an alias are
# unreliable, so the interpreter's own path is asked for instead.
# ASCII only: Windows PowerShell 5.1 reads a script without a BOM in the legacy codepage.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = python -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found at $pythonw" }

$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Xplore.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $pythonw
$s.Arguments = '"' + (Join-Path $here "xplore.pyw") + '"'
$s.WorkingDirectory = $here
$s.IconLocation = (Join-Path $here "assets\xplore.ico") + ",0"
$s.Description = "Xplore - your X bookmarks, archived and searchable"
$s.Save()
"Created $lnk"
"  runs $pythonw $($s.Arguments)"
