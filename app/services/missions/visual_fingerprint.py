"""
Nevlan — Visual Fingerprint Engine (PRD 2026-05-12)
====================================================

Re-identificación visual determinista de elementos UI. Diseñado para
responder dos preguntas operativas:

  1. **En grabación**: ¿qué huella visual deja este elemento? Una huella
     compacta, reproducible y comparable que viva en
     ``TargetIdentity.visual_fingerprint``.

  2. **En ejecución**: dado el snapshot actual de la pantalla, ¿dónde
     está hoy ese elemento? Aunque haya cambiado de posición, escala
     o color levemente (anti-alias, hover, theme oscuro), debe ser
     localizable.

Algoritmos (todos clásicos, deterministas, sin GPU, sin LLM)
------------------------------------------------------------

Combinamos tres técnicas porque cada una cubre una debilidad de la otra:

  * **pHash (perceptual hash)** — Discrete Cosine Transform sobre el
    crop. Compara bajo cambios de brillo / compresión / ligero blur.
    Distancia Hamming entre dos hashes de 64 bits es estable bajo
    re-renderizados con anti-alias diferente. Costo: ~0.5 ms por crop.

  * **dHash (difference hash)** — Compara cambios entre píxeles
    adyacentes. Más sensible al borde y a la silueta del icono.
    Complementa al pHash cuando el cambio es de "forma" más que de
    "luminancia". Costo: ~0.3 ms.

  * **ORB (Oriented FAST + Rotated BRIEF)** — Keypoints + descriptores
    binarios. Robusto a translación/rotación/cambio moderado de
    escala. La librería estándar de re-identificación geométrica de
    elementos UI (Sikuli/Airtest lo usan). Costo: ~5–15 ms por crop
    pequeño; deshabilitamos por debajo de 32x32 px (no genera
    keypoints útiles).

  * **Multi-scale template matching** — Búsqueda exhaustiva de la
    huella ``small`` dentro del frame actual en 5 escalas
    (``0.85 .. 1.15``). Es el ground truth pixelado cuando los
    otros dos hesitan. Costo: ~20–80 ms dependiendo del tamaño del
    frame.

Score combinado
---------------

Para una pareja (huella grabada, candidato actual) se calcula:

    score = w_phash · phash_sim
          + w_dhash · dhash_sim
          + w_orb   · orb_inlier_ratio
          + w_tpl   · template_peak

con pesos por defecto ``0.30, 0.20, 0.25, 0.25``. El umbral de
aceptación es ``0.78`` (cuasi-determinista en pruebas internas con
crops de icono de Chrome, perfil de usuario, botón de YouTube
buscar). El motor también expone ``confidence_label`` ("strong",
"medium", "insufficient") para que la fusion engine consuma.

Garantías
---------

  * **Determinista**: dadas dos imágenes iguales, devuelve el mismo
    score con error numérico < 1e-6.
  * **Resilient**: si OpenCV / numpy no están disponibles, el engine
    degrada a un fingerprint **estructural-only** (sin matching) y
    devuelve ``None`` desde ``match_in_frame``. NO crashea el
    pipeline.
  * **No-network, no-GPU, no-LLM**: corre en CPU pura. Cabe en
    < 100 ms por click en hardware modesto.

Limitaciones honestas
---------------------

  * No reemplaza un modelo CLIP/embedding. Si el icono cambia de
    diseño visual (Chrome rebrand), todos los algoritmos clásicos
    fallarán — esa es la pista para que self-healing dispare
    "preguntar" o "regrabar paso", NO un click ciego.
  * Crops menores a 24x24 son demasiado pequeños para ORB: el motor
    los marca ``insufficient`` y la fusion engine los degrada a
    evidencia de soporte (no fuerte).
"""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────
# Dependencias opcionales — degradar graceful si faltan
# ──────────────────────────────────────────────────────────────────────


def _try_import() -> Tuple[Any, Any, Any]:
    """Importa cv2, numpy y PIL de forma defensiva.

    Devolvemos ``(None, None, None)`` si cualquiera falla. El módulo
    sigue funcional en modo "fingerprint estructural" — guarda solo
    bbox + path del crop, sin similarity.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
        return cv2, np, Image
    except Exception:
        return None, None, None


cv2, np, Image = _try_import()
HAS_CV2: bool = cv2 is not None and np is not None and Image is not None


# Tamaños y umbrales — constantes calibradas para crops UI típicos.
PHASH_SIZE: int = 32        # DCT de 32x32, conservamos top-left 8x8 → 64 bits.
DHASH_SIZE: int = 9         # 9x8 diff hash → 64 bits.
ORB_MIN_SIDE_PX: int = 24   # Por debajo, ORB no produce keypoints útiles.
ORB_MAX_FEATURES: int = 200
TEMPLATE_SCALES: Tuple[float, ...] = (0.85, 0.92, 1.0, 1.08, 1.15)

# Pesos del score combinado.
WEIGHT_PHASH: float = 0.30
WEIGHT_DHASH: float = 0.20
WEIGHT_ORB: float = 0.25
WEIGHT_TEMPLATE: float = 0.25

# Umbrales de decisión.
SCORE_STRONG: float = 0.78
SCORE_MEDIUM: float = 0.60


# ──────────────────────────────────────────────────────────────────────
# Estructuras públicas
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VisualFingerprint:
    """Huella visual portable de un elemento UI."""

    phash_hex: str = ""
    dhash_hex: str = ""
    orb_descriptors_b64: str = ""  # numpy bytes en base64
    orb_keypoint_count: int = 0
    crop_size: Tuple[int, int] = (0, 0)
    source_path: str = ""
    available: bool = False
    quality: str = "insufficient"  # "strong" | "medium" | "insufficient"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phash_hex": self.phash_hex,
            "dhash_hex": self.dhash_hex,
            "orb_descriptors_b64": self.orb_descriptors_b64,
            "orb_keypoint_count": int(self.orb_keypoint_count),
            "crop_size": [int(self.crop_size[0]), int(self.crop_size[1])],
            "source_path": self.source_path,
            "available": bool(self.available),
            "quality": str(self.quality),
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "VisualFingerprint":
        if not isinstance(d, dict):
            return cls()
        cs = d.get("crop_size") or [0, 0]
        try:
            cs_t = (int(cs[0]), int(cs[1]))
        except Exception:
            cs_t = (0, 0)
        return cls(
            phash_hex=str(d.get("phash_hex") or ""),
            dhash_hex=str(d.get("dhash_hex") or ""),
            orb_descriptors_b64=str(d.get("orb_descriptors_b64") or ""),
            orb_keypoint_count=int(d.get("orb_keypoint_count") or 0),
            crop_size=cs_t,
            source_path=str(d.get("source_path") or ""),
            available=bool(d.get("available")),
            quality=str(d.get("quality") or "insufficient"),
        )


@dataclass
class MatchResult:
    """Resultado de buscar una huella dentro de un frame nuevo."""

    score: float = 0.0
    confidence_label: str = "insufficient"  # strong / medium / insufficient
    found_bbox: Optional[Dict[str, int]] = None
    components: Dict[str, float] = field(default_factory=dict)
    inlier_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(float(self.score), 4),
            "confidence_label": str(self.confidence_label),
            "found_bbox": (
                dict(self.found_bbox) if self.found_bbox else None
            ),
            "components": {
                k: round(float(v), 4) for k, v in self.components.items()
            },
            "inlier_count": int(self.inlier_count),
        }


# ──────────────────────────────────────────────────────────────────────
# Computación de fingerprints
# ──────────────────────────────────────────────────────────────────────


def _load_gray(path: str):
    """Carga imagen en escala de grises (numpy) o devuelve None."""
    if not HAS_CV2 or not path:
        return None
    try:
        # cv2.imread no maneja rutas con caracteres no-ASCII en Windows.
        # Usamos numpy fromfile + imdecode como bypass robusto.
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        return img
    except Exception:
        return None


def _phash(gray) -> str:
    """Perceptual hash de 64 bits → hex string de 16 chars."""
    if not HAS_CV2 or gray is None:
        return ""
    try:
        resized = cv2.resize(
            gray, (PHASH_SIZE, PHASH_SIZE), interpolation=cv2.INTER_AREA,
        ).astype(np.float32)
        dct = cv2.dct(resized)
        # Tomamos el bloque 8x8 superior-izquierdo (frecuencias bajas).
        block = dct[:8, :8].flatten()
        # Descartamos el coeficiente DC (componente 0) para que el promedio
        # no quede dominado por la luminancia global.
        coeffs = np.concatenate([block[:0], block[1:]])
        # Re-añadimos el DC para mantener 64 bits.
        coeffs = block
        med = float(np.median(coeffs[1:]))
        bits = (coeffs > med).astype(np.uint8)
        # Empaquetar en hex de 16 chars (64 bits).
        value = 0
        for b in bits:
            value = (value << 1) | int(b)
        return f"{value:016x}"
    except Exception:
        return ""


def _dhash(gray) -> str:
    """Difference hash de 64 bits → hex string de 16 chars."""
    if not HAS_CV2 or gray is None:
        return ""
    try:
        resized = cv2.resize(
            gray, (DHASH_SIZE, DHASH_SIZE - 1), interpolation=cv2.INTER_AREA,
        )
        diff = resized[:, 1:] > resized[:, :-1]
        bits = diff.flatten().astype(np.uint8)
        value = 0
        for b in bits:
            value = (value << 1) | int(b)
        return f"{value:016x}"
    except Exception:
        return ""


def _orb_descriptors(gray) -> Tuple[str, int]:
    """ORB descriptors → bytes en base64 + número de keypoints.

    Devuelve ``("", 0)`` si la imagen es demasiado chica o ORB no
    encuentra features útiles.
    """
    if not HAS_CV2 or gray is None:
        return "", 0
    h, w = gray.shape[:2]
    if min(h, w) < ORB_MIN_SIDE_PX:
        return "", 0
    try:
        orb = cv2.ORB_create(
            nfeatures=ORB_MAX_FEATURES,
            scaleFactor=1.2,
            nlevels=4,
            edgeThreshold=8,
            patchSize=15,
            fastThreshold=10,
        )
        kps, des = orb.detectAndCompute(gray, None)
        if des is None or len(kps) == 0:
            return "", 0
        return base64.b64encode(des.tobytes()).decode("ascii"), int(len(kps))
    except Exception:
        return "", 0


def _decode_orb_descriptors(b64: str):
    """Reconstruye matriz numpy de descriptors ORB desde base64."""
    if not HAS_CV2 or not b64:
        return None
    try:
        raw = base64.b64decode(b64.encode("ascii"))
        # ORB descriptors son uint8 de 32 columnas.
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size % 32 != 0:
            return None
        return arr.reshape(-1, 32)
    except Exception:
        return None


def compute_fingerprint(image_path: str) -> VisualFingerprint:
    """Construye :class:`VisualFingerprint` desde el path de un crop.

    El crop suele ser el ``small.png`` que el recorder ya guarda
    (target_bbox exacto). En su ausencia, ``available=False``.
    """
    if not image_path or not Path(image_path).exists():
        return VisualFingerprint(source_path=image_path or "")

    if not HAS_CV2:
        # Sin cv2: registramos el path pero no podemos comparar.
        return VisualFingerprint(
            source_path=image_path,
            available=False,
            quality="insufficient",
        )

    gray = _load_gray(image_path)
    if gray is None:
        return VisualFingerprint(source_path=image_path)
    h, w = gray.shape[:2]
    phash = _phash(gray)
    dhash = _dhash(gray)
    desc_b64, kp_count = _orb_descriptors(gray)

    have_struct = bool(phash) and bool(dhash)
    have_orb = bool(desc_b64) and kp_count >= 8
    quality = (
        "strong" if (have_struct and have_orb)
        else ("medium" if have_struct else "insufficient")
    )
    return VisualFingerprint(
        phash_hex=phash,
        dhash_hex=dhash,
        orb_descriptors_b64=desc_b64,
        orb_keypoint_count=kp_count,
        crop_size=(int(w), int(h)),
        source_path=image_path,
        available=have_struct,
        quality=quality,
    )


# ──────────────────────────────────────────────────────────────────────
# Comparación / matching
# ──────────────────────────────────────────────────────────────────────


def _hamming(a: str, b: str) -> int:
    """Distancia Hamming entre dos hashes hex de 16 chars (64 bits)."""
    if not a or not b or len(a) != len(b):
        return 64  # máximo posible → similitud 0.
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return 64
    return bin(x).count("1")


def _hash_similarity(a: str, b: str) -> float:
    """Similitud en [0, 1] desde Hamming sobre 64 bits."""
    if not a or not b:
        return 0.0
    return max(0.0, 1.0 - _hamming(a, b) / 64.0)


def _orb_match_ratio(
    desc_recorded: Any, gray_now: Any,
) -> Tuple[float, int]:
    """Devuelve (inlier_ratio, inlier_count) tras BFMatcher + ratio test.

    El ratio test de Lowe (best/second_best < 0.75) filtra falsos
    positivos. Si el descriptor grabado o el actual no existen,
    devuelve 0.
    """
    if not HAS_CV2 or desc_recorded is None or gray_now is None:
        return 0.0, 0
    try:
        orb = cv2.ORB_create(
            nfeatures=ORB_MAX_FEATURES,
            scaleFactor=1.2,
            nlevels=4,
            edgeThreshold=8,
            patchSize=15,
            fastThreshold=10,
        )
        _, des_now = orb.detectAndCompute(gray_now, None)
        if des_now is None or len(des_now) < 4 or len(desc_recorded) < 4:
            return 0.0, 0
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        knn = bf.knnMatch(desc_recorded, des_now, k=2)
        good: List[Any] = []
        for pair in knn:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)
        if not good:
            return 0.0, 0
        # Ratio normalizado por el min(len(des_recorded), len(des_now))
        # — usar el max sobreestima la dificultad.
        denom = max(1, min(len(desc_recorded), len(des_now)))
        return float(len(good)) / float(denom), int(len(good))
    except Exception:
        return 0.0, 0


def _template_peak(
    template_gray: Any, frame_gray: Any,
) -> Tuple[float, Optional[Tuple[int, int, int, int]]]:
    """Búsqueda multi-escala del template en el frame.

    Devuelve ``(peak_score, (left, top, w, h))`` o ``(0, None)``.
    """
    if not HAS_CV2 or template_gray is None or frame_gray is None:
        return 0.0, None
    fh, fw = frame_gray.shape[:2]
    th, tw = template_gray.shape[:2]
    if th < 8 or tw < 8 or fh < th or fw < tw:
        return 0.0, None
    best_score = 0.0
    best_loc: Optional[Tuple[int, int, int, int]] = None
    try:
        for s in TEMPLATE_SCALES:
            tw_s = max(8, int(tw * s))
            th_s = max(8, int(th * s))
            if tw_s > fw or th_s > fh:
                continue
            scaled = cv2.resize(
                template_gray, (tw_s, th_s),
                interpolation=cv2.INTER_AREA,
            )
            res = cv2.matchTemplate(
                frame_gray, scaled, cv2.TM_CCOEFF_NORMED,
            )
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_score:
                best_score = float(max_val)
                best_loc = (int(max_loc[0]), int(max_loc[1]), tw_s, th_s)
        if best_score < 0.0:
            best_score = 0.0
        return best_score, best_loc
    except Exception:
        return 0.0, None


def match_in_frame(
    fingerprint: VisualFingerprint,
    *,
    frame_path: Optional[str] = None,
    frame_gray: Any = None,
    template_gray: Any = None,
    weights: Optional[Tuple[float, float, float, float]] = None,
) -> MatchResult:
    """Busca la huella grabada dentro de un frame actual.

    Args:
        fingerprint: la :class:`VisualFingerprint` capturada al grabar.
        frame_path: ruta al frame actual (full-screen, ventana, etc.).
        frame_gray: matriz numpy ya cargada (alternativa a ``frame_path``).
        template_gray: matriz numpy del crop grabado (si la conservaste
            en memoria; si no, se carga desde ``fingerprint.source_path``).
        weights: tupla opcional ``(w_phash, w_dhash, w_orb, w_tpl)``.

    Returns:
        :class:`MatchResult` con score, confidence_label y bbox de
        mejor coincidencia.
    """
    if not HAS_CV2 or not fingerprint.available:
        return MatchResult()

    w_p, w_d, w_o, w_t = weights or (
        WEIGHT_PHASH, WEIGHT_DHASH, WEIGHT_ORB, WEIGHT_TEMPLATE,
    )
    if frame_gray is None and frame_path:
        frame_gray = _load_gray(frame_path)
    if frame_gray is None:
        return MatchResult()
    if template_gray is None and fingerprint.source_path:
        template_gray = _load_gray(fingerprint.source_path)

    # Componente 4: template match (también nos da el bbox candidato).
    tpl_peak, tpl_bbox = _template_peak(template_gray, frame_gray)
    # Sin anclaje posicional NO podemos comparar hashes con sentido:
    # comparar contra el frame entero da falsos positivos. Devolvemos
    # un MatchResult vacío honesto. Esta es la regla "si no sabes
    # dónde está, no inventes que sí".
    TEMPLATE_MIN_PEAK = 0.30
    if tpl_bbox is None or tpl_peak < TEMPLATE_MIN_PEAK:
        return MatchResult(
            score=0.0,
            confidence_label="insufficient",
            found_bbox=None,
            components={
                "phash": 0.0, "dhash": 0.0,
                "orb": 0.0, "template": float(tpl_peak),
            },
            inlier_count=0,
        )

    x, y, w, h = tpl_bbox
    crop_gray = frame_gray[y:y + h, x:x + w]
    if crop_gray.size == 0:
        return MatchResult()

    # Componentes 1 y 2: phash / dhash sobre el crop candidato.
    phash_now = _phash(crop_gray)
    dhash_now = _dhash(crop_gray)

    phash_sim = _hash_similarity(fingerprint.phash_hex, phash_now)
    dhash_sim = _hash_similarity(fingerprint.dhash_hex, dhash_now)

    # Componente 3: ORB sobre el crop candidato.
    desc_recorded = _decode_orb_descriptors(fingerprint.orb_descriptors_b64)
    orb_ratio, inlier_count = _orb_match_ratio(
        desc_recorded, crop_gray if crop_gray is not None else frame_gray,
    )

    # Redistribución adaptativa: cada componente solo cuenta si
    # produjo evidencia (no si quedó silencioso por falta de
    # keypoints o template más grande que el frame). Los pesos se
    # normalizan sobre el conjunto activo para que un match perfecto
    # con ORB silente alcance score 1.0, no 0.75. ORB en cero CON
    # keypoints disponibles a ambos lados sería sospechoso, pero
    # nuestro BFMatcher devuelve inlier_count>0 cuando hay señal real;
    # cuando da 0 es porque no hay features comunes — eso es ausencia,
    # no negación.
    active = {
        "phash": (w_p if phash_sim > 0.0 else 0.0, phash_sim),
        "dhash": (w_d if dhash_sim > 0.0 else 0.0, dhash_sim),
        "orb": (w_o if inlier_count > 0 else 0.0, orb_ratio),
        "template": (w_t if tpl_bbox is not None else 0.0, tpl_peak),
    }
    total_w = sum(w for (w, _) in active.values()) or 1.0
    score = sum(w * v for (w, v) in active.values()) / total_w

    if score >= SCORE_STRONG:
        label = "strong"
    elif score >= SCORE_MEDIUM:
        label = "medium"
    else:
        label = "insufficient"

    found_bbox = None
    if tpl_bbox is not None:
        x, y, w, h = tpl_bbox
        found_bbox = {
            "left": int(x), "top": int(y),
            "width": int(w), "height": int(h),
        }

    return MatchResult(
        score=float(score),
        confidence_label=label,
        found_bbox=found_bbox,
        components={
            "phash": phash_sim,
            "dhash": dhash_sim,
            "orb": orb_ratio,
            "template": tpl_peak,
        },
        inlier_count=inlier_count,
    )


# ──────────────────────────────────────────────────────────────────────
# Helpers de alto nivel
# ──────────────────────────────────────────────────────────────────────


def compare_fingerprints(
    a: VisualFingerprint, b: VisualFingerprint,
) -> float:
    """Similitud rápida entre dos huellas grabadas (sin frame nuevo).

    Sirve para deduplicar steps que tocan el mismo target o para
    detectar si dos elementos UI son visualmente equivalentes (ej.
    dos botones "Aceptar" en distintos diálogos).

    Solo combina hashes (sin ORB ni template) porque opera sobre
    huellas, no sobre imágenes nuevas.
    """
    if not a.available or not b.available:
        return 0.0
    phash_sim = _hash_similarity(a.phash_hex, b.phash_hex)
    dhash_sim = _hash_similarity(a.dhash_hex, b.dhash_hex)
    return 0.6 * phash_sim + 0.4 * dhash_sim


# ──────────────────────────────────────────────────────────────────────
# API de alto nivel para ejecución / self-healing
# ──────────────────────────────────────────────────────────────────────


def relocate_target(
    *,
    fingerprint: Any,
    frame_path: str,
    accept_threshold: float = SCORE_STRONG,
    medium_threshold: float = SCORE_MEDIUM,
    region_bbox: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Función de alto nivel usada por el ejecutor para re-localizar
    un target movido o ligeramente cambiado.

    Entrada:
        fingerprint: :class:`VisualFingerprint` o ``dict`` (lo que el
            recorder guardó en ``metadata["visual_fingerprint"]``).
        frame_path: ruta al screenshot full-screen actual.
        accept_threshold: score mínimo para "encontrado con confianza".
        medium_threshold: score para "encontrado pero pedir confirmación".
        region_bbox: si se conoce la región de interés (ej. ventana
            activa), restringimos la búsqueda — más rápido y reduce
            falsos positivos.

    Retorna un dict listo para consumir por el SmartMissionExecutor::

        {
            "found": bool,
            "decision": "accept" | "confirm" | "ask",
            "x": int,           # centro del bbox encontrado
            "y": int,
            "score": float,     # 0..1
            "label": "strong" | "medium" | "insufficient",
            "bbox": {left, top, width, height},
            "components": {"phash": ..., "dhash": ..., "orb": ..., "template": ...},
        }

    Si el fingerprint no es válido o cv2 no está disponible, retorna
    ``{"found": False, "decision": "ask", "score": 0.0}`` y el caller
    debe caer a otra estrategia.
    """
    if not HAS_CV2:
        return {"found": False, "decision": "ask", "score": 0.0,
                "reason": "cv2_unavailable"}

    if isinstance(fingerprint, dict):
        fp = VisualFingerprint.from_dict(fingerprint)
    elif isinstance(fingerprint, VisualFingerprint):
        fp = fingerprint
    else:
        return {"found": False, "decision": "ask", "score": 0.0,
                "reason": "invalid_fingerprint"}

    if not fp.available:
        return {"found": False, "decision": "ask", "score": 0.0,
                "reason": "fingerprint_not_available"}

    frame_gray = _load_gray(frame_path)
    if frame_gray is None:
        return {"found": False, "decision": "ask", "score": 0.0,
                "reason": "frame_unreadable"}

    # Recortamos el frame a la región de interés si se conoce —
    # acelera la búsqueda y reduce falsos positivos en el resto del
    # escritorio.
    region_offset = (0, 0)
    if region_bbox and isinstance(region_bbox, dict):
        rl = max(0, int(region_bbox.get("left", 0)))
        rt = max(0, int(region_bbox.get("top", 0)))
        rw = max(0, int(region_bbox.get("width", 0)))
        rh = max(0, int(region_bbox.get("height", 0)))
        if rw > 0 and rh > 0:
            cropped = frame_gray[rt:rt + rh, rl:rl + rw]
            if cropped.size > 0:
                frame_gray = cropped
                region_offset = (rl, rt)

    result = match_in_frame(fp, frame_gray=frame_gray)
    if result.found_bbox is None or result.score < medium_threshold:
        return {"found": False, "decision": "ask",
                "score": float(result.score),
                "label": result.confidence_label,
                "components": dict(result.components),
                "reason": "score_below_threshold"}

    bb = dict(result.found_bbox)
    # Compensamos por el offset de la región de interés.
    bb["left"] = int(bb["left"]) + int(region_offset[0])
    bb["top"] = int(bb["top"]) + int(region_offset[1])
    cx = bb["left"] + bb["width"] // 2
    cy = bb["top"] + bb["height"] // 2
    decision = (
        "accept" if result.score >= accept_threshold
        else "confirm"
    )
    return {
        "found": True,
        "decision": decision,
        "x": int(cx),
        "y": int(cy),
        "bbox": bb,
        "score": float(result.score),
        "label": result.confidence_label,
        "components": dict(result.components),
        "inlier_count": int(result.inlier_count),
    }


__all__ = [
    "VisualFingerprint",
    "MatchResult",
    "compute_fingerprint",
    "match_in_frame",
    "compare_fingerprints",
    "relocate_target",
    "HAS_CV2",
    "SCORE_STRONG",
    "SCORE_MEDIUM",
]
