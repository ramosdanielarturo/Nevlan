# Crea un acceso directo "Nevlan" en el escritorio que lanza la app
# sin consola y con el icono oficial.
# Uso: boton derecho sobre este archivo -> "Ejecutar con PowerShell".

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "Nevlan.vbs"
$iconPath = Join-Path $root "assets\nevlan.ico"

if (-not (Test-Path $target)) {
    Write-Host "No existe $target" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $iconPath)) {
    Write-Host "Generando icono Nevlan..." -ForegroundColor Cyan
    Push-Location $root
    try {
        python -c "from app.core.branding import ensure_icon; ensure_icon()"
    } finally {
        Pop-Location
    }
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Nevlan.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Description = "Nevlan - Centro de Automatizaciones"
if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Save()

Write-Host "Acceso directo creado: $lnk" -ForegroundColor Green
Write-Host "Ya puedes abrir Nevlan desde el escritorio con el icono oficial." -ForegroundColor Green
