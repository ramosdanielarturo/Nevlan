"""Aliases canónicos para LAUNCH_APP.

El usuario puede grabar "Chrome", "google chrome", "GOOGLE-CHROME" — todos
deben mapearse al mismo proceso (``chrome.exe``) y mostrar el mismo nombre
amigable ("Google Chrome"). Esta tabla es la fuente de verdad para:

  - el compilador (``_post_pass_windows_search_launch``): usa el nombre
    canónico para el ``app_name`` del payload.
  - el player (LAUNCH_APP): mira si la app ya corre antes de gastar un
    ``Win`` + Windows Search, y si la conoce intenta lanzarla por
    ``os.startfile``/``Start-Process`` para evitar sleeps fijos.
  - los tests: aseguran que las distintas variantes locale/typo terminen
    en el mismo paso compilado.

NO depender de variables de entorno o settings: este módulo es pure-data
para que importarlo sea seguro en headless / sin Windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class AppAlias:
    canonical: str          # "Chrome" — corto, así lo lee el humano
    display_name: str       # "Google Chrome" — para overlay/UI
    process_names: Tuple[str, ...]  # tal cual aparecen en psutil/Windows
    aliases: Tuple[str, ...]        # variantes que el usuario puede tipear


# Tabla de apps conocidas. Lo demás cae a "abrir por nombre tal cual".
_KNOWN_APPS: Tuple[AppAlias, ...] = (
    AppAlias(
        canonical="Chrome",
        display_name="Google Chrome",
        process_names=("chrome.exe",),
        aliases=("chrome", "google chrome", "google-chrome", "chromium"),
    ),
    AppAlias(
        canonical="Edge",
        display_name="Microsoft Edge",
        process_names=("msedge.exe",),
        aliases=("edge", "microsoft edge", "msedge"),
    ),
    AppAlias(
        canonical="Firefox",
        display_name="Mozilla Firefox",
        process_names=("firefox.exe",),
        aliases=("firefox", "mozilla firefox"),
    ),
    AppAlias(
        canonical="Brave",
        display_name="Brave",
        process_names=("brave.exe",),
        aliases=("brave", "brave browser"),
    ),
    AppAlias(
        canonical="Excel",
        display_name="Microsoft Excel",
        process_names=("EXCEL.EXE", "excel.exe"),
        aliases=("excel", "microsoft excel", "ms excel"),
    ),
    AppAlias(
        canonical="Word",
        display_name="Microsoft Word",
        process_names=("WINWORD.EXE", "winword.exe"),
        aliases=("word", "microsoft word", "ms word", "winword"),
    ),
    AppAlias(
        canonical="PowerPoint",
        display_name="Microsoft PowerPoint",
        process_names=("POWERPNT.EXE", "powerpnt.exe"),
        aliases=("powerpoint", "microsoft powerpoint", "ms powerpoint",
                 "ppt"),
    ),
    AppAlias(
        canonical="Outlook",
        display_name="Microsoft Outlook",
        process_names=("OUTLOOK.EXE", "outlook.exe"),
        aliases=("outlook", "microsoft outlook"),
    ),
    AppAlias(
        canonical="VS Code",
        display_name="Visual Studio Code",
        process_names=("Code.exe", "code.exe"),
        aliases=("vscode", "vs code", "visual studio code", "code editor"),
    ),
    AppAlias(
        canonical="Notepad",
        display_name="Notepad",
        process_names=("notepad.exe",),
        aliases=("notepad", "bloc de notas"),
    ),
    AppAlias(
        canonical="Calculadora",
        display_name="Calculator",
        process_names=("Calculator.exe", "calc.exe"),
        aliases=("calculator", "calculadora", "calc"),
    ),
    AppAlias(
        canonical="Explorador de archivos",
        display_name="File Explorer",
        process_names=("explorer.exe",),
        aliases=("file explorer", "explorador de archivos", "explorer",
                 "windows explorer"),
    ),
    AppAlias(
        canonical="Spotify",
        display_name="Spotify",
        process_names=("Spotify.exe",),
        aliases=("spotify",),
    ),
)


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def resolve_app_alias(name: str) -> AppAlias:
    """Devuelve el AppAlias que mejor matchea ``name``.

    Si no hay match, fabrica un alias trivial (canonical=name) para no
    romper el código que llama. Esto permite que el player siga
    intentando abrir apps que no estén en la tabla (delegando a Windows
    Search), mientras el compilador puede normalizar nombre.
    """
    n = _norm(name)
    if not n:
        return AppAlias(canonical="", display_name="",
                        process_names=(), aliases=())
    for app in _KNOWN_APPS:
        if n == _norm(app.canonical) or n == _norm(app.display_name):
            return app
        for a in app.aliases:
            if n == _norm(a):
                return app
        for p in app.process_names:
            if n == _norm(p):
                return app
    short = name.strip()
    return AppAlias(canonical=short, display_name=short,
                    process_names=(), aliases=())


def canonical_app_name(name: str) -> str:
    """Nombre amigable para mostrarle al usuario."""
    return resolve_app_alias(name).canonical or (name or "").strip()


def process_names_for(name: str) -> Tuple[str, ...]:
    """Lista de nombres de proceso candidatos para ``name``."""
    return resolve_app_alias(name).process_names


def is_app_running(name: str) -> Optional[bool]:
    """¿Hay un proceso para esta app actualmente corriendo?

    Devuelve ``None`` si no podemos determinarlo (sin psutil o sin
    procesos conocidos). El player usa esta señal para enfocar la
    ventana existente en lugar de lanzar una nueva instancia.
    """
    procs = process_names_for(name)
    if not procs:
        return None
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    procs_lower = {p.lower() for p in procs}
    try:
        for proc in psutil.process_iter(attrs=("name",)):
            try:
                pname = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if pname in procs_lower:
                return True
    except Exception:
        return None
    return False


def display_name_for(name: str) -> str:
    """Nombre largo / "marketing" para overlays opcionales."""
    return resolve_app_alias(name).display_name or canonical_app_name(name)


def known_aliases() -> List[str]:
    """Lista útil para tests: incluye alias y nombres canónicos."""
    out: List[str] = []
    for app in _KNOWN_APPS:
        out.append(app.canonical)
        out.append(app.display_name)
        out.extend(app.aliases)
    return out


__all__ = [
    "AppAlias",
    "resolve_app_alias",
    "canonical_app_name",
    "display_name_for",
    "process_names_for",
    "is_app_running",
    "known_aliases",
]
