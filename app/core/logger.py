"""ArthurOS - Logger

Objetivo:
- Logging consistente para consola (dev) y archivo (prod/dev).
- Rotación y retención automáticas.
- Un log humano + un log JSON (máquina).
- Interceptar logging estándar de Python hacia loguru.

Requisitos:
- loguru
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger

from app.core.config import settings


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level = logger.level(record.levelname).name
        except Exception:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _level() -> str:
    return "DEBUG" if settings.DEBUG else "INFO"


def setup_logging() -> None:
    logger.remove()

    # Consola: solo si hay stderr disponible. Cuando Nevlan se lanza
    # con ``pythonw.exe`` (sin consola, modo recomendado para usuario
    # final) ``sys.stderr`` es ``None`` y ``logger.add(None, ...)`` lanza
    # ``TypeError: Cannot log to objects of type 'NoneType'`` antes de
    # que la app arranque siquiera. Bug observado el 2026-05-03.
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=_level(),
            colorize=True,
            backtrace=settings.DEBUG,
            diagnose=settings.DEBUG,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            enqueue=True,
        )

    Path(settings.LOGS_PATH).mkdir(parents=True, exist_ok=True)

    # Archivo humano
    # Usamos timestamp en el nombre para evitar bloqueos de archivo en Windows (WinError 32)
    # cada vez que se reinicia la app.
    logger.add(
        str(settings.LOGS_PATH / "arthur_{time:YYYY-MM-DD_HH-mm-ss}.log"),
        level="DEBUG",
        retention="10 days",
        compression="zip",
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} - {message}",
        enqueue=True,
    )

    # Archivo JSON
    logger.add(
        str(settings.LOGS_PATH / "arthur_{time:YYYY-MM-DD_HH-mm-ss}.jsonl"),
        level="DEBUG",
        retention="10 days",
        compression="zip",
        serialize=True,
        enqueue=True,
    )

    # logging estándar -> loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    for noisy in ("urllib3", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
log = logger
