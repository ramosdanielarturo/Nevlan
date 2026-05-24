"""
Nevlan Asset Finalizer
=======================

Antes de aprobar una misión, mueve todos los assets visuales de la
carpeta temporal del recorder (``var/missions/assets/temp/...``) a su
ubicación canónica permanente:

    var/missions/assets/{mission_id}/{step_id}_{small|mid|full}.png

…y reescribe **todas** las referencias dentro de la misión, en estos
sitios donde pueden vivir paths a PNGs:

    raw_trace[i].snapshot_ref
    raw_trace[i].metadata["snapshot_mid" | "snapshot_full" | "snapshot_small"]
    raw_trace[i].metadata["target_signature"][...] (visión)
    compiled_execution_graph[i].target_context.image_ref
    compiled_execution_graph[i].target_context.image_ref_mid

Reglas (PRD §D)
---------------

* Una misión APPROVED no puede contener ninguna ruta con ``/temp/``.
* Cada path debe apuntar a un PNG válido (existe + firma PNG).
* Si algo falla al copiar, la misión queda en ``NEEDS_REVIEW`` (la
  decisión la toma el ``approval_gate``; aquí solo reportamos).
* La operación es **idempotente**: ejecutarla dos veces no rompe nada
  (los archivos ya destinados se ignoran si están en su sitio).

Diseño
------

* Pure-Python, sin dependencias de UI; testeable headless.
* La ruta destino usa ``step_id`` REAL del ``CompiledStep``, no un hash
  efímero. Esto permite re-ejecutar el finalizer tras editar la misión
  y mantener nombres estables.
* Si un step no tiene assets (paso de teclado, hotkey, etc.) se omite
  silenciosamente — no es error.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import Mission, RawEvent, CompiledStep
from app.core.config import settings
from app.services.missions.asset_validation import is_safe_png_path


# ──────────────────────────────────────────────────────────────────────
# Tipos de dominio
# ──────────────────────────────────────────────────────────────────────

@dataclass
class FinalizeReport:
    """Resultado de finalizar los assets de una misión.

    Attributes
    ----------
    ok:
        ``True`` cuando ningún path inválido sobrevive y todas las copias
        previstas terminaron correctamente.
    copied:
        Número de archivos movidos/copiados a su destino canónico.
    rewrites:
        Número de referencias (string paths) reescritas a la ubicación
        canónica dentro de la misión.
    missing:
        Paths que la misión declaraba pero no se pudieron localizar ni
        en temp ni en destino. Cada entrada es un descriptor humano
        (``"raw_trace[2].snapshot_ref"``, etc.).
    errors:
        Cualquier fallo no recuperable (``shutil.copy`` falló, PNG
        corrupto post-copia, etc.).
    temp_remaining:
        Paths que tras finalizar SIGUEN apuntando a ``/temp/``. Si esto
        no está vacío, la misión NO puede aprobarse.
    layout_enriched:
        Número de ``CompiledStep`` que recibieron datos de layout
        normalizado (capture_screen_size, dpi_scale, window_bbox,
        target_bbox, click_offset_ratio, element_layout_in_window).
    """
    ok: bool = True
    copied: int = 0
    rewrites: int = 0
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    temp_remaining: List[str] = field(default_factory=list)
    layout_enriched: int = 0


# ──────────────────────────────────────────────────────────────────────
# Helpers de path
# ──────────────────────────────────────────────────────────────────────

# Marker específico para distinguir el bucket temporal del recorder
# (``var/missions/assets/temp/...``) de otras carpetas con la palabra
# "temp" (p.ej. ``%LOCALAPPDATA%/Temp/...`` que usa pytest). Solo
# consideramos "temporal" un path que pasa por ``/assets/temp/``.
_TEMP_MARKER = "/assets/temp/"


def _is_temp_path(p: Any) -> bool:
    if not p:
        return False
    s = str(p).replace("\\", "/").lower()
    return _TEMP_MARKER in s


def _mission_assets_dir(mission_id: str) -> Path:
    """Carpeta canónica permanente de assets de una misión."""
    return settings.VAR_PATH / "missions" / "assets" / mission_id


def _suffix_from_path(path: str) -> str:
    """Inferir el ``small|mid|full`` desde el filename.

    El recorder escribe ``step_<hex>_small.png`` / ``_mid.png`` / ``_full.png``;
    si no hay sufijo (legacy), asumimos ``small``.
    """
    fn = os.path.basename(str(path)).lower()
    for tag in ("_small.png", "_mid.png", "_full.png"):
        if fn.endswith(tag):
            return tag[1:-4]  # 'small' | 'mid' | 'full'
    return "small"


def _canonical_destination(
    mission_id: str, step_id: str, suffix: str,
) -> Path:
    return _mission_assets_dir(mission_id) / f"{step_id}_{suffix}.png"


def _try_copy(src: Path, dst: Path) -> bool:
    """Copia ``src`` → ``dst`` creando la carpeta destino. Devuelve True si
    al final ``dst`` existe (incluso si ya existía)."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.resolve() == src.resolve():
            return True
        shutil.copy2(src, dst)
        return dst.exists()
    except Exception:
        try:
            return dst.exists()
        except Exception:
            return False


# ──────────────────────────────────────────────────────────────────────
# Localizadores de paths
# ──────────────────────────────────────────────────────────────────────

# Llaves de metadata donde puede vivir un path a PNG (a nivel raíz).
_METADATA_PATH_KEYS = (
    "snapshot_small",
    "snapshot_mid",
    "snapshot_full",
)

# Promoción inmediata al grabar (antes de ``finalize_mission_assets``).
_RECORDING_SNAPSHOT_METADATA_KEYS = _METADATA_PATH_KEYS + (
    "pre_action_snapshot_full",
)

# Sub-llaves "sufijo" del recorder dentro de ``target_signature`` para
# determinar el suffix canónico (small/mid/full) cuando la key contiene
# el bucket — sin ser exhaustivos.
_SUFFIX_FROM_KEY = {
    "ref_small": "small", "ref_mid": "mid", "ref_full": "full",
    "small": "small", "mid": "mid", "full": "full",
    "small_path": "small", "mid_path": "mid", "full_path": "full",
}


def _walk_path_strings(
    obj: Any,
    *,
    prefix: List[Any] | None = None,
) -> List[Tuple[List[Any], str]]:
    """Recorre recursivamente un dict/list y devuelve (trace, value) por
    cada string que tenga pinta de path a un PNG temporal.

    Esto permite reescribir ANY referencia a ``/assets/temp/`` viva en
    ``target_signature``, sin acoplar el finalizer a una estructura
    concreta (``vision`` vs ``visual_assets`` vs ...). El recorder ha
    cambiado el shape varias veces; este walker es agnóstico.
    """
    prefix = prefix or []
    out: List[Tuple[List[Any], str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk_path_strings(v, prefix=prefix + [k]))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_path_strings(v, prefix=prefix + [i]))
    elif isinstance(obj, str):
        s = obj
        if _looks_like_png_path(s):
            out.append((prefix, s))
    return out


def _looks_like_png_path(s: str) -> bool:
    if not s or len(s) < 5:
        return False
    lo = s.lower()
    if not lo.endswith(".png"):
        return False
    return True


def _set_at(root: Any, trace: List[Any], value: str) -> bool:
    """Reescribe ``root[trace[0]][trace[1]]...`` con ``value``. Devuelve
    True si tuvo éxito."""
    if not trace:
        return False
    cursor = root
    for k in trace[:-1]:
        try:
            if isinstance(cursor, list):
                cursor = cursor[int(k)]
            else:
                cursor = cursor.get(k) if isinstance(cursor, dict) else cursor[k]
        except Exception:
            return False
        if cursor is None:
            return False
    last = trace[-1]
    try:
        if isinstance(cursor, list):
            cursor[int(last)] = value
        elif isinstance(cursor, dict):
            cursor[last] = value
        else:
            return False
        return True
    except Exception:
        return False


def _del_at(root: Any, trace: List[Any]) -> bool:
    """Borra ``root[trace[0]][trace[1]]...``."""
    if not trace:
        return False
    cursor = root
    for k in trace[:-1]:
        try:
            if isinstance(cursor, list):
                cursor = cursor[int(k)]
            else:
                cursor = cursor.get(k) if isinstance(cursor, dict) else cursor[k]
        except Exception:
            return False
        if cursor is None:
            return False
    last = trace[-1]
    try:
        if isinstance(cursor, dict):
            cursor.pop(last, None)
        elif isinstance(cursor, list):
            cursor.pop(int(last))
        return True
    except Exception:
        return False


def _suffix_from_trace(trace: List[Any], path: str) -> str:
    """Inferir suffix small/mid/full mirando primero la key del trace."""
    for k in reversed(trace):
        if isinstance(k, str):
            sk = _SUFFIX_FROM_KEY.get(k.lower())
            if sk:
                return sk
            for tag in ("small", "mid", "full"):
                if tag in k.lower():
                    return tag
    return _suffix_from_path(path)


# ──────────────────────────────────────────────────────────────────────
# Resolución del step_id para un asset
# ──────────────────────────────────────────────────────────────────────

def _build_event_to_step_id(mission: Mission) -> Dict[str, str]:
    """Mapeo ``raw_event_id → compiled_step_id`` usando ``InterpretedStep.raw_event_ids``.
    Para eventos huérfanos devolvemos el id del raw_event como fallback.
    """
    out: Dict[str, str] = {}
    interp = mission.interpreted_steps or []
    compiled = mission.compiled_execution_graph or []
    for c, i in zip(compiled, interp):
        for ev_id in (i.raw_event_ids or []):
            out[ev_id] = c.id
    return out


# ──────────────────────────────────────────────────────────────────────
# Promoción inmediata (recorder → carpeta de misión)
# ──────────────────────────────────────────────────────────────────────

def promote_recording_temp_snapshot(
    path: Any,
    mission_id: str,
) -> Optional[str]:
    """Mueve un PNG del bucket ``assets/temp/`` a ``assets/{mission_id}/``.

    Si el path ya está en la carpeta de la misión y el archivo existe, se
    devuelve tal cual. Si el archivo no existe, devuelve ``None``.
    """
    if not path or not isinstance(path, str):
        return None
    if not mission_id:
        return str(path) if is_safe_png_path(path) else None
    src = Path(path)
    if not src.exists():
        return None
    dest_dir = _mission_assets_dir(mission_id)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    dest = dest_dir / src.name
    try:
        if dest.resolve() == src.resolve():
            return str(dest) if is_safe_png_path(dest) else None
        if dest.exists() and dest.stat().st_size > 0:
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass
            return str(dest) if is_safe_png_path(dest) else None
        src.replace(dest)
        return str(dest) if is_safe_png_path(dest) else None
    except Exception:
        try:
            if _try_copy(src, dest):
                try:
                    if _is_temp_path(src):
                        src.unlink(missing_ok=True)
                except Exception:
                    pass
                return str(dest) if is_safe_png_path(dest) else None
        except Exception:
            pass
        return None


def promote_raw_event_snapshots(ev: RawEvent, mission_id: str) -> int:
    """Promueve ``snapshot_ref`` y metadata PNG de temp → misión.

    Devuelve el número de referencias reescritas. Paths que no existen
    o no son PNG válidos se blanquean para no dejar rutas falsas.
    """
    if not ev or not mission_id:
        return 0
    rewrites = 0
    promoted: Dict[str, str] = {}

    def _resolve_path(cur: str) -> Optional[str]:
        norm = str(cur).replace("\\", "/")
        if norm in promoted:
            return promoted[norm]
        if _is_temp_path(cur):
            new = promote_recording_temp_snapshot(cur, mission_id)
        else:
            new = cur if is_safe_png_path(cur) else None
        if not new and not Path(cur).exists():
            fallback = _mission_assets_dir(mission_id) / Path(cur).name
            if is_safe_png_path(fallback):
                new = str(fallback)
        if new and is_safe_png_path(new):
            promoted[norm] = new
            return new
        return None

    def _apply(field: str, holder: Any, *, is_metadata_key: bool = False) -> None:
        nonlocal rewrites
        cur = holder.get(field) if is_metadata_key else getattr(holder, field, None)
        if not cur:
            return
        prev = cur
        cur = _resolve_path(cur)
        if cur != prev:
            rewrites += 1
        if is_metadata_key:
            if cur:
                holder[field] = cur
            else:
                holder.pop(field, None)
        else:
            setattr(holder, field, cur)

    _apply("snapshot_ref", ev)

    md = ev.metadata
    if not isinstance(md, dict):
        return rewrites
    for key in _RECORDING_SNAPSHOT_METADATA_KEYS:
        if key in md:
            _apply(key, md, is_metadata_key=True)

    ts = md.get("target_signature")
    if isinstance(ts, dict):
        for trace, val in _walk_path_strings(ts):
            if not _looks_like_png_path(val):
                continue
            if not _is_temp_path(val) and is_safe_png_path(val):
                continue
            new = _resolve_path(val)
            if new:
                if new != val:
                    rewrites += 1
                _set_at(ts, trace, new)
            elif _is_temp_path(val) or not is_safe_png_path(val):
                _del_at(ts, trace)
                rewrites += 1
        md["target_signature"] = ts
        ev.metadata = md

    return rewrites


# ──────────────────────────────────────────────────────────────────────
# Finalize
# ──────────────────────────────────────────────────────────────────────

def finalize_mission_assets(
    mission: Mission,
    *,
    target_dir: Optional[Path] = None,
) -> FinalizeReport:
    """Migra todos los assets temp → permanente y reescribe referencias.

    Args
    ----
    mission: la misión sobre la que operamos *in-place*.
    target_dir: opcional; carpeta destino. Por defecto se usa
        ``var/missions/assets/{mission.id}/``.

    Returns
    -------
    FinalizeReport con el balance de la operación.
    """
    report = FinalizeReport()
    if not mission or not mission.id:
        report.ok = False
        report.errors.append("mission_or_id_missing")
        return report

    dest_dir = target_dir or _mission_assets_dir(mission.id)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        report.ok = False
        report.errors.append(f"cannot_create_dest_dir: {e}")
        return report

    ev_to_step = _build_event_to_step_id(mission)
    # Cache de copias ya hechas: {src_path_normalized → dst_path}.
    moved: Dict[str, str] = {}

    def _resolve(src_path: str, *, step_id: str, suffix_hint: str = "") -> Optional[str]:
        """Copia (o reusa) y devuelve el destino canónico para ``src_path``.

        Si ``src_path`` ya está en ubicación canónica y el archivo existe,
        lo dejamos en su sitio. Si está en temp y el archivo existe, lo
        copiamos a ``{dest_dir}/{step_id}_{suffix}.png`` y devolvemos esa
        ruta. Si el archivo no existe, devolvemos ``None`` y registramos
        en ``missing``.
        """
        if not src_path:
            return None
        norm = str(src_path).replace("\\", "/")
        if norm in moved:
            return moved[norm]

        suffix = suffix_hint or _suffix_from_path(src_path)
        src = Path(src_path)

        # Caso 1: ya está en destino canónico. Confirmar archivo y reusar.
        try:
            in_canonical = (
                src.parent.resolve() == dest_dir.resolve()
            )
        except Exception:
            in_canonical = False
        if in_canonical and src.exists():
            moved[norm] = str(src)
            return str(src)

        # Caso 2: está en temp. Copiar a canónico.
        if not src.exists():
            return None

        dst = _canonical_destination(mission.id, step_id, suffix)
        # Si ya existe el dst (rerun), no copiar de nuevo a menos que el
        # tamaño difiera — preferimos lo viejo estable.
        if dst.exists() and dst.stat().st_size > 0:
            moved[norm] = str(dst)
            return str(dst)
        if _try_copy(src, dst):
            moved[norm] = str(dst)
            report.copied += 1
            # Si es un archivo en temp, intentar borrarlo (best-effort).
            if _is_temp_path(src):
                try:
                    src.unlink()
                except Exception:
                    pass
            return str(dst)
        report.errors.append(f"copy_failed: {src} → {dst}")
        return None

    def _process_walk(
        root: Any, *, step_id: str, descriptor_prefix: str,
    ) -> None:
        """Reescribe recursivamente cualquier path .png dentro de ``root``."""
        for trace, val in _walk_path_strings(root):
            suffix = _suffix_from_trace(trace, val)
            new_path = _resolve(val, step_id=step_id, suffix_hint=suffix)
            descriptor = (
                f"{descriptor_prefix}." + ".".join(map(str, trace))
            )
            if new_path:
                if new_path != val:
                    report.rewrites += 1
                if not _set_at(root, trace, new_path):
                    report.errors.append(f"rewrite_failed:{descriptor}")
            else:
                report.missing.append(descriptor)
                _del_at(root, trace)

    # ── 1. Procesar raw_trace ───────────────────────────────────────
    for idx, ev in enumerate(mission.raw_trace or []):
        # step_id: si el raw_event está mapeado a un compiled step, usamos ese.
        # Si no, fallback al id del propio evento (paths estables aunque no
        # esté en el grafo compilado).
        step_id = ev_to_step.get(ev.id, ev.id)

        # 1a. snapshot_ref (campo de primer nivel del RawEvent).
        if ev.snapshot_ref:
            new_path = _resolve(
                ev.snapshot_ref, step_id=step_id, suffix_hint="small",
            )
            if new_path:
                if new_path != ev.snapshot_ref:
                    report.rewrites += 1
                ev.snapshot_ref = new_path
            else:
                report.missing.append(f"raw_trace[{idx}].snapshot_ref")
                ev.snapshot_ref = None

        # 1b. CUALQUIER path PNG dentro de metadata (snapshot_*,
        #     target_signature.vision.*, target_signature.visual_assets.*,
        #     y futuros sin tener que mantener la lista).
        md = ev.metadata or {}
        _process_walk(
            md, step_id=step_id,
            descriptor_prefix=f"raw_trace[{idx}].metadata",
        )
        ev.metadata = md

    # ── 2. Procesar compiled_execution_graph ───────────────────────
    for idx, cs in enumerate(mission.compiled_execution_graph or []):
        tc = cs.target_context
        if tc is None:
            continue
        step_id = cs.id

        if tc.image_ref:
            new_path = _resolve(
                tc.image_ref, step_id=step_id, suffix_hint="small",
            )
            if new_path:
                if new_path != tc.image_ref:
                    report.rewrites += 1
                tc.image_ref = new_path
            else:
                report.missing.append(
                    f"compiled[{idx}].target_context.image_ref"
                )
                tc.image_ref = None

        if tc.image_ref_mid:
            new_path = _resolve(
                tc.image_ref_mid, step_id=step_id, suffix_hint="mid",
            )
            if new_path:
                if new_path != tc.image_ref_mid:
                    report.rewrites += 1
                tc.image_ref_mid = new_path
            else:
                report.missing.append(
                    f"compiled[{idx}].target_context.image_ref_mid"
                )
                tc.image_ref_mid = None

        # Walker recursivo sobre target_context.signature (vive como
        # dict serializable; tiene visual_assets, vision, environment...).
        if tc.signature is not None and isinstance(tc.signature, dict):
            _process_walk(
                tc.signature, step_id=step_id,
                descriptor_prefix=f"compiled[{idx}].target_context.signature",
            )

    # ── 3. Enriquecer cada step con datos de layout normalizado ─────
    #     (capture_screen_size, dpi_scale, window_bbox, target_bbox,
    #      click_xy, click_offset_ratio, element_layout_in_window) para
    #     que el player pueda resolver targets de forma resistente al
    #     cambio de resolución/DPI. PRD 2026-05-03c §1.
    try:
        report.layout_enriched = enrich_resolution_independent_layout(mission)
    except Exception as e:
        report.errors.append(f"layout_enrichment_failed: {e}")
        report.layout_enriched = 0

    # ── 4. Auditoría final: ¿queda algún /temp/? ────────────────────
    report.temp_remaining = _find_temp_paths(mission)
    if report.temp_remaining:
        report.ok = False
    if report.errors:
        report.ok = False

    # ── 5. Validación de cada path final ────────────────────────────
    for kind, path in _iter_all_asset_paths(mission):
        if path and not is_safe_png_path(path):
            report.ok = False
            report.errors.append(f"invalid_png:{kind}:{path}")

    return report


# ──────────────────────────────────────────────────────────────────────
# Inspección read-only
# ──────────────────────────────────────────────────────────────────────

def _iter_all_asset_paths(mission: Mission):
    for idx, ev in enumerate(mission.raw_trace or []):
        if ev.snapshot_ref:
            yield (f"raw_trace[{idx}].snapshot_ref", ev.snapshot_ref)
        md = ev.metadata or {}
        if isinstance(md, dict):
            for trace, val in _walk_path_strings(md):
                yield (
                    f"raw_trace[{idx}].metadata."
                    f"{'.'.join(map(str, trace))}",
                    val,
                )
    for idx, cs in enumerate(mission.compiled_execution_graph or []):
        tc = cs.target_context
        if tc is None:
            continue
        if tc.image_ref:
            yield (f"compiled[{idx}].target_context.image_ref", tc.image_ref)
        if tc.image_ref_mid:
            yield (
                f"compiled[{idx}].target_context.image_ref_mid",
                tc.image_ref_mid,
            )
        if isinstance(tc.signature, dict):
            for trace, val in _walk_path_strings(tc.signature):
                yield (
                    f"compiled[{idx}].target_context.signature."
                    f"{'.'.join(map(str, trace))}",
                    val,
                )

    # PRD 2026-05-07 §3: el plan semántico también puede arrastrar
    # paths /temp/ heredados (locators con image_path, contexto con
    # screenshot_ref…). Auditarlo en bloque para que ApprovalGate los
    # detecte aunque el legacy graph haya sido limpiado.
    sep = mission.semantic_execution_plan
    if isinstance(sep, dict):
        steps = sep.get("steps") or []
        for sidx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            params = step.get("params") or {}
            if isinstance(params, dict):
                for trace, val in _walk_path_strings(params):
                    yield (
                        f"semantic_execution_plan.steps[{sidx}].params."
                        f"{'.'.join(map(str, trace))}",
                        val,
                    )


def _find_temp_paths(mission: Mission) -> List[str]:
    """Lista descriptores legibles de cualquier ruta /temp/ que sobreviva."""
    return [k for k, v in _iter_all_asset_paths(mission) if _is_temp_path(v)]


def has_temp_paths(mission: Mission) -> bool:
    """Conveniencia para el ApprovalGate."""
    return bool(_find_temp_paths(mission))


# ──────────────────────────────────────────────────────────────────────
# Layout normalizado (PRD 2026-05-03c §1)
# ──────────────────────────────────────────────────────────────────────

def _normalize_bbox(b: Any) -> Optional[Dict[str, int]]:
    """Devuelve un dict con left/top/width/height enteros si ``b`` es un
    bbox válido. ``None`` en cualquier otro caso."""
    if not isinstance(b, dict):
        return None
    try:
        return {
            "left": int(b.get("left", b.get("x", 0))),
            "top": int(b.get("top", b.get("y", 0))),
            "width": int(b.get("width", b.get("w", 0))),
            "height": int(b.get("height", b.get("h", 0))),
        }
    except Exception:
        return None


def _ratio_inside(inner: Dict[str, int], outer: Dict[str, int]) -> Dict[str, float]:
    """Posición/tamaño del bbox ``inner`` relativo al bbox ``outer`` en
    rango [0..1]. Útil para que el player escale targets cuando cambia
    la resolución de pantalla.

    Devuelve un dict con ``left_ratio``, ``top_ratio``, ``width_ratio``,
    ``height_ratio``. Recorta a [0, 1] para evitar valores explosivos
    cuando el bbox del recorder se sale del frame.
    """
    ow = max(1, int(outer.get("width", 0)))
    oh = max(1, int(outer.get("height", 0)))

    def _clamp(x: float) -> float:
        return max(0.0, min(1.0, x))

    return {
        "left_ratio": _clamp((inner["left"] - outer["left"]) / ow),
        "top_ratio": _clamp((inner["top"] - outer["top"]) / oh),
        "width_ratio": _clamp(inner["width"] / ow),
        "height_ratio": _clamp(inner["height"] / oh),
    }


def _click_offset_ratio(click_xy: Optional[List[Any]],
                        target_bbox: Optional[Dict[str, int]],
                        ) -> Optional[Dict[str, float]]:
    """Posición del click DENTRO del target_bbox como ratio."""
    if not click_xy or not target_bbox:
        return None
    try:
        cx = float(click_xy[0])
        cy = float(click_xy[1])
    except Exception:
        return None
    bw = max(1, int(target_bbox.get("width", 0)))
    bh = max(1, int(target_bbox.get("height", 0)))
    return {
        "x_ratio": max(0.0, min(1.0, (cx - target_bbox["left"]) / bw)),
        "y_ratio": max(0.0, min(1.0, (cy - target_bbox["top"]) / bh)),
    }


def enrich_resolution_independent_layout(mission: Mission) -> int:
    """Calcula y persiste, para cada ``CompiledStep``, los descriptores
    necesarios para que el player resuelva targets de forma resistente
    al cambio de resolución/DPI:

      * ``capture_screen_size`` — tamaño de la pantalla cuando se grabó.
      * ``dpi_scale`` — DPI scale activo (1.0, 1.25, 1.5, …).
      * ``window_bbox`` — bbox de la ventana al momento del click.
      * ``target_bbox`` — bbox del elemento clicado/UIA.
      * ``click_xy`` — coordenada absoluta del click.
      * ``click_offset_ratio`` — posición relativa dentro del target.
      * ``element_layout_in_window`` — left/top/width/height_ratio del
        target dentro de la ventana.

    Todos viven dentro de ``target_context.signature["layout"]``. El
    player tiene prioridad UIA/DOM > OCR > vision normalized > relative
    > absolute, así que ``click_xy`` queda como último recurso de
    emergencia, mientras que los ratios son el "scale-aware" fallback.

    Devuelve el número de steps que recibieron datos nuevos.
    """
    enriched = 0
    if not mission or not mission.compiled_execution_graph:
        return 0

    # Indexamos raw events por id para no recorrer todo cada vez.
    raw_by_id = {ev.id: ev for ev in (mission.raw_trace or [])}

    for cs in mission.compiled_execution_graph:
        tc = cs.target_context
        if tc is None:
            continue

        # Buscamos el raw event "ancla" del step. ``raw_event_ids``
        # vive en InterpretedStep, y CompiledStep puede tenerlo en
        # ``meta`` o en su propio campo ``source_raw_event_ids``.
        raw_ev: Optional[RawEvent] = None
        candidate_ids: List[str] = []
        for attr in ("source_raw_event_ids", "raw_event_ids"):
            v = getattr(cs, attr, None)
            if isinstance(v, (list, tuple)):
                candidate_ids.extend([str(x) for x in v])
        meta = getattr(cs, "meta", None)
        if isinstance(meta, dict):
            v = meta.get("raw_event_ids")
            if isinstance(v, (list, tuple)):
                candidate_ids.extend([str(x) for x in v])
        for rid in candidate_ids:
            if rid in raw_by_id:
                raw_ev = raw_by_id[rid]
                break
        if raw_ev is None:
            # Mejor esfuerzo: el InterpretedStep que ``compiled_step``
            # mapea — si la misión no expone un puente, probamos por
            # ``id`` directo (algunos legacy usan el mismo id).
            raw_ev = raw_by_id.get(cs.id)

        md = (raw_ev.metadata if raw_ev else {}) or {}

        # Reunimos las pistas. Prioridad: metadata explícita del recorder
        # > uia_data del compiled step > nada.
        target_bbox = (
            _normalize_bbox(md.get("target_bbox"))
            or _normalize_bbox(md.get("anchor_bbox"))
            or _normalize_bbox(tc.anchor_bbox)
            or _normalize_bbox((tc.uia_data or {}).get("bbox"))
        )
        window_bbox = (
            _normalize_bbox(md.get("window_bbox"))
            or _normalize_bbox(((tc.uia_data or {}).get("window") or {}).get("bbox"))
        )
        screen = md.get("screen_size") or md.get("capture_screen_size") or {}
        if not screen:
            sig = tc.signature if isinstance(tc.signature, dict) else {}
            env = sig.get("environment") if isinstance(sig, dict) else {}
            screen = (env or {}).get("primary_screen_px") or {}
        screen_size = None
        if isinstance(screen, dict) and screen.get("w") and screen.get("h"):
            screen_size = {"w": int(screen["w"]), "h": int(screen["h"])}
        dpi = md.get("dpi_scale") or md.get("dpi")
        if dpi is None and isinstance(tc.signature, dict):
            env = tc.signature.get("environment") or {}
            dpi = env.get("dpi_scale") if isinstance(env, dict) else None
        click_xy = md.get("click_xy")

        layout: Dict[str, Any] = {}
        if screen_size:
            layout["capture_screen_size"] = screen_size
        if dpi is not None:
            try:
                layout["dpi_scale"] = float(dpi)
            except Exception:
                pass
        if window_bbox:
            layout["window_bbox"] = window_bbox
        if target_bbox:
            layout["target_bbox"] = target_bbox
        if click_xy:
            try:
                layout["click_xy"] = [int(click_xy[0]), int(click_xy[1])]
            except Exception:
                pass
        if target_bbox and click_xy:
            r = _click_offset_ratio(click_xy, target_bbox)
            if r:
                layout["click_offset_ratio"] = r
        if target_bbox and window_bbox:
            layout["element_layout_in_window"] = _ratio_inside(
                target_bbox, window_bbox,
            )

        # Estrategia de resolución (orden de prioridad para el player).
        # No copiamos la lista entera; sólo dejamos un descriptor para
        # que el player lo pueda introspeccionar y que las pruebas vean
        # que existe.
        layout.setdefault(
            "resolution_strategy",
            ["uia_dom", "ocr_text", "vision_normalized",
             "relative_coords", "absolute_emergency"],
        )

        if not layout:
            continue
        if not isinstance(tc.signature, dict):
            tc.signature = {}
        tc.signature["layout"] = layout
        enriched += 1

    return enriched


__all__ = [
    "FinalizeReport",
    "finalize_mission_assets",
    "has_temp_paths",
    "enrich_resolution_independent_layout",
    "promote_recording_temp_snapshot",
    "promote_raw_event_snapshots",
]
