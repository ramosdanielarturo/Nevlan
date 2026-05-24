"""
Nevlan Semantic Mission Interpreter
====================================

Transforma una ``Mission`` (raw_trace + field_commit + window_context) en un
output JSON limpio, semántico, agrupado en *bloques de fase humana*. Es la
capa de "lo que un humano experto haría" sobre el ``compiled_execution_graph``
técnico que consume el player.

Pipeline
--------

    raw_trace
       │
       ▼   (MissionCompiler — agrupación, hotkeys, field sessions,
           Windows-search collapse, patrones v3, normalización)
    compiled_execution_graph
       │
       ▼   (semantic_interpreter — aquí abajo)
    {
      "mission_summary": "...",
      "blocks": [
        {
          "id": "b1",
          "name": "Abrir entorno",
          "steps": [
            {
              "id": "s1",
              "type": "open_app",
              "params": {"app": "chrome"},
              "verification": "chrome.exe running",
              "confidence": 0.95
            },
            ...
          ]
        }
      ]
    }

Reglas clave
------------

1. **Fusión semántica**:
   ``LAUNCH_APP``, ``NAVIGATE_OR_SEARCH``, ``SUBMIT_SEARCH`` y
   ``SET_FIELD_VALUE+submit_after`` ya vienen fusionados desde el compiler
   técnico — aquí solo los traducimos al esquema corto.

2. **Eliminación de ruido**:
   Se descartan clicks sobre contenedores genéricos (``GroupControl``,
   ``PaneControl``, ``guide-service``, ``ytd-*``, ``video-stream``,
   ``container``, ``wrapper`` …), scrolls aislados, Tab/Enter sueltos,
   CapsLock, y cualquier paso con ``confidence_score < 0.5``.

3. **Field commit**:
   Para campos siempre se toma ``metadata.field_commit.chosen_final_text``
   (con fallback a ``user_typed_text`` si la lectura del DOM/UIA fue
   contaminada por una URL del navegador).

4. **Detección de intenciones**:
   - Windows search + texto + Enter      → ``open_app``
   - Click en perfil tras Chrome         → ``select_profile``
   - Ctrl+L + texto URL + Enter          → ``open_url``
   - Ctrl+L + texto no-URL + Enter       → ``search`` (motor por defecto)
   - SET_FIELD_VALUE + submit en YouTube → ``search`` con ``site=youtube``
   - SET_FIELD_VALUE sin submit          → ``fill_field``
   - SET_FIELD_VALUE + submit en form    → ``submit_form``

5. **Validación obligatoria**:
   Cada paso lleva ``verification`` derivado del tipo. Nunca ``null``.

6. **Target limpio**:
   Solo se incluye ``target`` cuando aporta valor (label humano + role).
   Las coordenadas crudas y los IDs autogenerados quedan fuera del JSON
   (siguen viviendo en el ``compiled_execution_graph`` para el player).

7. **Orden lógico**:
   Heredamos el orden ya saneado por el compiler técnico (que ordena por
   ``timestamp + sequence_id`` y aplica reordenes semánticos).

8. **Reducción + Bloques**:
   Se cortan bloques cuando cambia:
   - el proceso (Windows ↔ Chrome ↔ Excel),
   - la fase (entorno → navegación → búsqueda → formulario → envío),
   - o cuando un bloque alcanza ``MAX_STEPS_PER_BLOCK``.

9. **Score de confianza**:
   Cada paso tiene ``confidence ∈ [0, 1]``. Steps con < 0.5 se descartan.

10. **Optimización de tokens**:
    Keys cortas (``app``, ``val``, ``url``), descripciones de intent en
    una línea minúscula. Sin logs, sin razonamiento, sin texto extra.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    Mission,
)
from app.services.missions.compiler import MissionCompiler
from app.services.missions.field_commit import (
    is_browser_internal_url,
    is_url_contamination,
)
from app.services.missions.launch_apps import (
    canonical_app_name,
    process_names_for,
)


# ──────────────────────────────────────────────────────────────────────
# Constantes de configuración
# ──────────────────────────────────────────────────────────────────────

MAX_STEPS_PER_BLOCK = 6  # objetivo 5–7 según spec
MIN_CONFIDENCE = 0.5     # debajo de esto el paso se descarta

# Etiquetas técnicas que NO son intención humana — si el único descriptor
# del target es uno de estos, descartamos el paso.
_TECHNICAL_LABEL_PATTERNS = (
    "guide-service", "ytd-app", "ytd-search", "ytd-",
    "app-root", "app-shell", "react-root",
    "container", "wrapper", "outer", "inner", "root",
    "video-stream",
)

# ControlTypes UIA genéricos sin valor humano — un click sobre ellos
# es ruido salvo que tenga ``automation_id`` significativo.
_GENERIC_UIA_TYPES = {
    "groupcontrol", "panecontrol", "customcontrol",
    "documentcontrol", "windowcontrol", "toolbarcontrol",
    "statusbarcontrol", "group", "pane", "custom",
    "document", "window", "toolbar", "statusbar",
}

# Roles web genéricos que no aportan intención humana.
_GENERIC_WEB_ROLES = {"generic", "presentation", "none", ""}

# Navegadores conocidos: tras un LAUNCH_APP de uno de estos, el siguiente
# click "humano" suele ser elegir perfil.
_BROWSERS = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi"}


# ──────────────────────────────────────────────────────────────────────
# Helpers de inspección de un CompiledStep
# ──────────────────────────────────────────────────────────────────────

def _label_for(cs: CompiledStep) -> str:
    """Mejor etiqueta humana posible: web > uia > payload.label."""
    tc = cs.target_context
    web = (tc.web_data or {}) if tc else {}
    uia = (tc.uia_data or {}) if tc else {}
    pl = cs.action_payload or {}

    for key in ("accessible_name", "label", "placeholder", "name_attr"):
        v = (web.get(key) or "").strip()
        if v and not _is_technical(v):
            return v

    name = (uia.get("name") or "").strip()
    aid = (uia.get("automation_id") or "").strip()
    ct = (uia.get("control_type") or "").strip().lower()
    if name and not _is_technical(name) and ct not in _GENERIC_UIA_TYPES:
        return name
    if aid and not _is_technical(aid):
        return aid

    for key in ("label", "field_label", "menu_item"):
        v = str(pl.get(key) or "").strip()
        if v and not _is_technical(v):
            return v

    return ""


def _role_for(cs: CompiledStep) -> str:
    """Role corto: web role > UIA control_type normalizado."""
    tc = cs.target_context
    web = (tc.web_data or {}) if tc else {}
    role = (web.get("role") or "").strip().lower()
    if role and role not in _GENERIC_WEB_ROLES:
        return role
    ct = ((tc.uia_data or {}).get("control_type") or "").strip().lower() if tc else ""
    if ct.endswith("control"):
        ct = ct[: -len("control")]
    if ct and ct not in {c.replace("control", "") for c in _GENERIC_UIA_TYPES}:
        return ct
    return ""


def _process_for(cs: CompiledStep) -> str:
    """Nombre de proceso normalizado o cadena vacía."""
    tc = cs.target_context
    if not tc:
        return ""
    return (tc.process_name or "").strip().lower()


def _domain_for(cs: CompiledStep) -> str:
    """Dominio del campo web del paso, o cadena vacía."""
    tc = cs.target_context
    web = (tc.web_data or {}) if tc else {}
    dom = (web.get("domain") or "").strip().lower()
    if dom:
        return dom
    url = (web.get("url") or "").strip().lower()
    if url:
        try:
            parsed = urlparse(url if "://" in url else "http://" + url)
            return (parsed.hostname or "").lower()
        except Exception:
            return ""
    return ""


_SITE_CANONICAL = {
    "youtube": "YouTube",
    "google": "Google",
    "github": "GitHub",
    "gmail": "Gmail",
    "stackoverflow": "Stack Overflow",
    "twitter": "Twitter",
    "x": "X",
    "linkedin": "LinkedIn",
}


def _site_label_for_domain(domain: str) -> str:
    """'youtube.com' → 'YouTube', 'youtube' → 'YouTube'. ``""`` si no se puede inferir."""
    if not domain:
        return ""
    parts = domain.split(".")
    base = parts[-2] if len(parts) >= 2 and parts[-2] else parts[0]
    return _SITE_CANONICAL.get(base, base.capitalize())


def _is_technical(label: str) -> bool:
    """¿La etiqueta es un id técnico autogenerado?"""
    if not label:
        return False
    lo = label.strip().lower()
    for pat in _TECHNICAL_LABEL_PATTERNS:
        if pat in lo:
            return True
    # Heurística: id slug (sin espacios + con guiones + largo).
    if " " not in lo and "-" in lo and len(lo) >= 10:
        # Pero no descartar dominios reales (ej. "google.com" no aplica
        # porque tiene punto, no guion).
        return True
    return False


def _confidence_pct_to_unit(pct: Optional[float]) -> float:
    """100-scale del compiler → 0–1. Sentinel 0.5 si no hay dato."""
    if pct is None:
        return 0.5
    try:
        return round(max(0.0, min(1.0, float(pct) / 100.0)), 2)
    except Exception:
        return 0.5


def _looks_like_url(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if t.startswith(("http://", "https://", "ftp://")):
        return True
    # 'youtube.com', 'github.com/foo' — TLD presente, sin espacios.
    if " " not in t and "." in t and "/" not in t.split(".")[0]:
        tld = t.split("/")[0].rsplit(".", 1)[-1]
        if 2 <= len(tld) <= 6 and tld.isalpha():
            return True
    return False


def _short_url(url: str) -> str:
    """'https://www.youtube.com/' → 'youtube.com'."""
    if not url:
        return ""
    t = url.strip()
    try:
        if "://" not in t:
            t = "http://" + t
        host = (urlparse(t).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        return host or url.strip()
    except Exception:
        return url.strip()


def _final_text_from_field_session(cs: CompiledStep) -> str:
    """Aplica field_commit: prefiere ``chosen_final_text`` o user_typed.

    Para SET_FIELD_VALUE / TYPE_TEXT con field_session=True:
      1. ``metadata.field_commit.chosen_final_text`` si existe.
      2. ``metadata.field_commit.user_typed_text`` si la lectura DOM/UIA
         parece URL contaminada.
      3. ``payload.text`` como fallback.
    """
    pl = cs.action_payload or {}
    raw_text = str(pl.get("text", ""))
    fc = pl.get("field_commit") if isinstance(pl.get("field_commit"), dict) else None
    if not fc:
        # field_commit puede estar en target_context.signature o en metadata
        # del raw_event original; aquí confiamos en ``payload.text`` que ya
        # absorbió la decisión del recorder en _build_field_commit_result.
        return raw_text

    chosen = str(fc.get("chosen_final_text") or "").strip()
    if chosen:
        return chosen
    user_typed = str(fc.get("user_typed_text") or "").strip()
    if is_url_contamination(read_value=raw_text, user_typed=user_typed):
        return user_typed or raw_text
    return raw_text or user_typed


# ──────────────────────────────────────────────────────────────────────
# Filtros de ruido
# ──────────────────────────────────────────────────────────────────────

def _is_noise_click(cs: CompiledStep) -> bool:
    """¿Click sin valor humano? — sin nombre claro, control type genérico,
    o etiqueta técnica autogenerada."""
    label = _label_for(cs)
    role = _role_for(cs)
    if label and not _is_technical(label):
        return False
    # Sin label humano, evaluamos rol: un botón sin label es ruido,
    # un link en webs SPA también suele ser un contenedor.
    if role in _GENERIC_WEB_ROLES or role in {"generic", "presentation"}:
        return True
    # Clase UIA genérica sin label: ruido.
    ct = ((cs.target_context.uia_data or {}).get("control_type") or "").strip().lower() \
        if cs.target_context else ""
    if ct in _GENERIC_UIA_TYPES:
        return True
    # Sin label y sin role → ruido.
    return not bool(label)


def _is_noise_hotkey(cs: CompiledStep) -> bool:
    """Tab/Enter/CapsLock sueltos son ruido — ya fueron absorbidos por
    los patrones (NAVIGATE_OR_SEARCH, submit_after) cuando aportaban
    intención. Si quedan sueltos, no son acción humana significativa."""
    pl = cs.action_payload or {}
    keys = [str(k).lower() for k in (pl.get("keys") or [])]
    hk = str(pl.get("hotkey") or pl.get("key") or "").lower()
    if not keys and hk:
        keys = [hk]
    if not keys:
        return True
    if len(keys) == 1 and keys[0] in {"tab", "enter", "capslock", "caps_lock"}:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Traducción CompiledStep → SemanticStep dict
# ──────────────────────────────────────────────────────────────────────

def _verification_for(stype: str, params: Dict[str, Any]) -> str:
    """Verificación obligatoria. Nunca devuelve cadena vacía."""
    if stype == "open_app":
        app = str(params.get("app", "")).lower()
        procs = process_names_for(app)
        if procs:
            return f"{procs[0].lower()} running"
        return f"{app or 'app'} window opened"
    if stype == "select_profile":
        return f"profile '{params.get('profile', '')}' loaded"
    if stype == "open_url":
        host = _short_url(str(params.get("url", "")))
        return f"{host or 'page'} loaded"
    if stype == "search":
        site = str(params.get("site", "")).lower()
        return f"{site} results visible" if site else "results visible"
    if stype == "submit_form":
        return "form submitted"
    if stype == "fill_field":
        label = str(params.get("label", "")).strip()
        return f"field '{label}' contains value" if label else "field contains value"
    if stype == "open_menu_item":
        item = params.get("item") or params.get("menu_item") or ""
        return f"menu '{item}' triggered" if item else "menu action triggered"
    if stype == "confirm_dialog":
        ans = params.get("answer", "ok")
        return f"dialog {ans}"
    if stype == "scroll_to":
        label = params.get("label", "")
        return f"'{label}' visible" if label else "target visible"
    if stype in ("click", "double_click", "right_click"):
        label = params.get("label", "")
        return f"'{label}' state changed" if label else "element state changed"
    if stype == "hotkey":
        return "hotkey applied"
    if stype == "drag":
        return "element dropped at target"
    if stype == "needs_user_label":
        return "user must label target before execution"
    if stype == "open_new_tab":
        return "new tab visible"
    return "state changed"


def _semantic_step_from_compiled(
    cs: CompiledStep, *, prev_intent: str, prev_was_browser_launch: bool,
) -> Optional[Dict[str, Any]]:
    """Traduce un ``CompiledStep`` a un step semántico dict, o ``None``
    si es ruido / no aporta intención humana.

    ``prev_was_browser_launch`` permite mapear el primer click claro tras
    abrir Chrome/Edge a ``select_profile``.
    """
    a = cs.action_strategy
    pl = cs.action_payload or {}
    label = _label_for(cs)

    base_conf_pct = None
    try:
        base_conf_pct = float((cs.target_context.confidence or {}).get("capture_score"))
    except Exception:
        pass

    # ── 1. LAUNCH_APP ─────────────────────────────────────────────
    if a == ActionStrategy.LAUNCH_APP:
        app_name = str(pl.get("app_name") or pl.get("raw_typed") or "").strip()
        app_canon = canonical_app_name(app_name).lower() if app_name else ""
        params: Dict[str, Any] = {"app": app_canon or app_name.lower()}
        method = pl.get("launch_method")
        if method and method != "windows_search":
            params["method"] = method
        return {
            "type": "open_app",
            "params": params,
            "confidence": 0.95,
            "_meta": {"process": app_canon or ""},
        }

    # ── 1b. SELECT_PROFILE / OPEN_NEW_TAB / OPEN_BOOKMARK ────────
    if a == ActionStrategy.SELECT_PROFILE:
        # Si no hay nombre de perfil, emitimos un step semántico pidiendo
        # al usuario que lo etiquete (PRD 2026-05-03b §1).
        if pl.get("needs_user_label") or not str(pl.get("profile", "")).strip():
            return {
                "type": "needs_user_label",
                "params": {
                    "context": "select_profile",
                    "app": str(pl.get("app", "")),
                    "prompt": str(pl.get("label_prompt") or "¿Qué perfil seleccionaste?"),
                },
                "confidence": 0.5,
                "_meta": {"needs_reinforcement": True},
            }
        params = {"profile": str(pl.get("profile", ""))}
        if pl.get("app"):
            params["app"] = str(pl["app"])
        return {
            "type": "select_profile",
            "params": params,
            "confidence": 0.85,
        }
    if a == ActionStrategy.OPEN_NEW_TAB:
        return {
            "type": "open_new_tab",
            "params": {"app": str(pl.get("app", ""))} if pl.get("app") else {},
            "confidence": 0.9,
        }
    if a == ActionStrategy.OPEN_BOOKMARK:
        params = {"name": str(pl.get("name", ""))}
        if pl.get("url"):
            params["url"] = _short_url(str(pl["url"]))
        return {
            "type": "open_url",
            "params": params,
            "confidence": 0.9,
        }

    # ── 2. NAVIGATE_OR_SEARCH (Ctrl+L + texto + Enter) ────────────
    if a == ActionStrategy.NAVIGATE_OR_SEARCH:
        text = str(pl.get("text") or "").strip()
        if _looks_like_url(text):
            return {
                "type": "open_url",
                "params": {"url": _short_url(text)},
                "confidence": 0.9,
            }
        if text:
            return {
                "type": "search",
                "params": {"val": text, "submit": True},
                "confidence": 0.9,
            }
        return None  # Ctrl+L vacío sin valor

    # ── 3. SET_FIELD_VALUE / TYPE_TEXT ────────────────────────────
    if a in (ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT):
        text = _final_text_from_field_session(cs)
        if not text.strip():
            return None  # Campo vacío → no es intención, es ruido
        # Defensa de profundidad contra contaminación URL navegador.
        if is_browser_internal_url(text):
            return None
        submit = bool(pl.get("submit_after"))
        # site / target_label vienen de semantic_fusion._enrich_search_payload
        # cuando el window_title delata un sitio conocido (YouTube, Google,
        # GitHub, etc.). Damos prioridad a esos sobre el dominio web.
        site_kind = (
            str(pl.get("site") or "").strip().lower()
            or str(pl.get("search_site") or "").strip().lower()
        )
        target_label = str(pl.get("target_label") or "").strip()
        domain = _domain_for(cs) if not site_kind else ""

        if submit:
            params: Dict[str, Any] = {"val": text, "submit": True}
            site_resolved = site_kind or _site_label_for_domain(domain).lower()
            if site_resolved:
                params["site"] = site_resolved
            if target_label:
                params["target"] = target_label
            elif site_resolved:
                params["target"] = f"{site_resolved}_search_box"
            return {
                "type": "search",
                "params": params,
                "confidence": 0.9,
                "_meta": {"label": label},
            }
        # Sin submit → llenado de campo
        params = {"val": text}
        if label and not _is_technical(label):
            params["label"] = label
        return {
            "type": "fill_field",
            "params": params,
            "target": _target_dict(cs),
            "confidence": _confidence_pct_to_unit(base_conf_pct) or 0.7,
        }

    # ── 4. SUBMIT_SEARCH (legacy / pattern) ───────────────────────
    if a == ActionStrategy.SUBMIT_SEARCH:
        text = str(pl.get("text") or pl.get("val") or "").strip()
        if not text:
            return None
        site_kind = (
            str(pl.get("site") or "").strip().lower()
            or str(pl.get("search_site") or "").strip().lower()
        )
        target_label = str(pl.get("target_label") or "").strip()
        params: Dict[str, Any] = {"val": text, "submit": True}
        if site_kind:
            params["site"] = site_kind
        if target_label:
            params["target"] = target_label
        elif site_kind:
            params["target"] = f"{site_kind}_search_box"
        return {
            "type": "search",
            "params": params,
            "confidence": 0.9,
        }

    # ── 5. OPEN_MENU_ITEM ─────────────────────────────────────────
    if a == ActionStrategy.OPEN_MENU_ITEM:
        item = str(pl.get("menu_item") or label or "").strip()
        root = str(pl.get("menu_root") or "").strip()
        if not item:
            return None
        params = {"item": item}
        if root:
            params["menu"] = root
        return {
            "type": "open_menu_item",
            "params": params,
            "target": _target_dict(cs),
            "confidence": _confidence_pct_to_unit(base_conf_pct) or 0.8,
        }

    # ── 6. CONFIRM_DIALOG ─────────────────────────────────────────
    if a == ActionStrategy.CONFIRM_DIALOG:
        return {
            "type": "confirm_dialog",
            "params": {"answer": pl.get("answer") or "ok"},
            "confidence": 0.7,
        }

    # ── 7. SCROLL_UNTIL_VISIBLE ───────────────────────────────────
    if a == ActionStrategy.SCROLL_UNTIL_VISIBLE:
        marker = pl.get("scroll_until") or {}
        slabel = (
            marker.get("uia_name")
            or marker.get("web_name")
            or marker.get("web_label")
            or marker.get("web_test_id")
            or label
            or ""
        ).strip()
        if not slabel or _is_technical(slabel):
            return None
        return {
            "type": "scroll_to",
            "params": {"label": slabel},
            "confidence": 0.7,
        }

    # ── 8. SCROLL aislado → ruido ─────────────────────────────────
    if a == ActionStrategy.SCROLL:
        return None

    # ── 9. CLICK / DOUBLE_CLICK / RIGHT_CLICK ─────────────────────
    if a in (ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK):
        if _is_noise_click(cs):
            return None

        # 9a. Click marcado por ``semantic_fusion.pass_red_step_guard``
        # como necesitado de etiqueta humana (PRD 2026-05-03b §5).
        # Estos clicks NO son ejecutables: el player no sabe sobre qué
        # reproducirlos. Los emitimos como ``needs_user_label`` para
        # que Mission Review pida al usuario que los corrija.
        if pl.get("needs_reinforcement"):
            return {
                "type": "needs_user_label",
                "params": {
                    "context": "click",
                    "prompt": str(
                        pl.get("label_prompt")
                        or "¿Sobre qué elemento hiciste click?"
                    ),
                    "process": _process_for(cs) or "",
                },
                "confidence": 0.4,
                "_meta": {"needs_reinforcement": True},
            }

        # Heurística: tras LAUNCH_APP de un navegador, un click claro
        # suele ser elegir perfil.
        proc_match = _process_for(cs) in {
            p.lower() for p in process_names_for(prev_intent_app(prev_intent))
        } if prev_was_browser_launch else False
        if prev_was_browser_launch and label and not _is_technical(label) \
                and not _looks_like_url(label) \
                and label.lower() not in {"new tab", "nueva pestaña"}:
            return {
                "type": "select_profile",
                "params": {"profile": label},
                "confidence": 0.85,
            }

        stype = {
            ActionStrategy.CLICK: "click",
            ActionStrategy.DOUBLE_CLICK: "double_click",
            ActionStrategy.RIGHT_CLICK: "right_click",
        }[a]
        return {
            "type": stype,
            "params": {"label": label},
            "target": _target_dict(cs),
            "confidence": _confidence_pct_to_unit(base_conf_pct) or 0.7,
        }

    # ── 10. SEND_HOTKEY (solo las semánticas conocidas) ───────────
    if a == ActionStrategy.SEND_HOTKEY:
        if _is_noise_hotkey(cs):
            return None
        keys = [str(k).lower() for k in (pl.get("keys") or [])]
        hk = str(pl.get("hotkey") or pl.get("key") or "").lower()
        if not keys and hk:
            keys = [hk]
        # Solo conservamos hotkeys con intención humana clara.
        combo = "+".join(keys)
        semantic_known = {
            "ctrl+s": ("save", 0.85),
            "ctrl+t": ("open_new_tab", 0.8),
            "ctrl+w": ("close_tab", 0.75),
            "ctrl+c": ("copy", 0.8),
            "ctrl+v": ("paste", 0.8),
            "ctrl+x": ("cut", 0.75),
            "ctrl+z": ("undo", 0.75),
            "alt+f4": ("close_window", 0.8),
        }
        if combo in semantic_known:
            stype, conf = semantic_known[combo]
            return {"type": stype, "params": {}, "confidence": conf}
        return None  # Hotkey sin intención reconocida → ruido

    # ── 11. DRAG_DROP ─────────────────────────────────────────────
    if a == ActionStrategy.DRAG_DROP:
        return {
            "type": "drag",
            "params": {
                "from": [int(pl.get("start_x", 0)), int(pl.get("start_y", 0))],
                "to": [int(pl.get("end_x", 0)), int(pl.get("end_y", 0))],
            },
            "confidence": 0.7,
        }

    return None


def prev_intent_app(prev_intent: str) -> str:
    """Extrae el app del intent anterior si fue ``open_app:<name>``."""
    if not prev_intent or ":" not in prev_intent:
        return ""
    head, tail = prev_intent.split(":", 1)
    return tail if head == "open_app" else ""


def _target_dict(cs: CompiledStep) -> Optional[Dict[str, Any]]:
    """Target mínimo y semántico — solo si aporta valor humano."""
    if not cs.target_context:
        return None
    label = _label_for(cs)
    role = _role_for(cs)
    web = (cs.target_context.web_data or {})
    uia = (cs.target_context.uia_data or {})
    proc = _process_for(cs)

    if not label and not role:
        return None

    out: Dict[str, Any] = {}
    if label:
        out["label"] = label
    if role:
        out["role"] = role
    uia_name = (uia.get("name") or "").strip()
    if uia_name and uia_name != label and not _is_technical(uia_name):
        out["uia_name"] = uia_name
    if proc:
        out["app"] = proc
    domain = _domain_for(cs)
    if domain and "app" not in out:
        out["app"] = domain
    web_test_id = (web.get("test_id") or "").strip()
    if web_test_id:
        out["test_id"] = web_test_id
    out["fallback"] = ["uia", "ocr", "vision"]
    return out


# ──────────────────────────────────────────────────────────────────────
# Particionado en bloques
# ──────────────────────────────────────────────────────────────────────

# Mapa intent → categoría de fase. El cambio de categoría dispara un
# nuevo bloque (junto al cambio de proceso).
_PHASE_BY_TYPE = {
    "open_app": "environment",
    "select_profile": "environment",
    "open_url": "navigate",
    "open_new_tab": "navigate",
    "close_tab": "navigate",
    "search": "search",
    "fill_field": "form_fill",
    "submit_form": "form_submit",
    "open_menu_item": "menu",
    "confirm_dialog": "dialog",
    "scroll_to": "scroll",
    "click": "interact",
    "double_click": "interact",
    "right_click": "interact",
    "save": "save",
    "copy": "interact",
    "paste": "interact",
    "cut": "interact",
    "undo": "interact",
    "close_window": "environment",
    "drag": "interact",
    "hotkey": "interact",
    # ``needs_user_label`` se queda en ``environment`` para que se
    # agrupe con el ``open_app`` previo (el caso típico es un perfil
    # de Chrome sin etiqueta extraíble — sigue siendo "abrir Chrome").
    # Cuando el contexto sea otro (click sin label en una página web,
    # por ejemplo) ya se romperá el bloque por cambio de proceso.
    "needs_user_label": "environment",
}

_BLOCK_NAMES = {
    "environment": "Abrir entorno",
    "navigate": "Navegar",
    "search": "Buscar contenido",
    "form_fill": "Capturar datos",
    "form_submit": "Enviar información",
    "menu": "Abrir menú",
    "dialog": "Confirmar diálogo",
    "scroll": "Localizar contenido",
    "interact": "Realizar acción",
    "save": "Guardar",
}


def _block_name(category: str, steps: List[Dict[str, Any]]) -> str:
    """Nombre humano del bloque, enriquecido si hay sitio detectable."""
    base = _BLOCK_NAMES.get(category, "Acción")
    if not steps:
        return base
    # 1. Si el bloque es ``environment`` y el primer step es ``open_app``,
    #    usamos el nombre de la app: "Abrir Chrome" en vez de
    #    "Abrir entorno". Cubre el caso típico Bloque 1 del PRD.
    if category == "environment":
        first = steps[0]
        if first.get("type") == "open_app":
            app = str((first.get("params") or {}).get("app") or "").strip()
            if app:
                return f"Abrir {app.capitalize()}"
    # 2. Buscar un sitio (host > URL > app) priorizando lo que NO sea el
    #    navegador, para que un bloque "open_new_tab + open_url Youtube"
    #    se llame "Abrir YouTube" y no "Navegar a Chrome".
    site = ""
    for src_key in ("site", "url", "name", "app"):
        for s in steps:
            p = s.get("params") or {}
            candidate = p.get(src_key) or ""
            if not candidate:
                continue
            if isinstance(candidate, str) and "." in candidate:
                resolved = _site_label_for_domain(candidate)
            else:
                resolved = (
                    _site_label_for_domain(str(candidate))
                    or str(candidate).capitalize()
                )
            # Saltamos nombres genéricos del navegador para evitar
            # "Abrir Chrome" en un bloque que en realidad va a YouTube.
            if resolved.lower() in {"chrome", "edge", "firefox", "brave"}:
                continue
            if resolved:
                site = resolved
                break
        if site:
            break
    if category == "navigate" and site:
        return f"Abrir {site}"
    if category == "search" and site:
        return f"Buscar en {site}"
    return base


def _partition_into_blocks(
    semantic_steps: List[Dict[str, Any]],
    compiled: List[CompiledStep],
) -> List[Dict[str, Any]]:
    """Corta los steps en bloques por cambio de fase / proceso / overflow."""
    if not semantic_steps:
        return []
    blocks: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    current_cat: Optional[str] = None
    current_proc: Optional[str] = None

    def flush() -> None:
        nonlocal current, current_cat
        if not current:
            return
        cat = current_cat or _PHASE_BY_TYPE.get(current[0]["type"], "interact")
        blocks.append({
            "name": _block_name(cat, current),
            "steps": list(current),
        })
        current = []

    for step in semantic_steps:
        cat = _PHASE_BY_TYPE.get(step["type"], "interact")
        proc = (step.get("_meta") or {}).get("process") or ""
        # Cambios que disparan nuevo bloque:
        boundary_cat = current_cat is not None and cat != current_cat \
            and not _phases_compatible(current_cat, cat)
        boundary_proc = (
            current_proc is not None and proc and proc != current_proc
            and current_proc != ""
        )
        boundary_overflow = len(current) >= MAX_STEPS_PER_BLOCK

        if current and (boundary_cat or boundary_proc or boundary_overflow):
            flush()

        current.append(step)
        current_cat = current_cat or cat
        if cat != current_cat and _phases_compatible(current_cat, cat):
            # Mantenemos la categoría dominante del bloque (ej: search +
            # form_fill compatibles colapsan en "search").
            pass
        else:
            current_cat = cat
        if proc:
            current_proc = proc

    flush()
    # Limpiar campos privados antes de exponer.
    for b in blocks:
        for s in b["steps"]:
            s.pop("_meta", None)
    return blocks


def _phases_compatible(a: str, b: str) -> bool:
    """Fases que pueden convivir en el mismo bloque sin sentirse forzadas."""
    pairs = {
        ("environment", "environment"),
        ("navigate", "navigate"),
        ("search", "search"),
        ("form_fill", "form_fill"),
        ("form_fill", "form_submit"),  # rellenar + enviar = mismo bloque
        ("interact", "interact"),
        ("scroll", "interact"),         # scroll + click consecutivo
    }
    return (a, b) in pairs or (b, a) in pairs


# ──────────────────────────────────────────────────────────────────────
# Mission summary
# ──────────────────────────────────────────────────────────────────────

def _build_mission_summary(blocks: List[Dict[str, Any]], mission: Mission) -> str:
    """Resumen breve de la misión basado en los pasos semánticos."""
    if not blocks:
        return (mission.name or "").strip() or "Misión vacía"

    # Buscamos el último step "objetivo" (search, submit_form, save, fill_field
    # con submit). Esa suele ser la intención final de la misión.
    final = None
    for b in reversed(blocks):
        for s in reversed(b["steps"]):
            if s["type"] in ("search", "submit_form", "save"):
                final = s
                break
        if final:
            break
    if final is None:
        final = blocks[-1]["steps"][-1]

    if final["type"] == "search":
        val = (final.get("params") or {}).get("val", "")
        site = (final.get("params") or {}).get("site", "")
        if site:
            return f"Buscar '{val}' en {_site_label_for_domain(site) or site.capitalize()}"
        return f"Buscar '{val}'"
    if final["type"] == "submit_form":
        return "Enviar formulario"
    if final["type"] == "save":
        return "Guardar archivo"
    if final["type"] == "fill_field":
        label = (final.get("params") or {}).get("label", "")
        return f"Completar campo {label}".strip()
    if final["type"] == "open_url":
        host = (final.get("params") or {}).get("url", "")
        return f"Abrir {_site_label_for_domain(host) or host}"
    if final["type"] == "open_app":
        app = (final.get("params") or {}).get("app", "")
        return f"Abrir {app.capitalize() or 'aplicación'}"
    return (mission.name or "Misión").strip()


# ──────────────────────────────────────────────────────────────────────
# Entry point público
# ──────────────────────────────────────────────────────────────────────

def interpret_mission(mission: Mission, *, recompile: bool = True) -> Dict[str, Any]:
    """Devuelve la representación semántica por bloques de una ``Mission``.

    Args:
        mission: misión a interpretar (puede o no estar compilada).
        recompile: si ``True`` y la misión no tiene ``compiled_execution_graph``
            pero sí ``raw_trace``, se ejecuta el ``MissionCompiler`` técnico
            para producirlo. Pon ``False`` cuando ya tengas la misión
            compilada y quieras evitar trabajo redundante.

    Returns:
        ``dict`` con la forma::

            {
              "mission_summary": "...",
              "blocks": [
                {"id": "b1", "name": "...", "steps": [...]},
                ...
              ]
            }

    Notas:
        - El output **no contiene** logs ni razonamiento.
        - Cada step lleva ``id``, ``type``, ``params``, ``verification`` y
          ``confidence`` ∈ [0,1]. ``target`` solo aparece cuando aporta valor.
        - Los pasos por debajo de ``MIN_CONFIDENCE`` se descartan
          silenciosamente (precisión > completitud).
    """
    if recompile and mission.raw_trace and not mission.compiled_execution_graph:
        mission = MissionCompiler.compile_mission(mission)

    compiled = list(mission.compiled_execution_graph or [])
    if not compiled:
        return {
            "mission_summary": (mission.name or "").strip() or "Misión vacía",
            "blocks": [],
        }

    # ── 1. Traducir cada CompiledStep a SemanticStep o None ─────
    semantic_steps: List[Dict[str, Any]] = []
    prev_intent = ""
    prev_was_browser_launch = False
    for cs in compiled:
        st = _semantic_step_from_compiled(
            cs,
            prev_intent=prev_intent,
            prev_was_browser_launch=prev_was_browser_launch,
        )
        if st is None:
            continue
        if float(st.get("confidence", 0.0)) < MIN_CONFIDENCE:
            continue

        # Verificación obligatoria.
        st["verification"] = _verification_for(st["type"], st.get("params") or {})

        semantic_steps.append(st)

        # Estado para el próximo loop:
        prev_intent = f"{st['type']}:{(st.get('params') or {}).get('app', '')}"
        prev_was_browser_launch = (
            st["type"] == "open_app"
            and (st.get("params") or {}).get("app", "").lower() in _BROWSERS
        )

    # ── 2. Reducir pasos consecutivos triviales ─────────────────
    semantic_steps = _collapse_redundant(semantic_steps)

    # ── 3. Particionar en bloques de fase ───────────────────────
    blocks = _partition_into_blocks(semantic_steps, compiled)

    # ── 4. Asignar IDs estables y secuenciales ──────────────────
    sid = 1
    for bi, b in enumerate(blocks, start=1):
        b["id"] = f"b{bi}"
        ordered_steps = []
        for s in b["steps"]:
            s_out = {
                "id": f"s{sid}",
                "type": s["type"],
                "params": s.get("params") or {},
            }
            if s.get("target"):
                s_out["target"] = s["target"]
            s_out["verification"] = s.get("verification") or "state changed"
            s_out["confidence"] = float(s.get("confidence", 0.7))
            ordered_steps.append(s_out)
            sid += 1
        # Reordenar campos: id, name, steps
        b_out = {"id": b["id"], "name": b["name"], "steps": ordered_steps}
        blocks[bi - 1] = b_out

    summary = _build_mission_summary(blocks, mission)
    return {"mission_summary": summary, "blocks": blocks}


def _collapse_redundant(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Elimina pasos consecutivos redundantes:
    - dos ``open_app`` seguidos del mismo app,
    - ``fill_field`` cuyo valor es prefijo del siguiente sobre el mismo label,
    - ``click`` idéntico repetido.
    """
    if len(steps) < 2:
        return steps
    out: List[Dict[str, Any]] = []
    for s in steps:
        if not out:
            out.append(s)
            continue
        prev = out[-1]
        if s["type"] == prev["type"]:
            sp = s.get("params") or {}
            pp = prev.get("params") or {}

            if s["type"] == "open_app" and sp.get("app") == pp.get("app"):
                continue  # duplicado
            if s["type"] == "fill_field" and sp.get("label") == pp.get("label"):
                # mantenemos el más completo (último prevalece si extiende)
                a = str(pp.get("val") or "")
                b = str(sp.get("val") or "")
                if b.lower().startswith(a.lower()) or a.lower() == b.lower():
                    out[-1] = s
                    continue
            if s["type"] in ("click", "double_click", "right_click"):
                if sp.get("label") == pp.get("label") and sp.get("label"):
                    continue  # mismo botón clickeado dos veces seguidas
        out.append(s)
    return out


__all__ = ["interpret_mission", "MAX_STEPS_PER_BLOCK", "MIN_CONFIDENCE"]
