@echo off
REM Nevlan — launcher sin consola visible.
REM Doble clic aquí o crear acceso directo apuntando a este .bat para abrir Nevlan.

setlocal
cd /d "%~dp0"

REM Preferimos pythonw (sin consola). Si no existe, caemos a python.
REM main.pyw evita que Windows cree una consola al lanzar la app.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "Nevlan" pythonw "main.pyw"
) else (
    start "Nevlan" python "main.pyw"
)
endlocal
