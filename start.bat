@echo off
REM Start the X bookmark collector by hand. Safe to run when it is already
REM running: the server checks the port first and exits quietly if so.
wscript.exe "%~dp0start-collector.vbs"
echo Starting collector... open http://localhost:8502
