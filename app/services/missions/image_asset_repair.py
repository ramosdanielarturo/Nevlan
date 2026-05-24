"""Copia snapshots de reparación al almacén interno de la misión y opcional match de prueba."""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.contracts.mission import CompiledStep
from app.core.config import settings
from app.core.logger import log


@dataclass
class ImageReinforceResult:
    copied_small: Optional[str] = None  # ruta absoluta
    copied_mid: Optional[str] = None
    match_score: Optional[float] = None
    region_note: str = ""
    message: str = ""
    improved_quality: bool = False


def _assets_dir(mission_id: str) -> Path:
    d = settings.VAR_PATH / "missions" / "assets" / mission_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _copy_if_exists(src: Optional[str], dest: Path) -> Optional[str]:
    if not src:
        return None
    p = Path(str(src))
    if not p.is_file():
        return None
    try:
        shutil.copy2(p, dest)
        return str(dest.resolve())
    except OSError as e:
        log.warning(f"image_asset_repair copy: {e}")
        return None


def _trial_template_match(template_path: Path) -> tuple[Optional[float], str]:
    """Match ligero opcional contra captura actual de pantalla (OpenCV si existe)."""
    try:
        import cv2  # type: ignore[import-untyped]
        import numpy as np
        from PIL import ImageGrab
    except ImportError:
        return None, "opencv/pillow no disponible para match de prueba"

    try:
        tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            return None, "plantilla ilegible"
        screen_bgr = np.array(ImageGrab.grab(all_screens=True))
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_RGB2GRAY)
        res = cv2.matchTemplate(screen_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _mn_loc, mx_loc = cv2.minMaxLoc(res)
        return float(mx), f"cv2_TM_CCOEFF_NORMED @ {mx_loc}"
    except Exception as e:
        log.debug(f"trial_template_match: {e}")
        return None, str(e)[:80]


def reinforce_step_from_capture_bundle(
    step: CompiledStep,
    mission_id: str,
    bundle: Dict[str, Any],
) -> ImageReinforceResult:
    """Promueve imágenes del bundle a assets internos y actualiza target + firma."""
    out = ImageReinforceResult()
    sid = step.id
    dest_dir = _assets_dir(mission_id)

    small_src = bundle.get("image_ref")
    mid_src = bundle.get("image_ref_mid")
    small_name = f"{sid}_anchor_small.png"
    mid_name = f"{sid}_anchor_mid.png"

    sp = dest_dir / small_name
    mp = dest_dir / mid_name

    out.copied_small = _copy_if_exists(small_src, sp)
    out.copied_mid = _copy_if_exists(mid_src, mp)

    tc = step.target_context
    cf = dict(tc.confidence or {})
    sig = dict(tc.signature) if isinstance(tc.signature, dict) else {}

    if out.copied_small:
        tc.image_ref = out.copied_small
    elif out.copied_mid:
        tc.image_ref = out.copied_mid

    if out.copied_mid:
        tc.image_ref_mid = out.copied_mid

    asset_meta = {
        "stored_at": time.time(),
        "step_id": sid,
        "paths": {"small": out.copied_small, "mid": out.copied_mid},
        "source_was_external": True,
    }
    cf["image_reinforcement"] = asset_meta
    cf["capture_reasons"] = list(
        dict.fromkeys(list(cf.get("capture_reasons") or []) + ["image_repair_internal_asset"])
    )
    sig.setdefault("confidence", {})
    if isinstance(sig.get("confidence"), dict):
        sig["confidence"]["source"] = "image_repair"
        sig["confidence"]["stored_at"] = asset_meta["stored_at"]
    try:
        from PIL import Image as PILImage

        pth = out.copied_mid or out.copied_small
        if pth and Path(pth).is_file():
            im = PILImage.open(pth)
            w, h = im.size
            asset_meta["dimensions"] = {"w": w, "h": h}
            sig["confidence"]["image_dimensions"] = {"w": w, "h": h}
    except Exception:
        pass

    tc.confidence = cf
    tc.signature = sig

    tpl_try = out.copied_mid or out.copied_small
    if tpl_try:
        sc, note = _trial_template_match(Path(tpl_try))
        out.match_score = sc
        out.region_note = note
        if sc is not None and sc >= 0.55:
            out.improved_quality = True
            out.message = (
                f"Imagen reforzada. Match de prueba ≈ {sc:.2f}. "
                f"Confianza de captura revisada en el paso."
            )
        else:
            note_l = (note or "").lower()
            if sc is None and ("opencv" in note_l or "pillow" in note_l or "import" in note_l):
                out.message = (
                    "La imagen se guardó, pero no se pudo validar automáticamente porque falta "
                    "OpenCV/Pillow. Instala dependencias para el match de prueba o usa «Probar paso»."
                )
            else:
                out.message = (
                    "La imagen se guardó en la biblioteca del flujo, pero el match de prueba "
                    "no fue claro. Puedes regrabar el paso o probar otra captura."
                )
    else:
        out.message = "No se pudieron copiar snapshots del bundle al almacén interno."

    return out

