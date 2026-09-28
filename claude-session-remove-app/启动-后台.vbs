' Claude Sessions - start the local service with no console window.
'
' Called by the scheduled task "ClaudeSessions" (at logon, 2 min delay).
' Can also be run by hand to bring the service back up.
'
' Keep this file ASCII-only. wscript parses .vbs using the system ANSI
' codepage, so non-ASCII bytes here get mangled.
'
' The actual launch lives in service-run.bat.

Option Explicit

Dim sh, fso, base, batPath

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = fso.BuildPath(base, "service-run.bat")

sh.CurrentDirectory = base

' 0 = hidden window, False = don't wait; the service outlives this script
sh.Run Chr(34) & batPath & Chr(34), 0, False
