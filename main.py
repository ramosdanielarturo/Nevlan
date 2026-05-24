import os
import sys


def _hide_console_window_windows() -> None:
    """Oculta la ventana de consola heredada del lanzador (python.exe).
    No afecta si ya se lanzó con pythonw.exe (no hay consola). Para que el
    flash sea imperceptible, esto debe llamarse ANTES de cualquier import
    pesado: en caliente la consola aparece por unos milisegundos mientras
    Windows crea el proceso, y la ocultamos aquí en cuanto Python arranca.

    La forma recomendada de lanzar Nevlan es con `pythonw main.pyw` (ver
    `Nevlan.vbs` / `Nevlan.bat`), que evita que Windows cree una consola
    en primer lugar.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            # SW_HIDE = 0 → la consola sigue viva pero invisible.
            user32.ShowWindow(hwnd, 0)
            # Además liberamos la consola: si Windows tarda en pintar otra
            # cosa encima, al menos no sigue como ventana de la app.
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
    except Exception:
        pass


# IMPORTANTE: ocultar la consola ANTES que nada para minimizar el flash.
_hide_console_window_windows()

# Instalamos hooks de crashes lo antes posible: si algo en bootstrap
# explota o un thread daemon (pyautogui/pynput/UIA) crashea, queremos
# un archivo en var/crashes/ con stack trace en lugar de un cierre
# silencioso. Sin esto pasamos horas a ciegas.
from app.core.crash_handler import install_crash_handlers
install_crash_handlers()

# ─────────────────────────────────────────────────────────────────────
# Qt font engine: forzar Freetype (más tolerante con fuentes legacy)
# ─────────────────────────────────────────────────────────────────────
# Bug observado el 2026-05-02:
#   ``DirectWrite: CreateFontFaceFromHDC() failed [...] for "MS Serif"``
#   seguido de ``Windows fatal exception: access violation`` cerraba la
#   app cuando Qt intentaba renderizar texto que caía a la fuente
#   bitmap legacy "MS Serif". DirectWrite no soporta fuentes bitmap y
#   genera un access violation en lugar de devolver error.
#
# Fix de raíz: pedimos a Qt que use el motor Freetype, que sí maneja
# todos los formatos de fuente (incluyendo legacy raster) sin crashear.
# Esto debe setearse ANTES de importar PyQt6 — por eso vive aquí, antes
# de cualquier import de Qt.
#
# IMPORTANTE: ``setdefault`` solo aplica si la var NO existe. La
# sobrescribimos SIEMPRE en Windows. El crash del 2026-05-03 mostró que
# en algunos sistemas con QT_QPA_PLATFORM ya seteado a otro valor, el
# fontengine seguía siendo DirectWrite y crasheaba igual. Además
# pasamos ``-platform`` por sys.argv como cinturón+tirantes: Qt lo lee
# en QApplication() y NO depende del environment.
if sys.platform == "win32":
    _qt_platform = "windows:fontengine=freetype"
    os.environ["QT_QPA_PLATFORM"] = _qt_platform
    if "-platform" not in sys.argv:
        sys.argv.extend(["-platform", _qt_platform])

from app.bootstrap_llm_env import (
    bootstrap_llm_environment,
    configure_llm_fallback_from_environment,
    ensure_cloud_llm_configured,
)

bootstrap_llm_environment()
configure_llm_fallback_from_environment()

from app.core.logger import log

ensure_cloud_llm_configured(log)
from PyQt6.QtWidgets import QApplication

from app.interfaces.desktop.automation_center import AutomationCenter
from app.core.branding import apply_to_app, app_icon


def main():
    log.info("Nevlan — Le enseñas el proceso. Le dices cuándo. Nevlan lo hace.")

    app = QApplication(sys.argv)
    app.setApplicationName("Nevlan")
    app.setOrganizationName("Nevlan")

    # Estilo "fusion": pinta TODOS los widgets en el lado de Qt y NO
    # delega a la nativa de Windows (que en Win10 LTSC 1809 dispara
    # paint events vía DirectWrite/D3D que pueden crashear con
    # `access violation` durante el modal exec(). Bug 2026-05-03).
    # Fusion es estable en TODAS las plataformas y soporta nuestros
    # stylesheets sin perder fidelidad visual.
    try:
        app.setStyle("fusion")
        log.debug(f"Qt style: {app.style().objectName() if app.style() else 'unknown'}")
    except Exception as _e:
        log.debug(f"setStyle('fusion'): {_e}")

    # Fuente default explícita para evitar que Qt elija "MS Serif"
    # (fuente bitmap legacy) como fallback. Si DirectWrite intenta
    # renderizar MS Serif → access violation → la app cierra. Segoe UI
    # es la fuente nativa moderna de Windows desde Vista; siempre
    # disponible.
    try:
        from PyQt6.QtGui import QFont
        app.setFont(QFont("Segoe UI", 9))
    except Exception:
        pass

    # Diagnóstico: log del fontengine y plataforma efectivos para
    # auditar que el fix de Freetype está activo.
    try:
        from PyQt6.QtGui import QGuiApplication
        plat = QGuiApplication.platformName() if QGuiApplication.instance() else "?"
        log.info(
            f"Qt platform={plat}, "
            f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', 'unset')}"
        )
    except Exception:
        pass

    apply_to_app(app)

    window = AutomationCenter()
    icon = app_icon()
    if icon:
        window.setWindowIcon(icon)
    # Abrimos maximizado: Nevlan es una app de trabajo; arrancar con la
    # ventana pequeña obligaba al usuario a dar click en "expandir" cada vez.
    window.showMaximized()

    log.info("Nevlan: Centro de Automatizaciones cargado.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
