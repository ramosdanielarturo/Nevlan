"""
Nevlan — Invalid Target Resolver
=================================

Cuando un click cae sobre un contenedor genérico (``guide-service``,
``main``, ``GroupControl`` vacío, ``click_fallback``, bbox >25% pantalla),
el sanitizer ya lo descartó. Pero a veces NO podemos descartarlo —
el usuario sí intentaba hacer algo con ese click. Este módulo intenta
**rescatar la intención** y promover el click a una acción semántica
más fuerte, en lugar de guardarlo como "click rojo" inejecutable.

Rescates implementados
----------------------

A) **Contexto YouTube** — click en algo en youtube.com:
   * Si la sesión siguiente escribe texto y hay barra de búsqueda en el
     DOM (``input[name="search_query"]``, ``#search-input input``,
     ``aria-label="Buscar"``), emitimos un step ``focus_search_box``
     anclado al locator estable, NO al bbox.
   * Si era un click en miniatura (link YouTube), emitimos ``open_url``
     con la URL del vídeo si está disponible.

B) **Chrome Profile picker** — click en la pantalla de selección de
   perfil de Chrome/Edge:
   * Convierte el evento a ``select_profile`` con ``profile`` extraído
     de OCR/texto cercano (si existe).
   * Si OCR no recuperó el nombre, marca ``needs_user_label=True`` con
     ``label_prompt="¿Qué perfil seleccionaste?"`` (lo aprovecha el
     Quick Reinforcement).

C) **Nueva pestaña** — click en el botón "+" de Chrome o Ctrl+T:
   * Convierte a ``open_new_tab`` (no depende de coords).

D) **Bookmark / barra de favoritos** — click en bookmark conocido:
   * Si el target es un bookmark con texto reconocible (YouTube,
     Gmail, etc.) y hay URL en metadata web, convierte a
     ``open_url`` con dicha URL.

E) **Scroll** — secuencia de scrolls (ya coalesced por sanitizer):
   * Convierte a ``scroll_results`` (smart-executor sabe scrollear
     hasta ver el target). NO usa coords absolutas.

API
---

>>> rescued = rescue_invalid_targets(events)  # devuelve eventos
>>> # o por step (post-compiler):
>>> rescue_compiled_step(cs)  # in-place

Invariantes
-----------

* Si no hay rescate posible, devolvemos el evento ORIGINAL marcado
  con ``rescue_failed=True``: el approval_gate lo verá y producirá
  ``invalid_click_target`` (que dispara Quick Reinforcement).

* Nunca generamos coords absolutas como rescate. Si vamos a "open_url"
  o "select_profile", debe ser por nombre/locator/intención, no
  por píxel.

* Es PURO: no toca disco, no llama al LLM. Solo heurísticas estables.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    EventType,
    RawEvent,
    TargetContext,
    ValidationStrategy,
)
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Detección de "target inválido"
# ──────────────────────────────────────────────────────────────────────

INVALID_NAMES = frozenset({
    "guide-service", "main", "click_fallback",
})

INVALID_CT = frozenset({
    "groupcontrol", "panecontrol", "customcontrol",
})


def is_invalid_target_meta(meta: Dict[str, Any]) -> bool:
    """Replica de ``raw_event_sanitizer._is_invalid_uia_target`` para
    casos en que ya tenemos un evento sin posibilidad de rescate por
    contexto inmediato (no hay web_data, no hay OCR cercano)."""
    if not meta:
        return False
    uia = meta.get("uia_data") or meta.get("desktop_uia") or {}
    if not isinstance(uia, dict):
        return False
    name = str(uia.get("name") or "").strip().lower()
    aid = str(uia.get("automation_id") or "").strip().lower()
    ct = str(uia.get("control_type") or "").strip().lower()
    if name in INVALID_NAMES or aid in INVALID_NAMES:
        return True
    if ct in INVALID_CT and not name:
        return True
    return False


def is_invalid_compiled_step(cs: CompiledStep) -> bool:
    """Determina si un ``CompiledStep`` ya compilado tiene target inválido.

    Solo aplica a acciones con target visual (clicks). Acciones
    semánticas (LAUNCH_APP, NAVIGATE_OR_SEARCH, etc.) nunca tienen
    target inválido por definición — su intención está en el payload.
    """
    if cs.action_strategy not in (
        ActionStrategy.CLICK,
        ActionStrategy.DOUBLE_CLICK,
        ActionStrategy.RIGHT_CLICK,
    ):
        return False
    tc = cs.target_context
    uia = (tc.uia_data or {}) if tc else {}
    if isinstance(uia, dict):
        name = str(uia.get("name") or "").strip().lower()
        aid = str(uia.get("automation_id") or "").strip().lower()
        ct = str(uia.get("control_type") or "").strip().lower()
        if name in INVALID_NAMES or aid in INVALID_NAMES:
            return True
        if ct in INVALID_CT and not name and not aid:
            return True
    # bbox gigante (>25% del área de la ventana → contenedor seguro).
    bbox = (uia or {}).get("bbox") if isinstance(uia, dict) else None
    win = tc.window_title or ""
    if isinstance(bbox, dict) and bbox:
        try:
            w = int(bbox.get("w", bbox.get("width", 0)))
            h = int(bbox.get("h", bbox.get("height", 0)))
            if w * h > 800 * 600:  # heurística: enorme
                return True
        except Exception:
            pass
    return False


# ──────────────────────────────────────────────────────────────────────
# Selectores DOM por sitio (rescate A)
# ──────────────────────────────────────────────────────────────────────

YOUTUBE_SEARCH_SELECTORS = [
    {"kind": "css", "value": 'input[name="search_query"]', "unique": True},
    {"kind": "css", "value": '#search-input input', "unique": True},
    {"kind": "aria_label", "value": "Buscar", "unique": False},
    {"kind": "aria_label", "value": "Search", "unique": False},
]


def youtube_search_locators() -> List[Dict[str, Any]]:
    """Lista de locators canónicos de la barra de búsqueda de YouTube,
    en orden de preferencia. Los expone también
    ``intent_compiler_v2`` cuando promueve una sesión a search_youtube
    sin necesidad de un click previo.
    """
    return [dict(x) for x in YOUTUBE_SEARCH_SELECTORS]


# ──────────────────────────────────────────────────────────────────────
# Rescates
# ──────────────────────────────────────────────────────────────────────

@dataclass
class RescueResult:
    """Resultado de un intento de rescate sobre un step inválido.

    ``new_step`` es el step semántico que reemplaza al original (None
    si no se pudo rescatar). ``reason`` documenta qué rescate aplicó
    (útil para logs y telemetría).
    """
    new_step: Optional[CompiledStep] = None
    reason: str = ""
    rescue_kind: str = ""  # youtube|chrome_profile|new_tab|bookmark|scroll
    fallback_needs_label: bool = False


def _build_step(
    *,
    action: ActionStrategy,
    payload: Dict[str, Any],
    goal: str,
    semantic: Optional[Dict[str, Any]] = None,
) -> CompiledStep:
    """Helper: construye un ``CompiledStep`` con TargetContext canónico.

    Usa ``ValidationStrategy.REQUIRE_ELEMENT_EXISTS`` por defecto:
    cualquier rescate semántico DEBE validar que el elemento llega a
    aparecer (caja de búsqueda visible, URL cargada, etc.).
    """
    tc = TargetContext(semantic=semantic or {})
    return CompiledStep(
        goal=goal,
        action_strategy=action,
        action_payload=payload,
        target_context=tc,
        validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    )


def _site_from_meta(meta: Dict[str, Any]) -> Optional[str]:
    web = meta.get("web_data") or meta.get("web") or {}
    if not isinstance(web, dict):
        return None
    url = str(web.get("url") or "").lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "google.com" in url:
        return "google"
    return None


def _process_name(meta: Dict[str, Any]) -> str:
    win = meta.get("window") or meta.get("window_context") or {}
    if isinstance(win, dict):
        return str(win.get("process_name") or "").lower()
    return ""


def _ocr_text_around(meta: Dict[str, Any]) -> str:
    vis = meta.get("vision_data") or {}
    if not isinstance(vis, dict):
        return ""
    return str(vis.get("ocr_text_around") or vis.get("nearest_text_anchor") or "").strip()


def _try_rescue_youtube(
    cs: CompiledStep, meta: Dict[str, Any]
) -> Optional[RescueResult]:
    """Rescate A: click en YouTube → focus_search_box (con DOM)."""
    if _site_from_meta(meta) != "youtube":
        return None
    near = _ocr_text_around(meta).lower()
    # Intención plausible: el bbox del click rondaba la zona del
    # buscador (parte superior, ancho ~50%) y el OCR cercano contiene
    # "Buscar" / "Search" / palabras de query.
    payload = {
        "site": "youtube",
        "target": "youtube_search_box",
        "target_label": "youtube_search_box",
        "locators": youtube_search_locators(),
    }
    return RescueResult(
        new_step=_build_step(
            action=ActionStrategy.CLICK,  # focus = click semántico
            payload=payload,
            goal="Enfocar barra de búsqueda de YouTube",
            semantic={
                "intent": "focus_search",
                "human_label": "Barra de búsqueda de YouTube",
                "site": "youtube",
                "expected_state_after": "search_input_focused",
            },
        ),
        reason=f"site=youtube near={near[:30]!r}",
        rescue_kind="youtube",
    )


def _try_rescue_chrome_profile(
    cs: CompiledStep, meta: Dict[str, Any]
) -> Optional[RescueResult]:
    """Rescate B: click en pantalla "Elige tu perfil" de Chrome/Edge."""
    proc = _process_name(meta)
    win_title = ""
    win = meta.get("window") or meta.get("window_context") or {}
    if isinstance(win, dict):
        win_title = str(win.get("title") or "").lower()
    if not (("chrome" in proc) or ("msedge" in proc) or ("brave" in proc)):
        return None
    # Heurística: en la pantalla del profile picker, el título suele
    # ser literal "¿Quién eres?" / "Who are you?" / "Choose a profile".
    is_picker = any(
        kw in win_title
        for kw in ("quién eres", "who are you", "choose a profile",
                   "elige tu perfil", "select profile")
    )
    if not is_picker:
        # Fallback: si el OCR cercano contiene un nombre humano
        # típico (mayúscula inicial, ≥2 palabras), también lo
        # tratamos como profile click.
        near = _ocr_text_around(meta)
        words = [w for w in near.split() if w[:1].isupper()]
        if len(words) < 2:
            return None
    profile = ""
    near = _ocr_text_around(meta)
    if near:
        # Tomamos las 1-3 primeras palabras con mayúscula como
        # candidato a nombre de perfil.
        cand = []
        for tok in near.split():
            if tok and tok[0].isupper():
                cand.append(tok)
            if len(cand) >= 3:
                break
        profile = " ".join(cand).strip(",.;:")

    payload: Dict[str, Any] = {
        "app": "chrome" if "chrome" in proc else "edge",
        "profile": profile,
    }
    needs_label = not profile
    if needs_label:
        payload["needs_user_label"] = True
        payload["label_prompt"] = "¿Qué perfil seleccionaste?"

    return RescueResult(
        new_step=_build_step(
            action=ActionStrategy.SELECT_PROFILE,
            payload=payload,
            goal=f"Seleccionar perfil {profile or '(?)'}",
            semantic={
                "intent": "select_profile",
                "human_label": profile or "Perfil de Chrome",
            },
        ),
        reason=f"profile_picker proc={proc} ocr={near[:40]!r}",
        rescue_kind="chrome_profile",
        fallback_needs_label=needs_label,
    )


def _try_rescue_new_tab(
    cs: CompiledStep, meta: Dict[str, Any]
) -> Optional[RescueResult]:
    """Rescate C: click en botón "+" / Ctrl+T → open_new_tab."""
    proc = _process_name(meta)
    if not any(p in proc for p in ("chrome", "msedge", "brave")):
        return None
    # Texto cercano "Nueva pestaña" / "New tab" / "+" (símbolo en bbox).
    near = _ocr_text_around(meta).lower()
    uia = meta.get("uia_data") or meta.get("desktop_uia") or {}
    name = str((uia or {}).get("name") or "").lower()
    if any(
        s in (name + " " + near)
        for s in ("nueva pestaña", "new tab", "nueva ficha")
    ) or name == "+":
        return RescueResult(
            new_step=_build_step(
                action=ActionStrategy.OPEN_NEW_TAB,
                payload={"app": "chrome" if "chrome" in proc else "edge"},
                goal="Abrir nueva pestaña",
                semantic={"intent": "open_new_tab"},
            ),
            reason=f"new_tab name={name!r}",
            rescue_kind="new_tab",
        )
    return None


def _try_rescue_bookmark(
    cs: CompiledStep, meta: Dict[str, Any]
) -> Optional[RescueResult]:
    """Rescate D: click en bookmark conocido → open_url(youtube.com,
    google.com, etc.) si la web_data trae URL."""
    proc = _process_name(meta)
    if not any(p in proc for p in ("chrome", "msedge", "brave")):
        return None
    web = meta.get("web_data") or {}
    if not isinstance(web, dict):
        return None
    url = str(web.get("url") or "").strip()
    if not url:
        return None
    # Bookmark típicamente vive bajo class_name "ToolbarBookmarks".
    uia = meta.get("uia_data") or {}
    parent_chain = (uia or {}).get("parent_chain") if isinstance(uia, dict) else None
    near_ctx = ""
    if isinstance(parent_chain, list):
        near_ctx = " | ".join([str(p.get("class_name", "")) for p in parent_chain])
    is_toolbar = "bookmark" in (near_ctx + " " + (uia.get("class_name", "") if isinstance(uia, dict) else "")).lower()
    if not is_toolbar:
        return None
    name = str((uia or {}).get("name") or "Bookmark")
    return RescueResult(
        new_step=_build_step(
            action=ActionStrategy.OPEN_BOOKMARK,
            payload={"name": name, "url": url},
            goal=f"Abrir bookmark {name}",
            semantic={"intent": "open_url", "human_label": name},
        ),
        reason=f"bookmark url={url}",
        rescue_kind="bookmark",
    )


def _try_rescue_scroll(
    cs: CompiledStep, meta: Dict[str, Any]
) -> Optional[RescueResult]:
    """Rescate E: scroll absoluto → scroll_results.

    El sanitizer ya coalesce scrolls; si llega un step de scroll a
    target inválido (raro pero posible si el compiler legacy ya lo
    promovió), reemitimos como ``SCROLL_UNTIL_VISIBLE`` sin coords.
    """
    if cs.action_strategy != ActionStrategy.SCROLL:
        return None
    payload = dict(cs.action_payload or {})
    direction = "down"
    if int(payload.get("dy", 0)) > 0:
        direction = "up"
    payload = {
        "direction": direction,
        "max_steps": int(payload.get("count", 3)),
    }
    return RescueResult(
        new_step=_build_step(
            action=ActionStrategy.SCROLL_UNTIL_VISIBLE,
            payload=payload,
            goal=f"Scroll {direction} hasta ver resultados",
            semantic={"intent": "scroll_results"},
        ),
        reason="scroll absoluto → scroll_until_visible",
        rescue_kind="scroll",
    )


def _step_metadata(cs: CompiledStep) -> Dict[str, Any]:
    """Construye un dict con la metadata útil para los rescates."""
    tc = cs.target_context
    return {
        "uia_data": tc.uia_data or {},
        "web_data": tc.web_data or {},
        "vision_data": tc.vision_data or {},
        "window": {
            "title": tc.window_title or "",
            "process_name": tc.process_name or "",
            "hwnd": tc.hwnd,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def rescue_compiled_step(cs: CompiledStep) -> Optional[RescueResult]:
    """Intenta rescatar un ``CompiledStep`` con target inválido.

    Devuelve ``None`` si el step NO necesita rescate, o si no se pudo
    rescatar (en cuyo caso el caller debe marcar el step como
    ``needs_reinforcement=True`` o eliminarlo). Devuelve un
    ``RescueResult`` con ``new_step`` poblado si el rescate funcionó.

    Orden de preferencia: chrome_profile → new_tab → bookmark →
    youtube → scroll.
    """
    if not is_invalid_compiled_step(cs):
        return None
    meta = _step_metadata(cs)
    for rescuer in (
        _try_rescue_chrome_profile,
        _try_rescue_new_tab,
        _try_rescue_bookmark,
        _try_rescue_youtube,
        _try_rescue_scroll,
    ):
        try:
            res = rescuer(cs, meta)
        except Exception as e:
            log.debug(f"rescue {rescuer.__name__}: {e}")
            res = None
        if res and res.new_step:
            log.info(
                f"[invalid_target_resolver] rescued {cs.action_strategy.value} "
                f"→ {res.new_step.action_strategy.value} "
                f"({res.rescue_kind}: {res.reason})"
            )
            return res
    return None


def rescue_steps(steps: List[CompiledStep]) -> Tuple[List[CompiledStep], int]:
    """Aplica rescate a TODOS los pasos de una lista. Steps inválidos
    SIN rescate quedan marcados con ``needs_reinforcement=True`` y el
    label_prompt apropiado para que el Quick Reinforcement los pida.
    Devuelve ``(nuevos_steps, n_rescatados)``.
    """
    out: List[CompiledStep] = []
    rescued = 0
    for cs in steps:
        if not is_invalid_compiled_step(cs):
            out.append(cs)
            continue
        result = rescue_compiled_step(cs)
        if result and result.new_step:
            out.append(result.new_step)
            rescued += 1
            continue
        # No rescatable: marcar para refuerzo humano.
        pl = dict(cs.action_payload or {})
        pl["needs_reinforcement"] = True
        pl["needs_user_label"] = True
        pl.setdefault(
            "label_prompt",
            "¿Sobre qué elemento querías hacer click? "
            "Nevlan no pudo identificarlo automáticamente.",
        )
        cs.action_payload = pl
        out.append(cs)
    return out, rescued


__all__ = [
    "RescueResult",
    "is_invalid_compiled_step",
    "is_invalid_target_meta",
    "rescue_compiled_step",
    "rescue_steps",
    "youtube_search_locators",
    "INVALID_NAMES",
    "INVALID_CT",
]
