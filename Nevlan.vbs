' Nevlan — launcher 100% silencioso (sin flash de consola).
' Doble click o usar este archivo como target de un acceso directo en el escritorio.
Option Explicit

Dim fso, shell, scriptDir, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

' Preferimos pythonw.exe (sin consola). Si no existe, caemos a python.exe:
' main.py ya se auto-oculta la consola al arrancar, así que tampoco se ve.
Dim pythonwExists
pythonwExists = False

Dim paths, i, p
paths = Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
For i = LBound(paths) To UBound(paths)
    p = paths(i)
    If Len(p) > 0 Then
        If fso.FileExists(fso.BuildPath(p, "pythonw.exe")) Then
            pythonwExists = True
            Exit For
        End If
    End If
Next

' Usamos main.pyw con pythonw.exe para que Windows ni siquiera CREE una
' consola (sin flash del símbolo del sistema al arrancar).
If pythonwExists Then
    cmd = "pythonw """ & fso.BuildPath(scriptDir, "main.pyw") & """"
Else
    cmd = "python """ & fso.BuildPath(scriptDir, "main.pyw") & """"
End If

' 0 = ventana oculta; False = no esperar a que termine.
shell.Run cmd, 0, False
