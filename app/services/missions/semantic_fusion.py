"""
Nevlan Semantic Fusion Pass
============================

Toma el ``compiled_execution_graph`` que produce ``MissionCompiler`` y aplica
fusiones de alto nivel orientadas a INTENCIÓN HUMANA — no a evento técnico.
Esto cierra los huecos que el compilador clásico deja:

  * **Invalid Target Filter** — los CLICKs sobre contenedores genéricos
    (``GroupControl`` vacío, ``guide-service``, ``ytd-app``, ...) o que la
    fase visual ya marcó como ``click_fallback`` se descartan o se rescatan
    fundiéndolos con el siguiente paso (por ejemplo, click en searchbox de
    YouTube + escritura → un único ``SUBMIT_SEARCH``).

  * **Chrome Workflow** — Win Search → Chrome (incluyendo el patrón
    ``CLICK_search → CLICK_result + TYPE`` que el compiler clásico dejaba
    suelto), profile picker (``SELECT_PROFILE``), nueva pestaña
    (``OPEN_NEW_TAB``), bookmark (``OPEN_BOOKMARK`` con URL conocido).

  * **Field Session Merger v2** — agrupa fragmentos de tecleo que el
    listener partió por un ``Enter`` accidental o por un re-foco interno
    de la app web. El último texto gana, el ``Enter`` se absorbe como
    ``submit_after=True``.

Reglas de filtrado del Invalid Target Filter (PRD §B)
-----------------------------------------------------

Un step es ``invalid target`` cuando, para una acción de tipo CLICK/-CLICK
variantes, su ``target_context`` cumple cualquiera de:

  * ``UIA name == ""`` y ``UIA AutomationId == ""`` y ``ControlType ∈ {Group, Pane, Custom, Document, Window, Toolbar, StatusBar}``.
  * ``UIA name`` o ``automation_id`` ∈ blacklist técnica (``guide-service``,
    ``ytd-app``, ``ytd-search``, ``ytd-*``, ``video-stream``, ``app-root``,
    ``app-shell``, ``react-root``, ``container``, ``wrapper``, ...).
  * ``area_ratio > 0.25`` respecto a la ventana, y la acción NO está
    explícitamente marcada como sobre lienzo/canvas.
  * ``target_source == "click_fallback"`` y no hay etiqueta humana en
    ningún descriptor (UIA/web/payload).

Cuando el step está marcado como inválido, el filtro:
  1. Si el SIGUIENTE step es ``TYPE_TEXT`` / ``SET_FIELD_VALUE`` con
     ``field_session=True`` Y pertenece al mismo proceso → fusiona el
     CLICK como anchor del campo (rescate).
  2. Si NO hay rescate posible → descarta el step.

Field Session Merger v2 (PRD §C)
--------------------------------

Bridge sobre ``Enter`` cuando:
  * El step actual es ``TYPE_TEXT`` / ``SET_FIELD_VALUE`` con
    ``field_session=True``.
  * El siguiente es ``SEND_HOTKEY`` enter sin modificadores.
  * El siguiente es otro ``TYPE_TEXT`` / ``SET_FIELD_VALUE`` del MISMO
    proceso, donde el primer texto es **prefijo case-insensitive** del
    segundo, o el segundo lo subsume completamente.

→ El paso conservado es el SEGUNDO (texto más completo), con
``submit_after=True``. El ``Enter`` y el primer fragmento desaparecen.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    ValidationStrategy,
)


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

# Frames técnicos que NUNCA califican como target principal. Mezcla
# tokens slug (sub-string) y landmarks HTML/ARIA (match exacto).
TECHNICAL_NAME_PATTERNS = (
    "guide-service", "ytd-app", "ytd-search", "ytd-",
    "video-stream", "app-root", "app-shell", "react-root",
    "container", "wrapper", "outer", "inner",
)

# Landmarks HTML/ARIA exactos que el DOM o el AccessKit exponen como
# automation_id pero NO son targets accionables: son contenedores
# tan grandes que cualquier coordenada cae dentro. Si UIA reporta uno
# de estos como name o automation_id principal, lo tratamos como
# inválido sí o sí.
LANDMARK_TECHNICAL_NAMES = {
    "main", "body", "html", "root", "app", "document", "page",
    "primary", "content", "viewport", "navigation", "complementary",
    "banner", "contentinfo", "form", "search",
    # Comunes de SPAs grandes
    "react-root", "next-root", "vue-root", "ng-app",
}

GENERIC_UIA_TYPES = {
    "groupcontrol", "panecontrol", "customcontrol",
    "documentcontrol", "windowcontrol", "toolbarcontrol",
    "statusbarcontrol",
}

# Procesos de Windows Search — usados para reconocer el "click on result".
WINDOWS_SEARCH_PROCESSES = {
    "searchhost.exe", "searchui.exe", "searchapp.exe",
    "startmenuexperiencehost.exe", "cortana.exe",
}

# Frases EXACTAS de la caja de Win Search (i18n).
WINDOWS_SEARCH_BOX_NAMES = {
    "escribe aquí para buscar", "escribe aqui para buscar",
    "escriba aquí para buscar", "escriba aqui para buscar",
    "type here to search", "search windows",
    "hier eingeben zum suchen", "tapez ici pour rechercher",
    "digite aqui para pesquisar", "cortana",
    # Nevlan vio "Escribe aquí para buscar." (con punto) en explorer.exe.
    "escribe aquí para buscar.", "escribe aqui para buscar.",
}

# Mapeo de bookmarks comunes a URLs canónicas (heurística: el name del
# bookmark es el dominio o un alias humano del sitio).
BOOKMARK_NAME_TO_URL = {
    "youtube": "https://www.youtube.com/",
    "gmail": "https://mail.google.com/",
    "google": "https://www.google.com/",
    "github": "https://github.com/",
    "facebook": "https://www.facebook.com/",
    "twitter": "https://twitter.com/",
    "x": "https://x.com/",
    "linkedin": "https://www.linkedin.com/",
    "stack overflow": "https://stackoverflow.com/",
    "stackoverflow": "https://stackoverflow.com/",
    "reddit": "https://www.reddit.com/",
}

# UIA name patrones para "Nueva pestaña" / "New Tab" en distintos idiomas.
NEW_TAB_NAMES = {
    "nueva pestaña", "nueva pestana", "new tab",
    "neuer tab", "nouvel onglet", "nova guia", "nova aba",
}


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _process(cs: CompiledStep) -> str:
    tc = cs.target_context
    return ((tc.process_name or "") if tc else "").strip().lower()


def _uia(cs: CompiledStep) -> Dict[str, Any]:
    tc = cs.target_context
    return (tc.uia_data or {}) if tc else {}


def _web(cs: CompiledStep) -> Dict[str, Any]:
    tc = cs.target_context
    return (tc.web_data or {}) if tc else {}


def _has_human_label(cs: CompiledStep) -> bool:
    """¿El step tiene una etiqueta humana real (no técnica) en algún descriptor?"""
    uia = _uia(cs)
    web = _web(cs)
    pl = cs.action_payload or {}

    candidates: List[str] = []
    for k in ("name", "automation_id"):
        v = (uia.get(k) or "").strip()
        if v:
            candidates.append(v)
    for k in ("accessible_name", "label", "placeholder", "name_attr", "test_id"):
        v = (web.get(k) or "").strip()
        if v:
            candidates.append(v)
    for k in ("label", "field_label", "menu_item"):
        v = str(pl.get(k) or "").strip()
        if v:
            candidates.append(v)

    for c in candidates:
        if not _is_technical_name(c):
            return True
    return False


def _is_technical_name(s: str) -> bool:
    if not s:
        return True
    lo = s.strip().lower()
    # Landmark HTML/ARIA exacto.
    if lo in LANDMARK_TECHNICAL_NAMES:
        return True
    for pat in TECHNICAL_NAME_PATTERNS:
        if pat in lo:
            return True
    # slug autogenerado: sin espacios, con guiones, ≥10 chars y sin punto
    if " " not in lo and "-" in lo and len(lo) >= 10 and "." not in lo:
        return True
    return False


def _bbox_is_pathological(b: Optional[Dict[str, Any]]) -> bool:
    """¿El bbox tiene coordenadas/dimensiones imposibles?

    Casos típicos que vemos en Chrome cuando UIA inspecciona un
    contenedor con virtual-scroll:

      * ``top`` o ``left`` muy negativos (e.g. ``-113758``).
      * ``height``/``width`` mayor que cualquier monitor real (>10 000 px).

    Si el bbox cae aquí, NO podemos confiar en él como target.
    """
    if not b:
        return False
    try:
        left = int(b.get("left") or 0)
        top = int(b.get("top") or 0)
        w = int(b.get("width") or 0)
        h = int(b.get("height") or 0)
    except Exception:
        return False
    if w <= 0 or h <= 0:
        return True
    # Negativo MÁS allá del límite razonable de un monitor secundario
    # (pantallas a la izquierda o arriba pueden tener coords negativas
    # legítimas hasta -8192 en Windows; más allá es virtual-scroll).
    if top < -8192 or left < -8192:
        return True
    if w > 12000 or h > 12000:
        return True
    return False


def _step_target_source(cs: CompiledStep) -> str:
    """Devuelve ``target_source`` del paso (uia/dom/ocr/vision/click_fallback).

    Lo extrae del bloque ``signature`` del target_context, que es donde
    ``visual_capture`` lo persiste.
    """
    if not cs.target_context:
        return ""
    sig = cs.target_context.signature or {}
    if isinstance(sig, dict):
        v = sig.get("target_source") or sig.get("source") or ""
        return str(v).lower()
    return ""


def _trusted_pre_action_signature(cs: CompiledStep) -> bool:
    """Identidad PRE-click fuerte preservada tras transición de estado."""
    if not cs.target_context:
        return False
    sig = cs.target_context.signature or {}
    if not isinstance(sig, dict):
        return False
    return (
        sig.get("trusted_pre_action_identity") is True
        and sig.get("identity_preserved_after_transition") is True
    )


def _step_quality(cs: CompiledStep) -> str:
    """``quality_level`` (high/medium/low/red) del visual_capture, o ''."""
    if not cs.target_context:
        return ""
    conf = cs.target_context.confidence or {}
    if isinstance(conf, dict):
        return str(conf.get("quality_level") or "").lower()
    return ""


def _is_red_or_fallback_click(cs: CompiledStep) -> bool:
    """¿El click viene de click_fallback o quality_level rojo/bajo?

    Estos NUNCA deben quedar como ``CLICK`` ejecutable: el player no
    sabe a qué reproducir. Hay que convertirlos a un step semántico
    "necesita refuerzo humano".
    """
    if not _is_click_action(cs):
        return False
    if _trusted_pre_action_signature(cs):
        return False
    src = _step_target_source(cs)
    if src == "click_fallback":
        return True
    ql = _step_quality(cs)
    if ql in {"red", "low"}:
        return True
    # También capturamos el caso "no hay anchor_bbox y el uia.bbox es
    # patológico": no hay manera de reproducir.
    tc = cs.target_context
    if tc and not tc.anchor_bbox:
        uia = _uia(cs)
        bb = (uia.get("bbox") or {}) if isinstance(uia.get("bbox"), dict) else {}
        if _bbox_is_pathological(bb):
            return True
    return False


# Controles típicamente estables cuando el usuario clica un control real de
# shell/Desktop — aunque el pipeline visual marcó ``click_fallback`` porque
# el bbox no contenía exactamente el clic.
_STABLE_FALLBACK_PROMOTION_CT: frozenset = frozenset({
    "buttoncontrol",
    "splitbuttoncontrol",
    "editcontrol",
    "comboboxcontrol",
    "checkboxcontrol",
    "radiobuttoncontrol",
    "hyperlinkcontrol",
    "listitemcontrol",
    "menuitemcontrol",
    "tabitemcontrol",
    "treeitemcontrol",
    "thumbcontrol",
    "toolbarbuttoncontrol",
})


def _windows_search_anchor_name(name: str) -> bool:
    n = name.strip().lower().rstrip(".")
    if not n:
        return False
    for phrase in WINDOWS_SEARCH_BOX_NAMES:
        p = phrase.strip().lower().rstrip(".")
        if not p:
            continue
        if n == p or n.startswith(p) or p in n:
            return True
    return False


def _fallback_promotion_blocked_by_geometry(cs: CompiledStep) -> bool:
    tc = cs.target_context
    if _bbox_is_pathological(tc.anchor_bbox if tc else None):  # type: ignore[union-attr]
        return True
    uia = _uia(cs)
    ubb = (uia.get("bbox") or {}) if isinstance(uia.get("bbox"), dict) else {}
    if _bbox_is_pathological(ubb):
        return True
    if _is_canvas_action(cs):
        return False
    anchor = dict(tc.anchor_bbox or {}) if tc else {}
    if not anchor and isinstance(uia.get("bbox"), dict):
        anchor = dict(uia.get("bbox") or {})
    sig = dict(tc.signature or {}) if tc else {}
    env = sig.get("environment") or {}
    screen = {}
    if isinstance(env, dict):
        screen = env.get("primary_screen_px") or {}
    window_bbox = None
    try:
        if isinstance(uia.get("window"), dict):
            window_bbox = (uia["window"]).get("bbox")
    except Exception:
        window_bbox = None
    ref_area = 0
    if window_bbox:
        ref_area = _bbox_area(window_bbox)
    if not ref_area and screen:
        ref_area = int(screen.get("w", 0)) * int(screen.get("h", 0))
    if ref_area > 0 and anchor and _bbox_area(anchor) > 0.25 * ref_area:
        return True
    return False


def maybe_promote_click_fallback_identity(cs: CompiledStep) -> bool:
    """Promociona ``click_fallback`` → ``semantic_target`` o ``uia_control``.
    Ejecutar antes del filtro de targets inválidos (§B5).
    """
    if not _is_click_action(cs) or not cs.target_context:
        return False
    if _step_target_source(cs) != "click_fallback":
        return False
    trusted = _trusted_pre_action_signature(cs)
    if _fallback_promotion_blocked_by_geometry(cs) and not trusted:
        return False

    uia = _uia(cs)
    name = (uia.get("name") or "").strip()
    aid = (uia.get("automation_id") or "").strip()
    ct = (uia.get("control_type") or "").strip().lower()
    proc = _process(cs)

    new_src = ""
    if trusted and (name or aid):
        new_src = "uia_control"
    if proc in WINDOWS_SEARCH_PROCESSES and _windows_search_anchor_name(name):
        new_src = "semantic_target"
    elif aid and not _is_technical_name(aid):
        new_src = "uia_control"
    elif name and not _is_technical_name(name) and ct in _STABLE_FALLBACK_PROMOTION_CT:
        new_src = "uia_control"

    if not new_src:
        return False

    sig = dict(cs.target_context.signature or {})
    sig["target_source"] = new_src
    sig["identity_promoted_from"] = "click_fallback"
    cs.target_context.signature = sig

    conf = dict(cs.target_context.confidence or {})
    ql = str(conf.get("quality_level") or "").lower()
    if ql in ("low", "red"):
        conf["quality_level"] = "medium"
    cs.target_context.confidence = conf

    sem = dict(cs.target_context.semantic or {})
    sem.setdefault("identity_promotion_audit", "click_fallback→" + new_src)
    cs.target_context.semantic = sem
    return True


def pass_promote_fallback_identity(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    for cs in compiled:
        try:
            maybe_promote_click_fallback_identity(cs)
        except Exception:
            continue
    return compiled, interp


def _is_generic_ct(cs: CompiledStep) -> bool:
    ct = (_uia(cs).get("control_type") or "").strip().lower()
    return ct in GENERIC_UIA_TYPES


def _is_click_action(cs: CompiledStep) -> bool:
    return cs.action_strategy in (
        ActionStrategy.CLICK,
        ActionStrategy.DOUBLE_CLICK,
        ActionStrategy.RIGHT_CLICK,
    )


def _is_field_text(cs: CompiledStep) -> bool:
    """Step que escribe texto en un campo.

    Aceptamos:
      * ``SET_FIELD_VALUE`` (siempre es campo).
      * ``TYPE_TEXT`` con ``field_session=True`` en payload (el camino
        "limpio" cuando el recorder propaga la marca).
      * ``TYPE_TEXT`` sin marca explícita: lo tratamos como campo si el
        payload trae texto (≥1 char). El recorder a veces emite estos
        cuando el listener no logró abrir field_session pero el texto
        SÍ pertenece a un campo (caso típico: barra de búsqueda de
        YouTube tras un click en un contenedor que el filtro rechazó).
    """
    if cs.action_strategy == ActionStrategy.SET_FIELD_VALUE:
        return True
    if cs.action_strategy == ActionStrategy.TYPE_TEXT:
        pl = cs.action_payload or {}
        if pl.get("field_session"):
            return True
        text = str(pl.get("text") or "")
        return len(text) >= 1
    return False


def _hotkey_is(cs: CompiledStep, key: str) -> bool:
    if cs.action_strategy != ActionStrategy.SEND_HOTKEY:
        return False
    pl = cs.action_payload or {}
    if pl.get("combo"):
        keys = [str(k).lower() for k in (pl.get("keys") or [])]
        return bool(keys) and keys[-1] == key.lower() and len(keys) == 1
    hk = (pl.get("hotkey") or pl.get("key") or "").lower()
    return hk == key.lower()


def _bbox_area(b: Optional[Dict[str, Any]]) -> int:
    if not b:
        return 0
    return max(0, int(b.get("width") or 0)) * max(0, int(b.get("height") or 0))


def _is_canvas_action(cs: CompiledStep) -> bool:
    """Marca explícita en payload o semantic.action_kind == 'canvas'."""
    pl = cs.action_payload or {}
    if pl.get("on_canvas"):
        return True
    sem = (cs.target_context.semantic if cs.target_context else None) or {}
    return sem.get("action_kind") == "canvas"


def _set_semantic(cs: CompiledStep, *,
                  intent: Optional[str] = None,
                  human_label: Optional[str] = None,
                  action_kind: Optional[str] = None,
                  expected_state_after: Optional[str] = None,
                  needs_reinforcement: Optional[bool] = None,
                  label_prompt: Optional[str] = None,
                  ) -> None:
    """Reescribe el bloque ``target_context.semantic`` tras una fusión.

    Sin esto, los pasos convertidos por ``semantic_fusion`` mantienen
    el ``intent='unknown'`` original de cuando eran ``CLICK`` plano —
    y el ``semantic_interpreter`` no los puede traducir bien al output
    final (problema #3 del PRD).
    """
    if not cs.target_context:
        return
    sem = dict(cs.target_context.semantic or {})
    if intent is not None:
        sem["intent"] = intent
    if human_label is not None:
        sem["human_label"] = human_label
    if action_kind is not None:
        sem["action_kind"] = action_kind
    if expected_state_after is not None:
        sem["expected_state_after"] = expected_state_after
    if needs_reinforcement is not None:
        sem["needs_reinforcement"] = bool(needs_reinforcement)
    if label_prompt is not None:
        sem["label_prompt"] = label_prompt
    cs.target_context.semantic = sem


def _is_invalid_click_target(cs: CompiledStep) -> bool:
    """Implementa la regla §B (Invalid Target Filter).

    Mejorado 2026-05-03b para los casos de la misión f1634274:

      * Landmarks HTML como ``main``/``body``/``root`` se reconocen como
        técnicos (``_is_technical_name`` extiende su lista interna).
      * bbox patológico (negativo masivo o >12 000 px) se considera
        inválido, lo lleve UIA en ``anchor_bbox`` o en ``uia_data.bbox``.
      * Cualquier ``target_source == click_fallback`` se considera
        inválido aunque haya un name humano detrás (el name viene del
        UIA pero la coordenada NO está dentro del bbox real → el player
        no podrá reproducir).
    """
    if not _is_click_action(cs):
        return False

    uia = _uia(cs)
    name = (uia.get("name") or "").strip()
    aid = (uia.get("automation_id") or "").strip()
    ct = (uia.get("control_type") or "").strip().lower()
    web = _web(cs)
    has_web_label = any(
        not _is_technical_name((web.get(k) or "").strip())
        and (web.get(k) or "").strip()
        for k in ("accessible_name", "label", "placeholder", "test_id")
    )

    # 1) UIA vacío + CT genérico, sin rescate web.
    if not name and not aid and ct in GENERIC_UIA_TYPES and not has_web_label:
        return True

    # 2) Nombre/aid técnico y nada más.
    only_signals = [s for s in (name, aid) if s]
    if only_signals and all(_is_technical_name(s) for s in only_signals) \
            and not has_web_label:
        return True

    # 3) bbox patológico (coords negativas masivas, >12k px). Esto NO
    #    depende del tamaño de ventana — un bbox así no se puede usar.
    for bb in (
        (cs.target_context.anchor_bbox if cs.target_context else None),
        uia.get("bbox") if isinstance(uia.get("bbox"), dict) else None,
    ):
        if _bbox_is_pathological(bb):
            return True

    # 4) bbox > 25% del área de la ventana (a menos que sea canvas).
    if not _is_canvas_action(cs):
        anchor = (cs.target_context.anchor_bbox if cs.target_context else None) or {}
        if not anchor and isinstance(uia.get("bbox"), dict):
            anchor = uia.get("bbox") or {}
        if anchor:
            sig = (cs.target_context.signature if cs.target_context else None) or {}
            env = (sig.get("environment") or {}) if isinstance(sig, dict) else {}
            screen = (env.get("primary_screen_px") or {}) if isinstance(env, dict) else {}
            ref_area = 0
            window_bbox = None
            try:
                window_bbox = (uia.get("window") or {}).get("bbox") if isinstance(uia.get("window"), dict) else None
            except Exception:
                window_bbox = None
            if window_bbox:
                ref_area = _bbox_area(window_bbox)
            if not ref_area and screen:
                ref_area = int(screen.get("w", 0)) * int(screen.get("h", 0))
            if ref_area > 0:
                if _bbox_area(anchor) > 0.25 * ref_area:
                    return True

    # 5) target_source == click_fallback (independientemente de label).
    #    El UIA puede haber reportado un nombre humano, pero el bbox
    #    real del elemento no contenía la coordenada — el player no
    #    podrá reproducir el click sin OCR/vision adicional.
    if _step_target_source(cs) == "click_fallback":
        if not _trusted_pre_action_signature(cs):
            return True

    # 6) Sin name + sin aid + sin web label → ruido.
    if not name and not aid and not has_web_label:
        return True

    return False


# ──────────────────────────────────────────────────────────────────────
# Pass A: Invalid Target Filter
# ──────────────────────────────────────────────────────────────────────

def pass_invalid_target_filter(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Rescata o conserva clicks sobre targets inválidos.

    Política (post-PRD §B + tests legacy):
      * Si el siguiente step es texto en un campo del mismo proceso,
        el click se entiende como "enfoque accidental" del campo y se
        descarta (silenciosamente) tras propagar contexto al texto.
      * Si NO hay rescate posible, **se conserva** el step. El
        ``approval_gate`` lo bloqueará después con ``invalid_click_target``
        y el usuario podrá arreglarlo desde Mission Review. Esto evita
        eliminar silenciosamente la única acción que el usuario pudo
        registrar (ej. click en un canvas sin UIA — puede ser intencional).
    """
    out_c: List[CompiledStep] = []
    out_i: List[InterpretedStep] = []
    n = len(compiled)
    i = 0
    while i < n:
        cs = compiled[i]
        ii = interp[i] if i < len(interp) else None
        if not _is_invalid_click_target(cs):
            out_c.append(cs)
            if ii is not None:
                out_i.append(ii)
            i += 1
            continue

        # Rescate: si el siguiente step es texto en un campo del MISMO proceso,
        # asumimos que el click invalido fue para enfocar ese campo.
        # Fundimos arrastrando el target_context del click (window_bbox, etc.)
        # al step de texto.
        rescued = False
        if i + 1 < n:
            nxt = compiled[i + 1]
            if _is_field_text(nxt) and _process(nxt) == _process(cs):
                ntc = nxt.target_context
                if ntc and not ntc.process_name:
                    ntc.process_name = cs.target_context.process_name if cs.target_context else None
                if ntc and not ntc.window_title:
                    ntc.window_title = cs.target_context.window_title if cs.target_context else None
                payload = dict(nxt.action_payload or {})
                payload["fused_from_invalid_click"] = True
                nxt.action_payload = payload
                rescued = True

        if rescued:
            i += 1
            continue

        # No hay rescate: conservamos el step para que el usuario lo vea.
        # El ``approval_gate`` lo bloqueará en aprobación.
        out_c.append(cs)
        if ii is not None:
            out_i.append(ii)
        i += 1
    return out_c, out_i


# ──────────────────────────────────────────────────────────────────────
# Pass B: Chrome Workflow (Win Search rescue, profile, new tab, bookmark)
# ──────────────────────────────────────────────────────────────────────

_BROWSER_PROCESSES = {
    "chrome.exe", "msedge.exe", "firefox.exe",
    "brave.exe", "opera.exe", "vivaldi.exe",
}


def pass_chrome_workflow(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Detecta select_profile / open_new_tab / open_bookmark + Win Search rescue.

    Pre-condición: ``pass_invalid_target_filter`` ya descartó la basura.
    Esto significa que un click "fantasma" en el profile picker puede ya
    no estar — pero si sobrevivió (porque el filtro lo dejó pasar), lo
    detectamos aquí mirando el contexto pre/post: ventana cambia de
    ``Google Chrome`` (sin sufijo) a ``... - Google Chrome``.
    """
    if not compiled:
        return compiled, interp

    n = len(compiled)
    out_c: List[CompiledStep] = []
    out_i: List[InterpretedStep] = []
    i = 0

    # Helper: ¿el step está pegado a un LAUNCH_APP browser previo?
    # Mira lo ya emitido en `out_c` y devuelve True si el último paso
    # significativo previo es LAUNCH_APP de un navegador. Si por el camino
    # encuentra OPEN_NEW_TAB/OPEN_BOOKMARK/NAVIGATE_OR_SEARCH/SUBMIT_SEARCH
    # devuelve False (ese LAUNCH_APP ya fue "consumido" por una acción
    # navegacional posterior — el siguiente click no es profile picker).
    def _follows_browser_launch() -> bool:
        if not out_c:
            return False
        for k in range(len(out_c) - 1, -1, -1):
            prev = out_c[k]
            if prev.action_strategy == ActionStrategy.LAUNCH_APP:
                app = str(
                    (prev.action_payload or {}).get("app_name", "")
                ).lower()
                return app in {
                    "chrome", "edge", "firefox", "brave",
                    "opera", "vivaldi",
                }
            if prev.action_strategy in (
                ActionStrategy.SELECT_PROFILE,
                ActionStrategy.OPEN_NEW_TAB,
                ActionStrategy.OPEN_BOOKMARK,
                ActionStrategy.NAVIGATE_OR_SEARCH,
                ActionStrategy.SUBMIT_SEARCH,
                ActionStrategy.OPEN_MENU_ITEM,
                ActionStrategy.SET_FIELD_VALUE,
                ActionStrategy.TYPE_TEXT,
            ):
                # Ya hubo una acción semántica posterior al LAUNCH_APP:
                # el siguiente click NO es profile picker, es una acción
                # de uso normal del navegador.
                return False
        return False

    while i < n:
        cs = compiled[i]
        ii = interp[i] if i < len(interp) else None

        # ── B.1 Win Search rescue: "Escribe aquí para buscar" CLICK ─
        # (LAUNCH_APP del compiler clásico no lo absorbió porque el ORDEN
        # capturado fue: click_search_box → click_result → type_text.
        # Si vemos un CLICK sobre la caja de Win Search seguido de un
        # LAUNCH_APP en la ventana de cualquier resultado de Win Search,
        # fundimos el click en el LAUNCH_APP.
        if _is_click_action(cs):
            uia_name = (_uia(cs).get("name") or "").strip().lower().rstrip(".")
            proc = _process(cs)
            is_search_box = (
                uia_name in {n.rstrip(".") for n in WINDOWS_SEARCH_BOX_NAMES}
                or proc in WINDOWS_SEARCH_PROCESSES
            )
            if is_search_box and i + 1 < n:
                # Buscar el siguiente LAUNCH_APP dentro de los próximos 3 steps
                # (el compiler clásico puede haber metido pasos intermedios).
                for j in range(i + 1, min(n, i + 4)):
                    if compiled[j].action_strategy == ActionStrategy.LAUNCH_APP:
                        # Fundir: descartar el click, conservar el LAUNCH_APP.
                        i += 1
                        break
                else:
                    out_c.append(cs)
                    if ii is not None:
                        out_i.append(ii)
                    i += 1
                continue

        # ── B.2 open_new_tab: click "Nueva pestaña" en navegador ──
        # CHEQUEAR ANTES que profile picker porque "Nueva pestaña" SÍ
        # tiene UIA name específico — no es ambiguo.
        if _is_click_action(cs) and _process(cs) in _BROWSER_PROCESSES:
            name = (_uia(cs).get("name") or "").strip().lower()
            if name in NEW_TAB_NAMES or _hotkey_is(cs, "t"):
                cs.action_strategy = ActionStrategy.OPEN_NEW_TAB
                cs.action_payload = {
                    "app": _process(cs).removesuffix(".exe"),
                }
                cs.validation_strategy = ValidationStrategy.REQUIRE_ELEMENT_EXISTS
                _set_semantic(
                    cs,
                    intent="open_new_tab",
                    action_kind="open_new_tab",
                    expected_state_after="new_tab_opened",
                )
                if ii is not None:
                    ii.description = "➕ Abrir nueva pestaña"
                out_c.append(cs)
                if ii is not None:
                    out_i.append(ii)
                i += 1
                continue

        # ── B.3 open_bookmark: click bookmark en chrome con name conocido ──
        if _is_click_action(cs) and _process(cs) in _BROWSER_PROCESSES:
            name = (_uia(cs).get("name") or "").strip()
            url = BOOKMARK_NAME_TO_URL.get(name.lower())
            if url and not _is_technical_name(name):
                cs.action_strategy = ActionStrategy.OPEN_BOOKMARK
                cs.action_payload = {
                    "name": name,
                    "url": url,
                    "app": _process(cs).removesuffix(".exe"),
                }
                cs.validation_strategy = ValidationStrategy.REQUIRE_TEXT_MATCH
                _set_semantic(
                    cs,
                    intent="open_url",
                    human_label=name,
                    action_kind="open_bookmark",
                    expected_state_after="page_loaded",
                )
                if ii is not None:
                    ii.description = f"🔖 Abrir bookmark '{name}'"
                out_c.append(cs)
                if ii is not None:
                    out_i.append(ii)
                i += 1
                continue

        # ── B.4 select_profile: click ambiguo pegado a LAUNCH_APP ──
        # SOLO entra aquí si NINGÚN patrón anterior matchó. Tras un
        # LAUNCH_APP de un navegador, el primer click DENTRO del proceso
        # con título "Google Chrome" (sin sufijo de pestaña) y SIN UIA
        # name claro, asumimos elección de perfil.
        #
        # AMPLIADO 2026-05-03b:
        #   * Acepta también clicks sobre landmarks HTML técnicos
        #     (``main``, ``body``, ``root``…) que apuntan al picker pero
        #     no fueron capturados como invalid_click_target por la
        #     versión vieja de la regla.
        #   * Si NO podemos extraer el nombre del perfil, marcamos
        #     ``needs_user_label`` con un prompt para que Mission Review
        #     pida al usuario que rellene el nombre.
        if _is_click_action(cs) and _process(cs) in _BROWSER_PROCESSES \
                and _follows_browser_launch():
            cur_title = (cs.target_context.window_title or "").strip() \
                if cs.target_context else ""
            future_title = ""
            for j in range(i + 1, min(n, i + 3)):
                t = (compiled[j].target_context.window_title or "").strip() \
                    if compiled[j].target_context else ""
                if t and t != cur_title:
                    future_title = t
                    break
            uia = _uia(cs)
            uia_name = (uia.get("name") or "").strip().lower()
            uia_aid = (uia.get("automation_id") or "").strip().lower()
            target_is_ambiguous = (
                _is_invalid_click_target(cs)
                or _is_technical_name(uia_aid)
                or _is_technical_name(uia_name)
                or (not uia_name and not uia_aid)
            )
            looks_like_profile = (
                target_is_ambiguous
                and cur_title.lower() in {
                    "google chrome", "microsoft edge",
                    "mozilla firefox", "brave",
                }
            )
            if looks_like_profile:
                cs.action_strategy = ActionStrategy.SELECT_PROFILE
                profile_name = _extract_profile_name(cs, future_title=future_title)
                payload = {
                    "profile": profile_name,
                    "app": _process(cs).removesuffix(".exe"),
                    "fused_from_click": True,
                }
                if not profile_name:
                    payload["needs_user_label"] = True
                    payload["label_prompt"] = "¿Qué perfil seleccionaste?"
                cs.action_payload = payload
                cs.validation_strategy = ValidationStrategy.REQUIRE_TEXT_MATCH
                _set_semantic(cs, intent="select_profile",
                              human_label=profile_name or "perfil del navegador",
                              action_kind="select_profile")
                if ii is not None:
                    if profile_name:
                        ii.description = f"👤 Elegir perfil '{profile_name}'"
                    else:
                        ii.description = "👤 Elegir perfil del navegador (pendiente etiqueta)"
                out_c.append(cs)
                if ii is not None:
                    out_i.append(ii)
                i += 1
                continue

        out_c.append(cs)
        if ii is not None:
            out_i.append(ii)
        i += 1
    return out_c, out_i


def _extract_profile_name(cs: CompiledStep, *, future_title: str = "") -> str:
    """Best-effort: extrae el nombre del perfil de UIA / signature / título.

    El profile picker no expone el nombre por UIA (GroupControl vacío). En
    ausencia de OCR sobre el asset, devolvemos cadena vacía y dejamos que
    el usuario rellene desde Mission Review. Lo que SÍ podemos:

      - Si el future_title cambió a algo tipo ``"Avances de Nevlan - Google Chrome"``,
        usamos esa primera parte como hint de espacio de trabajo.
      - Si el web_data trae ``profile_name`` (Playwright lo lee del menú).
    """
    web = _web(cs)
    pn = str(web.get("profile_name") or "").strip()
    if pn:
        return pn

    # Workspace title hint (no es el profile, pero es información útil).
    if future_title and " - " in future_title:
        head = future_title.split(" - ", 1)[0].strip()
        if head and head.lower() not in {"google chrome", "microsoft edge"}:
            return ""  # workspace ≠ profile, no inventar
    return ""


# ──────────────────────────────────────────────────────────────────────
# Pass C: Field Session Merger v2
# ──────────────────────────────────────────────────────────────────────

def pass_field_session_merger(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Reúne fragmentos de un mismo campo separados por Enter accidental.

    Patrón:
        TYPE_TEXT "De"  → Enter → TYPE_TEXT "Devuelveme el amor Luis Miguel"
                                  (mismo proceso/ventana)
        ⇒
        SET_FIELD_VALUE/TYPE_TEXT "Devuelveme el amor Luis Miguel" submit_after=True
        (los dos primeros pasos se descartan)
    """
    if len(compiled) < 3:
        return compiled, interp

    out_c: List[CompiledStep] = []
    out_i: List[InterpretedStep] = []
    n = len(compiled)
    i = 0
    while i < n:
        cs = compiled[i]
        ii = interp[i] if i < len(interp) else None

        if _is_field_text(cs) and i + 2 < n:
            mid = compiled[i + 1]
            tail = compiled[i + 2]
            tail_i = interp[i + 2] if i + 2 < len(interp) else None
            if _hotkey_is(mid, "enter") and _is_field_text(tail) \
                    and _process(tail) == _process(cs):
                a = str((cs.action_payload or {}).get("text", ""))
                b = str((tail.action_payload or {}).get("text", ""))
                if _is_prefix_or_supersede(a, b):
                    payload = dict(tail.action_payload or {})
                    payload["text"] = b
                    payload["submit_after"] = True
                    payload["merged_from_fragments"] = [a, b]
                    _enrich_search_payload(payload, tail)
                    tail.action_payload = payload
                    tail.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
                    _set_semantic(
                        tail,
                        intent="search" if payload.get("site") else "type_text",
                        action_kind="search" if payload.get("site") else "type_text",
                        expected_state_after="results_visible"
                        if payload.get("site") else "field_value_set",
                    )
                    if tail_i is not None:
                        merged_ids: List[str] = []
                        for k in (i, i + 1, i + 2):
                            if k < len(interp):
                                merged_ids += list(interp[k].raw_event_ids)
                        seen: set = set()
                        tail_i.raw_event_ids = [
                            x for x in merged_ids
                            if not (x in seen or seen.add(x))
                        ]
                        short = b if len(b) <= 40 else b[:40] + "…"
                        site = payload.get("site")
                        if site:
                            tail_i.description = (
                                f"🔎 Buscar en {site}: '{short}' (Enter)"
                            )
                        else:
                            tail_i.description = (
                                f"📝 Rellenar campo con '{short}' y enviar (Enter)"
                            )
                    out_c.append(tail)
                    if tail_i is not None:
                        out_i.append(tail_i)
                    i += 3
                    continue

        # No fusión: si el step es field_text en YouTube/Google/etc.
        # también enriquecemos para que el ``semantic_interpreter`` pueda
        # emitir un ``search`` con site/target aunque NO haya Enter
        # (caso: el usuario aún no envió pero ya fundimos la sesión).
        if _is_field_text(cs):
            payload = dict(cs.action_payload or {})
            if _enrich_search_payload(payload, cs):
                cs.action_payload = payload
                _set_semantic(
                    cs,
                    intent="search",
                    action_kind="search",
                    expected_state_after="results_visible",
                )

        out_c.append(cs)
        if ii is not None:
            out_i.append(ii)
        i += 1
    return out_c, out_i


# ──────────────────────────────────────────────────────────────────────
# Enriquecimiento de search payload (PRD 2026-05-03b §2)
# ──────────────────────────────────────────────────────────────────────

# Sitios con barra de búsqueda canónica conocida. La clave es lo que
# debe aparecer en ``window_title`` (case-insensitive); el valor son
# (site_id, target_label) que el player usa para localizar la barra.
_KNOWN_SEARCH_SITES: List[Tuple[str, str, str]] = [
    ("youtube", "youtube", "youtube_search_box"),
    ("google", "google", "google_search_box"),
    ("amazon", "amazon", "amazon_search_box"),
    ("github", "github", "github_search_box"),
    ("twitter", "twitter", "twitter_search_box"),
    (" - x", "x", "x_search_box"),
    ("wikipedia", "wikipedia", "wikipedia_search_box"),
    ("stackoverflow", "stackoverflow", "stackoverflow_search_box"),
    ("stack overflow", "stackoverflow", "stackoverflow_search_box"),
    ("reddit", "reddit", "reddit_search_box"),
]


def _enrich_search_payload(payload: Dict[str, Any], cs: CompiledStep) -> bool:
    """Si el ``window_title`` o el host del web_data identifica un sitio
    conocido, añade ``site`` y ``target_label`` al payload. Devuelve
    True si hubo enriquecimiento."""
    if payload.get("site"):
        return True
    title = ""
    url = ""
    try:
        title = (cs.target_context.window_title or "").lower()
    except Exception:
        title = ""
    try:
        web = cs.target_context.web_data or {}
        url = (web.get("url") or "").lower()
    except Exception:
        url = ""
    haystack = " ".join([title, url])
    for token, site_id, target_label in _KNOWN_SEARCH_SITES:
        if token in haystack:
            payload["site"] = site_id
            payload["target_label"] = target_label
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Pass D: Red-step Guard
# ──────────────────────────────────────────────────────────────────────

def pass_red_step_guard(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Garantiza que NINGÚN ``CLICK`` rojo o ``click_fallback`` salga
    como acción ejecutable (PRD §5).

    Lo que hace:
      * Si un click sobrevivió a las pasadas anteriores (sin fundirse a
        ``select_profile`` / ``open_new_tab`` / ``open_bookmark`` ni
        rescatarse a un campo de texto) Y su ``target_source`` es
        ``click_fallback`` o su ``quality_level`` es ``red/low``, lo
        marcamos:
          - ``payload["needs_reinforcement"] = True``
          - ``payload["label_prompt"] = "¿Sobre qué elemento hiciste click?"``
          - ``semantic.intent = "needs_user_label"``
          - ``semantic.needs_reinforcement = True``
        El paso queda en el grafo como **placeholder no ejecutable**.
        El ``semantic_interpreter`` lo emite como ``type:
        needs_user_label``; el ``approval_gate`` lo bloquea hasta que el
        usuario lo arregle desde Mission Review.
      * No tocamos otros tipos de acción (TYPE_TEXT, LAUNCH_APP, etc.)
        — sólo CLICKs.
    """
    for cs, ii in zip(compiled, interp + [None] * (len(compiled) - len(interp))):
        if not _is_click_action(cs):
            continue
        if not _is_red_or_fallback_click(cs):
            continue
        payload = dict(cs.action_payload or {})
        payload["needs_reinforcement"] = True
        payload.setdefault(
            "label_prompt", "¿Sobre qué elemento hiciste click?",
        )
        cs.action_payload = payload
        cs.validation_strategy = ValidationStrategy.REQUIRE_ELEMENT_EXISTS
        _set_semantic(
            cs,
            intent="needs_user_label",
            action_kind="needs_user_label",
            needs_reinforcement=True,
            label_prompt=payload["label_prompt"],
        )
        if ii is not None:
            ii.description = "⚠️ Refuerzo necesario: etiqueta de target"
    return compiled, interp


def _is_prefix_or_supersede(a: str, b: str) -> bool:
    """`a` es prefijo case-insensitive de `b`, o `b` lo subsume."""
    if not b:
        return False
    if not a:
        return True
    a_lo = a.strip().lower()
    b_lo = b.strip().lower()
    if a_lo == b_lo:
        return True
    if b_lo.startswith(a_lo) and len(b_lo) >= len(a_lo):
        return True
    # Subsume: el segundo es completamente nuevo (>3x más largo) — el primero
    # fue un fragmento accidental.
    if len(b_lo) >= 3 * max(len(a_lo), 1):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Pass E: drop_redundant_focus_clicks (PRD 2026-05-03c §4)
# ──────────────────────────────────────────────────────────────────────

def pass_drop_redundant_focus_clicks(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Elimina CLICKs sobre contenedores genéricos en navegador cuando
    inmediatamente después hay un ``TYPE_TEXT``/``SET_FIELD_VALUE`` en
    la misma ventana.

    El caso típico es el click sobre ``guide-service``/``ytd-app`` antes
    de tipear en la barra de búsqueda de YouTube: el click es sólo una
    re-foco accidental del cursor, pero el listener lo captura igual.
    Si lo dejamos pasa al ``compiled_execution_graph`` como un step
    ejecutable (rojo), cuando en realidad es ruido.

    Reglas:
      * Sólo aplica a ``CLICK``/``DOUBLE_CLICK``/``RIGHT_CLICK``.
      * El target debe ser inválido (``_is_invalid_click_target``) o
        landmark técnico — no podemos descartar clicks legítimos.
      * El proceso debe ser un navegador conocido.
      * En las próximas ≤2 posiciones del grafo debe haber un
        ``TYPE_TEXT``/``SET_FIELD_VALUE`` del MISMO proceso.
      * El step rescatado por ``invalid_target_filter`` ya no llega aquí;
        este pass se ejecuta DESPUÉS del filtro y captura los que el
        filtro decidió "preservar para que el approval gate los reporte".
    """
    n = len(compiled)
    drop_idx: set = set()
    for i, cs in enumerate(compiled):
        if not _is_click_action(cs):
            continue
        if _process(cs) not in _BROWSER_PROCESSES:
            continue
        if not _is_invalid_click_target(cs):
            continue
        for j in range(i + 1, min(n, i + 3)):
            nxt = compiled[j]
            if _is_field_text(nxt) and _process(nxt) == _process(cs):
                drop_idx.add(i)
                break
    if not drop_idx:
        return compiled, interp
    out_c = [cs for k, cs in enumerate(compiled) if k not in drop_idx]
    out_i = [ii for k, ii in enumerate(interp) if k not in drop_idx]
    return out_c, out_i


# ──────────────────────────────────────────────────────────────────────
# Pass F: search_finalizer (PRD 2026-05-03c §3)
# ──────────────────────────────────────────────────────────────────────

def pass_search_finalizer(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Mop-up final: cualquier ``TYPE_TEXT`` adyacente a un ``SEND_HOTKEY``
    Enter en el mismo proceso debe quedar como UN único step ``search``
    con ``submit_after=True``, sin importar el orden.

    Casos cubiertos:
      A) ``TYPE_TEXT → SEND_HOTKEY(enter)``
         → fusiona Enter dentro del TYPE_TEXT como ``submit_after=True``.
      B) ``SEND_HOTKEY(enter) → TYPE_TEXT``
         → orden invertido por timestamps casi simultáneos del recorder;
         absorbemos el Enter en el TYPE_TEXT siguiente.

    En ambos casos enriquecemos el payload con ``site``/``target_label``
    si la ``window_title`` delata un sitio conocido.

    Este pass se ejecuta TRAS ``field_session_merger`` para mop-up de
    parejas que el merger v2 (que requiere fragmento + enter +
    fragmento) no ve.
    """
    out_c: List[CompiledStep] = []
    out_i: List[InterpretedStep] = []
    i, n = 0, len(compiled)
    while i < n:
        cs = compiled[i]
        ii = interp[i] if i < len(interp) else None

        # Patrón A: text → enter
        if _is_field_text(cs) and i + 1 < n:
            nxt = compiled[i + 1]
            if _hotkey_is(nxt, "enter") and _process(nxt) == _process(cs):
                payload = dict(cs.action_payload or {})
                payload["submit_after"] = True
                _enrich_search_payload(payload, cs)
                cs.action_payload = payload
                cs.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
                _set_semantic(
                    cs,
                    intent="search" if payload.get("site") else "type_text",
                    action_kind="search" if payload.get("site") else "type_text",
                    expected_state_after="results_visible"
                    if payload.get("site") else "field_value_set",
                )
                if ii is not None and i + 1 < len(interp):
                    ii.raw_event_ids = list(ii.raw_event_ids) + list(
                        interp[i + 1].raw_event_ids
                    )
                    text = str(payload.get("text", ""))
                    short = text if len(text) <= 40 else text[:40] + "…"
                    site = payload.get("site")
                    ii.description = (
                        f"🔎 Buscar en {site}: '{short}' (Enter)"
                        if site
                        else f"📝 '{short}' y enviar (Enter)"
                    )
                out_c.append(cs)
                if ii is not None:
                    out_i.append(ii)
                i += 2
                continue

        # Patrón B: enter → text (timestamps invertidos)
        if _hotkey_is(cs, "enter") and i + 1 < n:
            nxt = compiled[i + 1]
            if _is_field_text(nxt) and _process(nxt) == _process(cs):
                payload = dict(nxt.action_payload or {})
                payload["submit_after"] = True
                _enrich_search_payload(payload, nxt)
                nxt.action_payload = payload
                nxt.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
                _set_semantic(
                    nxt,
                    intent="search" if payload.get("site") else "type_text",
                    action_kind="search" if payload.get("site") else "type_text",
                    expected_state_after="results_visible"
                    if payload.get("site") else "field_value_set",
                )
                tail_i = interp[i + 1] if i + 1 < len(interp) else None
                if tail_i is not None and ii is not None:
                    tail_i.raw_event_ids = list(ii.raw_event_ids) + list(
                        tail_i.raw_event_ids
                    )
                    text = str(payload.get("text", ""))
                    short = text if len(text) <= 40 else text[:40] + "…"
                    site = payload.get("site")
                    tail_i.description = (
                        f"🔎 Buscar en {site}: '{short}' (Enter)"
                        if site
                        else f"📝 '{short}' y enviar (Enter)"
                    )
                out_c.append(nxt)
                if tail_i is not None:
                    out_i.append(tail_i)
                i += 2
                continue

        out_c.append(cs)
        if ii is not None:
            out_i.append(ii)
        i += 1
    return out_c, out_i


# ──────────────────────────────────────────────────────────────────────
# Pipeline público
# ──────────────────────────────────────────────────────────────────────

def apply_semantic_fusion(
    compiled: List[CompiledStep], interp: List[InterpretedStep],
) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
    """Aplica todos los passes en orden canónico.

    Orden CRÍTICO:
      0. ``pass_promote_fallback_identity``: sube ``click_fallback`` acoplado
         a UIA fuerte → ``uia_control`` / ``semantic_target`` (antes de §B).
      1. ``chrome_workflow`` PRIMERO: necesita ver el click "huérfano"
         sobre ``GroupControl`` vacío del profile picker para convertirlo
         en ``SELECT_PROFILE``. Si filtramos antes, perdemos esa señal.
      2. ``invalid_target_filter``: limpia los CLICKs sin intención humana
         que NO fueron rescatados como acciones semánticas.
      3. ``drop_redundant_focus_clicks``: descarta clicks "foco" sobre
         contenedores genéricos en navegador justo antes de tipeo.
      4. ``field_session_merger``: une fragmentos de tecleo separados por
         Enter accidental (text → enter → text).
      5. ``search_finalizer``: mop-up de parejas text+enter (en cualquier
         orden) que el merger v2 no captura.
      6. ``red_step_guard``: garantiza que ningún CLICK rojo o
         click_fallback sobreviva como acción ejecutable.
    """
    compiled, interp = pass_promote_fallback_identity(compiled, interp)
    compiled, interp = pass_chrome_workflow(compiled, interp)
    compiled, interp = pass_invalid_target_filter(compiled, interp)
    compiled, interp = pass_drop_redundant_focus_clicks(compiled, interp)
    compiled, interp = pass_field_session_merger(compiled, interp)
    compiled, interp = pass_search_finalizer(compiled, interp)
    compiled, interp = pass_red_step_guard(compiled, interp)
    return compiled, interp


def confirm_step_label(
    cs: CompiledStep,
    *,
    profile: Optional[str] = None,
    target_label: Optional[str] = None,
    semantic_label: Optional[str] = None,
    user_confirmed_label: Optional[str] = None,
) -> None:
    """Aplica una etiqueta humana a un step que tenía
    ``needs_user_label=True`` o ``needs_reinforcement=True`` y limpia
    todas las marcas de pendiente.

    Pensado para invocarse desde Mission Review (Quick Reinforcement
    UI) cuando el usuario confirma la intención sugerida o introduce
    una etiqueta manual. Tras llamarlo:

      * ``action_payload`` ya NO tiene ``needs_user_label`` /
        ``needs_reinforcement`` / ``label_prompt``.
      * Se persiste ``user_confirmed_label`` y ``semantic_label`` en el
        payload Y en ``target_context.semantic`` para auditar el origen.
      * Si la acción es ``SELECT_PROFILE``, el ``profile`` queda en
        ``payload.profile`` y ``semantic.human_label``.
      * El intent se vuelve a poner explícito (no ``unknown``).

    El caller debe re-correr ``check_mission_approvable`` para
    determinar el nuevo status.
    """
    pl = dict(cs.action_payload or {})
    label = (
        user_confirmed_label
        or profile
        or target_label
        or semantic_label
        or ""
    ).strip()
    pl.pop("needs_user_label", None)
    pl.pop("needs_reinforcement", None)
    pl.pop("label_prompt", None)
    if cs.action_strategy == ActionStrategy.SELECT_PROFILE and profile:
        pl["profile"] = profile.strip()
    if target_label:
        pl["target_label"] = target_label.strip()
    if label:
        pl["user_confirmed_label"] = label
    if semantic_label:
        pl["semantic_label"] = semantic_label.strip()
    cs.action_payload = pl

    if cs.target_context is not None:
        sem = dict(cs.target_context.semantic or {})
        sem.pop("needs_reinforcement", None)
        sem.pop("label_prompt", None)
        if label:
            sem["user_confirmed_label"] = label
        if semantic_label:
            sem["semantic_label"] = semantic_label.strip()
        if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
            sem["intent"] = "select_profile"
            sem["action_kind"] = "select_profile"
            if profile:
                sem["human_label"] = profile.strip()
        elif cs.action_strategy == ActionStrategy.CLICK and label:
            # Estaba como ``needs_user_label`` (placeholder) — al
            # confirmar, el operador nos dice qué se cliqueó. Subimos
            # el intent a ``click_button`` con la etiqueta humana.
            sem["intent"] = "click_button"
            sem["action_kind"] = "click"
            sem["human_label"] = label
        cs.target_context.semantic = sem


__all__ = [
    "apply_semantic_fusion",
    "pass_promote_fallback_identity",
    "maybe_promote_click_fallback_identity",
    "pass_invalid_target_filter",
    "pass_chrome_workflow",
    "pass_drop_redundant_focus_clicks",
    "pass_field_session_merger",
    "pass_search_finalizer",
    "pass_red_step_guard",
    "confirm_step_label",
    "TECHNICAL_NAME_PATTERNS",
    "LANDMARK_TECHNICAL_NAMES",
    "BOOKMARK_NAME_TO_URL",
    "NEW_TAB_NAMES",
    "WINDOWS_SEARCH_BOX_NAMES",
]
