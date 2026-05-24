"""
Nevlan — Branding helper
------------------------
Genera y cachea el icono de la app (.ico) para ventanas, taskbar y empaquetado.
Si el asset no existe, lo dibuja con PIL. Evita depender de binarios externos.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from app.core.logger import log

try:
    from PyQt6.QtGui import QIcon
    _HAS_QT = True
except Exception:
    _HAS_QT = False

_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
ICON_ICO = _ASSETS_DIR / "nevlan.ico"
ICON_PNG = _ASSETS_DIR / "nevlan.png"

# Paleta Nevlan (coherente con NevlanTheme del UI)
_BG1 = (17, 17, 24)
_BG2 = (30, 30, 42)
_ACCENT = (9, 132, 227)
_HIGHLIGHT = (116, 185, 255)


def _draw_mark(size: int):
    """Dibuja un diamante con 'N' en el centro y un fondo tipo glass."""
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Fondo redondeado con gradiente simulado
    pad = max(2, size // 16)
    radius = size // 5
    # sombra
    d.rounded_rectangle(
        [pad + 1, pad + 2, size - pad + 1, size - pad + 2],
        radius=radius, fill=(0, 0, 0, 110)
    )
    d.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius, fill=_BG1
    )

    # Diamante accent
    cx, cy = size // 2, size // 2
    rhombus_r = int(size * 0.32)
    d.polygon(
        [(cx, cy - rhombus_r), (cx + rhombus_r, cy),
         (cx, cy + rhombus_r), (cx - rhombus_r, cy)],
        fill=_ACCENT
    )

    # Letra N
    try:
        font_size = int(size * 0.38)
        font = ImageFont.truetype("segoeuib.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("arialbd.ttf", int(size * 0.38))
        except Exception:
            font = ImageFont.load_default()

    text = "N"
    try:
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = cx - tw // 2 - bbox[0]
        ty = cy - th // 2 - bbox[1]
    except Exception:
        tw, th = d.textsize(text, font=font)  # type: ignore[attr-defined]
        tx, ty = cx - tw // 2, cy - th // 2
    d.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)

    return img


def _generate_icon() -> bool:
    """Genera assets/nevlan.ico y nevlan.png con múltiples tamaños."""
    try:
        from PIL import Image  # type: ignore
    except Exception as e:
        log.warning(f"branding: PIL no disponible, sin icono ({e})")
        return False

    _ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master = _draw_mark(256)

    try:
        master.save(ICON_PNG, format="PNG")
    except Exception as e:
        log.debug(f"branding: error guardando PNG: {e}")

    try:
        master.save(
            ICON_ICO,
            format="ICO",
            sizes=[(s, s) for s in sizes],
        )
        return True
    except Exception as e:
        log.error(f"branding: error escribiendo ICO: {e}")
        return False


def ensure_icon() -> Optional[Path]:
    """Garantiza que exista el icono; lo genera si falta. Retorna path al .ico."""
    if ICON_ICO.exists() and ICON_ICO.stat().st_size > 0:
        return ICON_ICO
    if _generate_icon() and ICON_ICO.exists():
        return ICON_ICO
    if ICON_PNG.exists():
        return ICON_PNG
    return None


def apply_to_app(app) -> None:
    """Aplica icono y AppUserModelID en Windows para que el taskbar use Nevlan."""
    icon_path = ensure_icon()
    if not icon_path or not _HAS_QT:
        return

    try:
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))
    except Exception as e:
        log.debug(f"branding: no se pudo aplicar QIcon global: {e}")

    # Windows: desacopla el icono del proceso Python en el taskbar
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Nevlan.App.Desktop.1")  # type: ignore[attr-defined]
        except Exception as e:
            log.debug(f"branding: AppUserModelID no aplicado: {e}")


def app_icon():
    """QIcon reutilizable."""
    if not _HAS_QT:
        return None
    p = ensure_icon()
    return QIcon(str(p)) if p else None
