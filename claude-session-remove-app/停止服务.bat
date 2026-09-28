@echo off
rem ASCII-only on purpose: cmd.exe parses .bat with the system ANSI codepage,
rem so Chinese text in here would come out as garbage and break the lines.

chcp 65001 >nul
title Stop Claude Sessions
echo.

rem Only kill the process on port 8756 if its command line really is this app.
rem If something else grabbed the port, leave it alone.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $c) { '  Nothing is listening on port 8756.' } else { $p = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $c.OwningProcess) -ErrorAction SilentlyContinue; if ($p -and $p.CommandLine -like '*claude-session-remove-app*') { Stop-Process -Id $c.OwningProcess -Force; '  Claude Sessions service stopped (pid ' + $c.OwningProcess + ').' } else { '  Port 8756 is held by another program - not touching it.' } }"

echo.
pause
