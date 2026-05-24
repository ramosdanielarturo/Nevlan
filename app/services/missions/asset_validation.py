"""Validación defensiva de archivos de assets de una misión.

Bug observado el 2026-05-03:
    El recorder a veces fallaba al mover un snapshot de
    ``var/missions/assets/temp/`` a la carpeta de la misión
    (``WinError 2`` por race con el filesystem). El log mostraba:

        Failed to move snapshot: [WinError 2] El sistema no puede
        encontrar el archivo especificado:
        '...\\assets\\temp\\step_b09f476f.png' ->
        '...\\assets\\<mid>\\step_b09f476f.png'

    El archivo destino sí existía, pero el ``image_ref`` del
    ``CompiledStep`` quedaba apuntando al path origen ya inexistente.
    Cuando ``MissionReviewDialog`` intentaba renderizar un thumbnail
    con ``QPixmap(path_huérfano)`` Qt disparaba un paint event lazy
    que crasheaba con ``access violation`` y mataba toda la app.

Este módulo aísla la validación de los thumbnails para que cualquier
consumidor (review, store, optimizer, weak repair) trate los assets
como datos NO confiables. Las funciones nunca lanzan: a la peor,
devuelven False / None.

Diseño:
  - Sin dependencias de PyQt6: testeable headless.
  - Sin side-effects: solo lee del filesystem.
  - Validación estricta: existe + no vacío + firma PNG válida. Esto
    elimina archivos de 0 bytes (race del grabador) y archivos que
    fueron sobreescritos por otra cosa.
"""
from __future__ import annotations

import os
from typing import Any, Optional


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
"""Firma de 8 bytes que TODO PNG válido empieza. Si no coincide,
``QPixmap`` puede ir por una rama interna que crashea (observado en
PyQt6 6.10.1 en Win10 LTSC 1809)."""


def is_safe_png_path(path: Any) -> bool:
    """True si ``path`` apunta a un archivo PNG existente, no vacío y
    con firma PNG válida. False en cualquier otro caso, sin lanzar.
    """
    try:
        if not path:
            return False
        p = str(path)
        if not os.path.exists(p):
            return False
        # Un PNG válido pesa ≥ 67 bytes (firma + IHDR + IDAT mínimo +
        # IEND). Si pesa <8, ni siquiera tiene la firma — descartar.
        size = os.path.getsize(p)
        if size <= 8:
            return False
        with open(p, "rb") as f:
            head = f.read(8)
        return head == _PNG_SIGNATURE
    except Exception:
        return False


def safe_png_or_none(path: Any) -> Optional[str]:
    """Devuelve ``path`` (str) si pasa la validación, ``None`` si no.

    Útil para construir un ``image_ref`` saneado en una sola línea:
        tc.image_ref = safe_png_or_none(tc.image_ref)
    """
    return str(path) if is_safe_png_path(path) else None


_METADATA_SNAPSHOT_KEYS = (
    "snapshot_small",
    "snapshot_mid",
    "snapshot_full",
    "pre_action_snapshot_full",
)


def _sanitize_png_string_field(holder: Any, field: str) -> int:
    """Blanquea un campo string si no apunta a un PNG válido."""
    try:
        cur = holder.get(field) if isinstance(holder, dict) else getattr(holder, field, None)
    except Exception:
        return 0
    if not cur:
        return 0
    if is_safe_png_path(cur):
        return 0
    try:
        if isinstance(holder, dict):
            holder.pop(field, None)
        else:
            setattr(holder, field, None)
    except Exception:
        return 0
    return 1


def _sanitize_metadata_png_paths(md: Any) -> int:
    """Recorre metadata y blanquea paths .png inválidos (incl. anidados)."""
    if not isinstance(md, dict):
        return 0
    sanitized = 0
    for key in _METADATA_SNAPSHOT_KEYS:
        sanitized += _sanitize_png_string_field(md, key)
    ts = md.get("target_signature")
    if isinstance(ts, dict):
        sanitized += _sanitize_nested_png_paths(ts)
    return sanitized


def _sanitize_nested_png_paths(obj: Any) -> int:
    """Blanquea strings ``*.png`` inválidos dentro de dict/list anidados."""
    sanitized = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and v.lower().endswith(".png"):
                if not is_safe_png_path(v):
                    obj.pop(k, None)
                    sanitized += 1
            elif isinstance(v, (dict, list)):
                sanitized += _sanitize_nested_png_paths(v)
    elif isinstance(obj, list):
        for i, v in enumerate(list(obj)):
            if isinstance(v, str) and v.lower().endswith(".png"):
                if not is_safe_png_path(v):
                    try:
                        obj.pop(i)
                    except Exception:
                        obj[i] = None
                    sanitized += 1
            elif isinstance(v, (dict, list)):
                sanitized += _sanitize_nested_png_paths(v)
    return sanitized


def sanitize_mission_image_refs(mission: Any) -> int:
    """Blanquea referencias a PNG inválidos en grafo compilado y ``raw_trace``.

    Devuelve el número de referencias saneadas para diagnóstico.

    Casos típicos que provocan crash si no se sanitizan:
      - ``assets/temp/...`` huérfano tras ``Failed to move snapshot``.
      - PNG escrito a 0 bytes por race entre captura y stop.
      - Borrado manual del directorio de assets.

    En cualquiera de esos casos un ``QPixmap(path)`` puede crashear o
    devolver un pixmap sentinel que rompe paint events posteriores.
    """
    sanitized = 0
    for c_step in (getattr(mission, "compiled_execution_graph", None) or []):
        try:
            tc = c_step.target_context
        except Exception:
            continue
        if tc is None:
            continue
        sanitized += _sanitize_png_string_field(tc, "image_ref")
        sanitized += _sanitize_png_string_field(tc, "image_ref_mid")
    for ev in (getattr(mission, "raw_trace", None) or []):
        try:
            sanitized += _sanitize_png_string_field(ev, "snapshot_ref")
            sanitized += _sanitize_metadata_png_paths(getattr(ev, "metadata", None))
        except Exception:
            continue
    return sanitized


__all__ = [
    "is_safe_png_path",
    "safe_png_or_none",
    "sanitize_mission_image_refs",
]
