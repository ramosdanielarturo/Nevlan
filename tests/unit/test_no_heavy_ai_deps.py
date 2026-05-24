"""
Test guardia (PRD 2026-05-12) — el core de Nevlan NO debe importar
modelos pesados / cloud vision.

Razones:
  * Latencia: torch/transformers/CLIP cargan en segundos, no en
    milisegundos. El core debe responder en ~1-50 ms.
  * Footprint: ~2 GB en disco solo para CLIP/transformers. Inaceptable
    en una app de productividad desktop.
  * Determinismo: estos modelos son no-determinísticos entre versiones
    y pueden alucinar. El core debe ser reproducible bit-perfect.
  * Costo: cloud vision (Azure/AWS/GCP) cobra por llamada — viola
    "grabar una vez, ejecutar siempre" sin costo recurrente.

Si en el futuro queremos adoptar uno de estos, debe ser opt-in detrás
de un feature flag, no parte del core.
"""
from __future__ import annotations

from pathlib import Path

import pytest


#: Paquetes que están PROHIBIDOS de importarse desde el core de
#: Nevlan (`app/services/missions/`). Si alguno aparece en un import,
#: este test falla.
FORBIDDEN_HEAVY_IMPORTS = (
    "torch",
    "torchvision",
    "transformers",
    "open_clip",
    "open_clip_torch",
    "clip",       # OpenAI CLIP package (`pip install clip`)
    "sentence_transformers",
    "diffusers",
    "tensorflow",
    "keras",
    "onnxruntime_gpu",  # CPU ONNX está OK; GPU implica torch/cuda
    "huggingface_hub",
    # Cloud vision SDKs nuevos. NO añadirse al core.
    "boto3",  # podría estar para otra cosa; revisar caso a caso si se necesita
)

#: Cloud vision: agregar si hay sospecha. Por ahora omitido para no
#: chocar con usos ajenos al core.
FORBIDDEN_CLOUD_VISION = (
    "google.cloud.vision",
    "azure.cognitiveservices.vision",
)


#: Subárbol del repo que constituye "el core" para esta guardia.
CORE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "missions"


def _iter_python_files(root: Path):
    for p in root.rglob("*.py"):
        # Ignoramos cachés y archivos vacíos.
        if "__pycache__" in p.parts:
            continue
        yield p


@pytest.mark.parametrize("forbidden", FORBIDDEN_HEAVY_IMPORTS)
def test_core_does_not_import_heavy_ai_pkg(forbidden: str) -> None:
    """Recorre todos los .py del core de missions y falla si alguno
    importa el paquete prohibido.

    Detecta tanto ``import X`` como ``from X import ...`` (con o sin
    espacios). NO usa ast porque queremos detectar incluso imports
    dentro de funciones / try-except.
    """
    offenders = []
    for f in _iter_python_files(CORE_PATH):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Patrones de import válidos.
            patterns = (
                f"import {forbidden}",
                f"from {forbidden} ",
                f"from {forbidden}.",
                f"importlib.import_module('{forbidden}')",
                f'importlib.import_module("{forbidden}")',
            )
            if any(p in stripped for p in patterns):
                offenders.append(f"{f.relative_to(CORE_PATH.parents[2])}:{lineno}: {stripped}")
    assert offenders == [], (
        f"El core de missions importa el paquete prohibido {forbidden!r}:\n"
        + "\n".join(offenders)
        + "\n\nPRD 2026-05-12: el core no puede depender de modelos pesados "
        "tipo CLIP/transformers/torch. Si lo necesitas, escóndelo detrás de "
        "un feature flag opt-in fuera del core."
    )


def test_core_imports_only_lightweight_vision_libs() -> None:
    """Sanity check: el core USA cv2 / numpy / PIL — eso está
    explícitamente permitido por el PRD ('visual fingerprints ligeros').

    Este test es positivo: verifica que NO eliminamos por accidente
    las libs livianas mientras eliminamos las pesadas.
    """
    imported = False
    for f in _iter_python_files(CORE_PATH):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "import cv2" in text or "from cv2 import" in text or "import numpy" in text:
            imported = True
            break
    assert imported, (
        "Esperábamos que al menos un módulo del core importe cv2/numpy "
        "(visual fingerprint ligero). Si lo quitamos, perdemos el "
        "nivel 2 de la pipeline. PRD 2026-05-12."
    )
