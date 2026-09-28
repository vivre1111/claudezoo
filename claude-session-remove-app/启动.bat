@echo off
rem NOTE: keep this file ASCII-only.
rem cmd.exe parses .bat using the system ANSI codepage, not UTF-8, so non-ASCII
rem bytes here get mangled and break the line structure. All Chinese output
rem comes from node (see app.js), which writes UTF-8 correctly after chcp 65001.
chcp 65001 >nul
cd /d "%~dp0"
title Claude Sessions

echo.
echo   Claude Sessions
echo   ------------------------------------------
echo   Starting local server...
echo   Your browser will open http://localhost:8756 in a moment.
echo.
echo   This server is READ-ONLY. Close this window to stop it.
echo.

rem wait ~2s, then open the browser (URL has no spaces, so no quotes needed --
rem avoids cmd /c quoting hell)
start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://localhost:8756"

node app.js

echo.
echo   Server stopped.
pause
