@echo off
rem Started hidden by the launcher vbs (scheduled task); runnable by hand too.
rem
rem ASCII-only, with an ASCII filename, on purpose: cmd parses .bat using the
rem system ANSI codepage, so any Chinese text in here gets mangled and breaks
rem the line structure -- which silently ate the `set NODE=` line below once.
rem
rem node is not on PATH for a scheduled task the way it is in an interactive
rem shell, so pin the real path and only fall back to PATH if it ever moves.
rem
rem The full app.js path matters too: it puts this folder's name into the
rem process command line, which is what the stop script matches on before it
rem is willing to kill anything.

cd /d "%~dp0"

set "NODE=C:\Users\vivre\Desktop\workspace\tools\node\node.exe"
if not exist "%NODE%" set "NODE=node"

"%NODE%" "%~dp0app.js" >> service.log 2>&1
