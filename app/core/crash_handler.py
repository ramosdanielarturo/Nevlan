"""Captura de crashes para Nevlan — diagnóstico real, no oscuridad.

Problema observado (2026-05-01):
    Tras detener una grabación y abrir la pantalla de revisión, la app
    cerraba sin dejar rastro en los logs. La última línea escrita era
    ``mission_review:__init__:165 - Review de automatización: ...``
    y luego silencio. Eso es típico de:
      - segfault en Qt (driver de GPU + WA_TranslucentBackground)
      - excepción no capturada en thread de pyautogui / pynput
      - llamada UIA/COM mal hilo
      - C-extension corrompiendo memoria
    Sin un hook robusto no hay forma de saber QUÉ está fallando.

Lo que instala este módulo (cuando llamas ``install_crash_handlers``):

  1. ``sys.excepthook``:    excepciones del MainThread no capturadas.
  2. ``threading.excepthook``: excepciones de threads no capturadas
     (Python 3.8+).
  3. ``faulthandler``: dumps Python+nativos al recibir señal o segfault
     (escribe stacks de TODOS los hilos al archivo).
  4. ``Qt MessageHandler``: captura warnings/críticos de PyQt6 que el
     usuario no ve por ir a stderr.
  5. ``atexit``: marca cierre normal vs anormal.

Cada crash crea un archivo en ``var/crashes/crash_<ts>_<reason>.log``
con flush inmediato. NUNCA borramos crashes — el usuario los necesita
para diagnóstico.

Diseño:
  - Idempotente: ``install_crash_handlers()`` es seguro de llamar varias
    veces; solo instala una vez.
  - Tolerante: si algo falla durante la instalación (e.g. faulthandler
    no disponible), seguimos con los hooks que sí pudimos poner.
  - Sin dependencias pesadas: solo stdlib + PyQt6 (opcional).
"""
from __future__ import annotations

import atexit
import faulthandler
import os
import platform
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO


_INSTALLED = False
_FAULTHANDLER_FILE: Optional[TextIO] = None


def _crashes_dir() -> Path:
    """``var/crashes/`` — siempre relativo al PROJECT_ROOT detectado."""
    try:
        from app.core.config import settings
        d = settings.VAR_PATH / "crashes"
    except Exception:
        d = Path.cwd() / "var" / "crashes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _system_info_block() -> str:
    """Una línea por dato — útil para correlacionar crashes."""
    try:
        import PyQt6.QtCore as _qc
        qt_ver = _qc.PYQT_VERSION_STR
    except Exception:
        qt_ver = "n/a"
    parts = [
        f"timestamp_utc={datetime.now(timezone.utc).isoformat()}",
        f"python={sys.version.split()[0]}",
        f"platform={platform.platform()}",
        f"machine={platform.machine()}",
        f"pyqt={qt_ver}",
        f"executable={sys.executable}",
        f"cwd={os.getcwd()}",
        f"pid={os.getpid()}",
        f"main_thread={threading.main_thread().name}",
    ]
    return "\n".join(parts)


def _alive_threads_block() -> str:
    """Lista todos los threads vivos al momento del crash. Si el crash
    es por race con pyautogui/pynput, esto delata al culpable.
    """
    try:
        active = threading.enumerate()
    except Exception:
        return "active_threads=ENUM_FAILED"
    out = [f"active_threads={len(active)}"]
    for t in active:
        out.append(f"  - {t.name} daemon={t.daemon} alive={t.is_alive()} ident={t.ident}")
    return "\n".join(out)


def _write_crash(reason: str, body: str, *, also_log: bool = True) -> Optional[Path]:
    """Escribe un crash a disco con flush inmediato. Devuelve la ruta."""
    try:
        path = _crashes_dir() / f"crash_{_now_tag()}_{reason}.log"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"=== Nevlan crash report — {reason} ===\n")
            f.write(_system_info_block())
            f.write("\n\n")
            f.write(_alive_threads_block())
            f.write("\n\n")
            f.write("=== payload ===\n")
            f.write(body)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        if also_log:
            try:
                from app.core.logger import log
                log.error(f"[CRASH-CAPTURED] {reason} → {path}")
            except Exception:
                pass
        return path
    except Exception:
        # Último recurso: stderr, aunque no sobreviva al crash.
        try:
            sys.stderr.write(f"[CRASH-WRITE-FAILED] {reason}\n{body}\n")
            sys.stderr.flush()
        except Exception:
            pass
        return None


# ── 1. sys.excepthook ──────────────────────────────────────────────────

_PREV_EXCEPTHOOK = sys.excepthook


def _hook_main(exc_type, exc_value, exc_tb) -> None:
    body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_crash("main_exception", body)
    try:
        _PREV_EXCEPTHOOK(exc_type, exc_value, exc_tb)
    except Exception:
        pass


# ── 2. threading.excepthook ────────────────────────────────────────────

def _hook_thread(args) -> None:  # type: ignore[no-untyped-def]
    """Firma: ExceptHookArgs(exc_type, exc_value, exc_traceback, thread)."""
    try:
        exc_type = args.exc_type
        exc_value = args.exc_value
        exc_tb = args.exc_traceback
        thread = args.thread
    except Exception:
        exc_type = exc_value = exc_tb = None
        thread = None
    body_parts = []
    if thread is not None:
        body_parts.append(f"thread_name={thread.name}\n")
        body_parts.append(f"thread_daemon={thread.daemon}\n\n")
    body_parts.append(
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        if exc_type is not None
        else "no traceback"
    )
    _write_crash("thread_exception", "".join(body_parts))


# ── 3. faulthandler ────────────────────────────────────────────────────

def _install_faulthandler() -> None:
    """Activa ``faulthandler`` para volcar stacks ante segfaults o
    señales. Mantiene un archivo abierto (no se cierra hasta el final
    del proceso) para que el dump funcione incluso si el proceso muere
    sin cleanup ordenado.
    """
    global _FAULTHANDLER_FILE
    try:
        path = _crashes_dir() / f"faulthandler_{_now_tag()}.log"
        f = open(path, "w", encoding="utf-8")
        # ``faulthandler.enable`` requiere un fileno() válido.
        faulthandler.enable(file=f, all_threads=True)
        # NO cerramos el archivo: lo necesitamos vivo si ocurre
        # un segfault. Lo retenemos en una global para evitar GC.
        _FAULTHANDLER_FILE = f
    except Exception:
        pass


# ── 4. Qt message handler ─────────────────────────────────────────────

def _install_qt_handler() -> None:
    """Captura logs de Qt (Critical/Fatal) que de otra forma irían a
    stderr y se perderían. Solo se instala si PyQt6 está disponible.
    """
    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType  # type: ignore
    except Exception:
        return

    def _handler(msg_type, context, message):  # type: ignore[no-untyped-def]
        try:
            level = {
                QtMsgType.QtDebugMsg: "qt_debug",
                QtMsgType.QtInfoMsg: "qt_info",
                QtMsgType.QtWarningMsg: "qt_warning",
                QtMsgType.QtCriticalMsg: "qt_critical",
                QtMsgType.QtFatalMsg: "qt_fatal",
                QtMsgType.QtSystemMsg: "qt_system",
            }.get(msg_type, "qt_unknown")
        except Exception:
            level = "qt_unknown"
        # Solo persistimos los relevantes (warning+) para no llenar disco.
        if level in {"qt_critical", "qt_fatal", "qt_warning"}:
            ctx_parts = []
            try:
                if getattr(context, "file", None):
                    ctx_parts.append(f"file={context.file}:{context.line}")
                if getattr(context, "function", None):
                    ctx_parts.append(f"func={context.function}")
            except Exception:
                pass
            ctx = " ".join(ctx_parts)
            _write_crash(level, f"{ctx}\n{message}", also_log=(level != "qt_warning"))

    try:
        qInstallMessageHandler(_handler)
    except Exception:
        pass


# ── 5. atexit ──────────────────────────────────────────────────────────

def _hook_atexit() -> None:
    """Marca el final del proceso. Útil para distinguir cierre normal
    vs. crash silencioso.

    Implementación tolerante: durante shutdown los sinks de loguru
    pueden estar ya cerrados; tragamos cualquier error para no
    contaminar stderr con tracebacks de ``ValueError: I/O operation
    on closed file.``
    """
    try:
        from app.core.logger import log
        log.info(f"[CRASH-HANDLER] proceso pid={os.getpid()} terminando normalmente")
    except Exception:
        try:
            sys.stderr.write(
                f"[CRASH-HANDLER] proceso pid={os.getpid()} terminando normalmente\n"
            )
        except Exception:
            pass


# ── API pública ────────────────────────────────────────────────────────

def install_crash_handlers() -> None:
    """Instala todos los hooks de captura de crashes. Idempotente."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    sys.excepthook = _hook_main

    try:
        threading.excepthook = _hook_thread  # type: ignore[assignment]
    except Exception:
        pass

    _install_faulthandler()
    _install_qt_handler()

    try:
        atexit.register(_hook_atexit)
    except Exception:
        pass


def write_manual_crash(reason: str, exc: BaseException) -> Optional[Path]:
    """API pública para que cualquier handler ``except`` deje rastro
    aunque luego degrade silenciosamente. Útil en signal/slot Qt donde
    una excepción se traga.
    """
    body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _write_crash(reason, body)


__all__ = ["install_crash_handlers", "write_manual_crash"]
