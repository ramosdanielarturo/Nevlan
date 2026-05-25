"""
ArthurOS Services - Semantic → SmartExecutor Adapter
----------------------------------------------------
Convierte la representación **semántica** de una misión — la que produce
``interpret_mission`` (bloques humanos con ``type``/``params``/
``verification``/``confidence``) — en una lista plana de
``MissionStep`` que el :class:`SmartMissionExecutor` puede ejecutar.

Objetivo (PRD 2026-05-03d §2)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
El motor inteligente (state detect → precondition → action →
postcondition → checkpoint) ya está implementado en ``smart_executor``,
pero hasta ahora la UI seguía invocando ``MissionPlayer`` (replay lineal
de eventos crudos). Este adaptador es el *puente* entre ambos mundos:

1.  Toma una ``Mission`` ya compilada (o interpretada) y devuelve los
    ``MissionStep`` canónicos del Smart Executor.
2.  Aplica reglas de **bloqueo duras** antes de permitir la ejecución:
    perfil vacío, query vacío, target vacío, kind desconocido, etc.
    → estos casos devuelven ``NEEDS_REVIEW`` sin generar steps.
3.  Es **stateless**: ninguna función aquí toca disco, ni el store, ni
    inicia procesos. Pura transformación de datos.

Entradas aceptadas
~~~~~~~~~~~~~~~~~~
El adaptador acepta dos formatos:

* ``Mission`` con ``compiled_execution_graph`` (traducimos cada
  ``CompiledStep`` usando el mismo diccionario que produciría el
  ``semantic_interpreter``, SIN reconstruir la salida JSON entera).
* ``dict`` con forma ``{"blocks": [{"id": ..., "steps": [...]}, ...]}``
  — el output directo de ``interpret_mission``.

Este doble camino evita volver a compilar la misión cuando ya tenemos
``compiled_execution_graph`` (más rápido, respeta la regla del PRD de
"no recompilar a menos que sea necesario").

Mapeo semantic → MissionStep (según PRD §2)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    open_app         → MissionStep(kind="open_app",
                                   params={"name": app})
    select_profile   → MissionStep(kind="select_profile",
                                   params={"profile_name": profile})
    open_new_tab     → MissionStep(kind="open_new_tab",
                                   params={"app": "chrome"})
    open_url/        → MissionStep(kind="open_url",
    open_bookmark                  params={"alias": name, "url": url})
    search /         → MissionStep(kind="search_youtube",
    search_youtube                 params={"query": val,
                                           "site": "youtube",
                                           "target": "youtube_search_box"})

Reglas (hard fails → ``NEEDS_REVIEW``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``select_profile`` sin ``profile`` (y sin ``needs_user_label``
  explícito) ⇒ blocker ``empty_profile``.
* ``search``/``search_youtube`` sin ``target`` ni ``query`` ⇒ blockers
  ``empty_target`` / ``empty_query``.
* Un bloque con ``type`` no reconocido ⇒ blocker ``unknown_kind``.

La función :func:`adapt_mission_to_steps` devuelve un ``AdaptedMission``:
mistos steps + lista de blockers. El caller (UI/runner bridge) decide si
ejecutar o abrir Mission Review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urlparse

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    Mission,
)
from app.services.missions.execution_contracts import MissionStep


# ─── Tipos de entrada/salida ──────────────────────────────────────────


@dataclass
class AdaptedBlocker:
    """Motivo por el que un step semántico NO puede ejecutarse tal cual.

    El adaptador no arregla estos casos: deja el trabajo a Mission Review
    y a ``approval_gate.classify_status`` (que marca la misión como
    ``NEEDS_REVIEW`` cuando hay al menos un blocker de esta familia).
    """

    code: str                # "empty_profile" | "empty_query" | ...
    message: str             # texto humano listo para la UI
    step_index: Optional[int] = None
    step_type: Optional[str] = None


@dataclass
class AdaptedMission:
    """Resultado de :func:`adapt_mission_to_steps`.

    Attributes:
        steps: lista lista para ``SmartMissionExecutor.run_mission(steps)``.
            Está vacía si la misión es legacy o si todos los bloques están
            vacíos / bloqueados.
        blockers: razones duras por las que la ejecución no debe arrancar.
            Si alguna existe, el caller DEBE parar y pedir refuerzo.
        is_semantic: ``True`` si la misión trae bloques semánticos
            reconocibles (aunque algunos hayan sido descartados). ``False``
            para misiones legacy (puro raw_trace + compiler viejo).
        skipped: steps descartados silenciosamente (confianza baja,
            ``type=="noop"``, etc.) — devuelve visibilidad para el modo
            experto pero NO bloquea ejecución.
    """

    steps: List[MissionStep] = field(default_factory=list)
    blockers: List[AdaptedBlocker] = field(default_factory=list)
    is_semantic: bool = False
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers and bool(self.steps)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blockers)


# ─── Kinds soportados por el Smart Executor ───────────────────────────

_SUPPORTED_KINDS = {
    "open_app",
    "select_profile",
    "select_entity_from_collection",
    "open_new_tab",
    "open_url",
    "search_content",
    "search_site",
    "search_youtube",
}

# Aliases semánticos que produce ``interpret_mission`` / ``semantic_fusion``
# y que el Smart Executor todavía no modela como paso propio. Se ignoran
# explícitamente (retornan ``None`` en el convertidor) para no bloquear
# la misión entera con un unknown_kind.
_SILENT_SKIP_TYPES = {
    "needs_user_label",      # lo maneja approval_gate
    "confirm_dialog",         # contrato UOL confirm_dialog:uol_dialog
    "scroll_to",              # lo cubre el player; smart executor no aún
    "open_menu_item",         # idem
    "fill_field",             # sin submit: no es "misión ejecutable"
    "submit_form",            # contrato UOL submit_form:uol_submit
    "noop",                   # marcador sintético
    # V2 Intent Compiler: waits dinámicos inyectados entre bloques.
    # El Smart Executor ya hace su propio wait via state_detector +
    # postcondition; estos waits explícitos son hints opcionales que
    # ignoramos para no duplicar esperas. (El compiler V3 emitirá
    # WAIT_FOR_STATE como step de primera clase en el futuro.)
    "wait_for_state",
    "scroll_results",         # equivale a scroll dinámico
}


# ─── API de entrada ────────────────────────────────────────────────────

SemanticInput = Union[Mission, Dict[str, Any], List[Dict[str, Any]]]


def adapt_mission_to_steps(
    source: SemanticInput,
    *,
    mission_id: Optional[str] = None,
) -> AdaptedMission:
    """Convierte una misión (o su árbol semántico) en ``MissionStep`` para
    el Smart Executor.

    Args:
        source:
            * ``Mission``  — se prefiere ``compiled_execution_graph``; si
              está vacío, se usa ``interpreted_steps`` o se devuelve una
              adaptación vacía (``is_semantic=False``).
            * ``dict`` con ``{"blocks": [...]}`` — formato directo de
              ``interpret_mission``.
            * ``list[dict]`` — lista plana de steps semánticos
              (``[{type:..., params:...}, ...]``). Útil para tests.
        mission_id: opcional, se usa como parte de los ``id`` generados
            para los ``MissionStep`` (facilita logs y checkpoints).

    Returns:
        :class:`AdaptedMission` con los steps y los blockers. Si
        ``ok`` es ``True`` y no hay blockers, el caller puede pasar
        ``steps`` directo a ``SmartMissionExecutor.run_mission``.
    """
    mid = (mission_id or "").strip() or "m"
    flat = _flatten_semantic(source)
    if not flat:
        # Puede ser legacy (sin compiled + sin semantic output) o
        # simplemente vacía. Diferenciamos por tipo en el caller.
        return AdaptedMission(is_semantic=False)

    out_steps: List[MissionStep] = []
    blockers: List[AdaptedBlocker] = []
    skipped: List[str] = []

    for i, entry in enumerate(flat):
        raw_type = str(entry.get("type") or "").strip()
        if not raw_type:
            skipped.append(f"step#{i}: sin type")
            continue

        if raw_type in _SILENT_SKIP_TYPES:
            skipped.append(f"step#{i}: {raw_type} (ignorado)")
            continue

        step, err = _convert_one(
            entry,
            index=i,
            block_id=entry.get("_block_id"),
            mission_id=mid,
        )
        if err is not None:
            blockers.append(err)
            continue
        if step is not None:
            try:
                from app.services.missions.universal_operational_language import (
                    enrich_mission_step_with_uol,
                )

                step = enrich_mission_step_with_uol(step)
            except Exception:
                pass
            out_steps.append(step)

    adapted = AdaptedMission(
        steps=out_steps,
        blockers=blockers,
        is_semantic=True,
        skipped=skipped,
    )
    return adapted


# ─── Flatten ───────────────────────────────────────────────────────────


def _flatten_semantic(source: SemanticInput) -> List[Dict[str, Any]]:
    """Devuelve una lista plana de dicts semánticos con forma
    ``{"type", "params", "confidence", "_block_id"}``.

    Maneja los tres formatos de entrada aceptados.
    """
    # Caso A: lista plana (tests, consumer custom)
    if isinstance(source, list):
        return [dict(x) for x in source if isinstance(x, dict)]

    # Caso B: dict con "blocks" (output de interpret_mission)
    if isinstance(source, dict):
        out: List[Dict[str, Any]] = []
        for block in source.get("blocks") or []:
            bid = block.get("id")
            for step in block.get("steps") or []:
                d = dict(step)
                d["_block_id"] = bid
                out.append(d)
        return out

    # Caso C: Mission
    if isinstance(source, Mission):
        return _mission_to_flat(source)

    return []


def _mission_to_flat(mission: Mission) -> List[Dict[str, Any]]:
    """Traduce una ``Mission`` a dicts semánticos.

    Prioridad:
      1. Si hay ``compiled_execution_graph`` ⇒ mapa directo por
         ``action_strategy``. Es el camino rápido y el que usa la UI
         cuando la misión ya pasó por el compiler.
      2. Si no, y hay ``interpreted_steps``/``action_groups``, delegamos
         a :func:`interpret_mission` (un poco más caro: corre semantic
         interpreter).

    Devuelve ``[]`` si la misión no trae nada semántico (legacy pura).
    """
    compiled = list(mission.compiled_execution_graph or [])
    if compiled:
        return _from_compiled_steps(compiled)

    # Fallback: intentar interpret_mission sin recompilar (no hay
    # compiled_execution_graph → recompile=False devolvería vacío, así
    # que permitimos recompile=True). Importamos aquí para evitar ciclo.
    try:
        from app.services.missions.semantic_interpreter import interpret_mission
        out = interpret_mission(mission, recompile=False) or {}
    except Exception:
        return []
    flat: List[Dict[str, Any]] = []
    for block in out.get("blocks") or []:
        for step in block.get("steps") or []:
            d = dict(step)
            d["_block_id"] = block.get("id")
            flat.append(d)
    return flat


def _from_compiled_steps(
    compiled: Iterable[CompiledStep],
) -> List[Dict[str, Any]]:
    """Mapea cada ``CompiledStep`` semántico a un dict
    ``{type, params}`` equivalente al que produce
    ``semantic_interpreter._semantic_step_from_compiled`` (pero mucho
    más liviano: sólo los kinds que el Smart Executor sabe ejecutar).
    """
    out: List[Dict[str, Any]] = []
    for cs in compiled:
        pl = dict(cs.action_payload or {})
        a = cs.action_strategy

        if a == ActionStrategy.LAUNCH_APP:
            app_name = str(pl.get("app_name") or pl.get("app") or "").strip()
            if not app_name:
                continue
            out.append({
                "type": "open_app",
                "params": {"app": app_name},
                "_source_step_id": cs.id,
            })
            continue

        if a == ActionStrategy.SELECT_PROFILE:
            profile = str(pl.get("profile", "")).strip()
            needs_label = bool(pl.get("needs_user_label"))
            out.append({
                "type": "select_profile",
                "params": {
                    "profile": profile,
                    "app": str(pl.get("app", "")).strip(),
                    "needs_user_label": needs_label,
                },
                "_source_step_id": cs.id,
            })
            continue

        if a == ActionStrategy.OPEN_NEW_TAB:
            out.append({
                "type": "open_new_tab",
                "params": {"app": str(pl.get("app", "")).strip() or "chrome"},
                "_source_step_id": cs.id,
            })
            continue

        if a == ActionStrategy.OPEN_BOOKMARK:
            name = str(pl.get("name", "")).strip()
            url = str(pl.get("url", "")).strip()
            out.append({
                "type": "open_url",
                "params": {"name": name, "url": url},
                "_source_step_id": cs.id,
            })
            continue

        if a == ActionStrategy.NAVIGATE_OR_SEARCH:
            text = str(pl.get("text") or "").strip()
            if not text:
                continue
            if _looks_like_url(text):
                out.append({
                    "type": "open_url",
                    "params": {"url": text},
                    "_source_step_id": cs.id,
                })
            else:
                # Búsqueda "ambigua": si veníamos de Chrome con Ctrl+L
                # y texto libre, lo mapeamos a una búsqueda en YouTube
                # sólo si el texto trae la pista (no el caso aquí).
                # Sin ``site`` fiable, lo dejamos ir como search
                # genérica — el Smart Executor no lo soporta aún, así
                # que se silencia y el blocker quedará en un step
                # posterior con site=youtube si aplica.
                out.append({
                    "type": "search",
                    "params": {"val": text, "submit": True},
                    "_source_step_id": cs.id,
                })
            continue

        if a == ActionStrategy.SUBMIT_SEARCH:
            text = str(pl.get("text") or pl.get("val") or "").strip()
            site = (
                str(pl.get("site") or "").strip().lower()
                or str(pl.get("search_site") or "").strip().lower()
            )
            target = str(pl.get("target_label") or pl.get("target") or "").strip()
            params: Dict[str, Any] = {"val": text, "submit": True}
            if site:
                params["site"] = site
            if target:
                params["target"] = target
            out.append({
                "type": "search",
                "params": params,
                "_source_step_id": cs.id,
            })
            continue

        if a in (ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT):
            if not pl.get("submit_after"):
                # Es fill_field: no ejecutable por Smart Executor. Skip.
                continue
            text = str(pl.get("text") or "").strip()
            site = (
                str(pl.get("site") or "").strip().lower()
                or str(pl.get("search_site") or "").strip().lower()
            )
            target = str(pl.get("target_label") or pl.get("target") or "").strip()
            if not text:
                continue
            params = {"val": text, "submit": True}
            if site:
                params["site"] = site
            if target:
                params["target"] = target
            out.append({
                "type": "search",
                "params": params,
                "_source_step_id": cs.id,
            })
            continue

        # ── V2: pasos sintéticos del Intent Compiler V2 ───────────
        # WAIT_FOR_STATE se ignora (Smart Executor ya espera por
        # postcondition); SCROLL_UNTIL_VISIBLE se traduce a un dict
        # ``scroll_results`` que cae en _SILENT_SKIP_TYPES (no
        # ejecutable aún por Smart Executor, pero no bloquea).
        if a == ActionStrategy.WAIT_FOR_STATE:
            out.append({
                "type": "wait_for_state",
                "params": dict(pl),
                "_source_step_id": cs.id,
            })
            continue
        if a == ActionStrategy.SCROLL_UNTIL_VISIBLE:
            out.append({
                "type": "scroll_results",
                "params": dict(pl),
                "_source_step_id": cs.id,
            })
            continue

        # El resto de acciones (CLICK, DOUBLE_CLICK, OPEN_MENU_ITEM, …)
        # no tienen contrato en el Smart Executor todavía. Se ignoran
        # silenciosamente — la misión seguirá considerándose "semántica"
        # si al menos otro step se adaptó, y el caller decidirá.

    return out


# ─── Conversión unitaria ───────────────────────────────────────────────


def _convert_one(
    entry: Dict[str, Any],
    *,
    index: int,
    mission_id: str,
    block_id: Optional[str] = None,
) -> Tuple[Optional[MissionStep], Optional[AdaptedBlocker]]:
    """Transforma un dict ``{type, params}`` en ``MissionStep`` + blocker."""
    t = str(entry.get("type") or "").strip()
    params_in = dict(entry.get("params") or {})
    step_id = _derive_step_id(
        entry, mission_id=mission_id, index=index, kind=t
    )
    block = block_id or entry.get("_block_id")

    if t == "open_app":
        name = (
            str(params_in.get("app") or params_in.get("name") or "")
        ).strip()
        if not name:
            return None, AdaptedBlocker(
                code="unknown_kind",
                message=(
                    f"Paso #{index + 1}: open_app sin nombre de aplicación. "
                    "Revísalo antes de ejecutar."
                ),
                step_index=index,
                step_type=t,
            )
        return (
            MissionStep(
                id=step_id,
                kind="open_app",
                params={"name": name},
                block_id=block,
                human_label=f"Abrir {name}",
            ),
            None,
        )

    if t == "select_entity_from_collection":
        ent_raw = params_in.get("collection_entity") or params_in.get("entity") or {}
        label = ""
        if isinstance(ent_raw, dict):
            label = str(ent_raw.get("display_name") or "").strip()
        label = label or str(params_in.get("selection_label") or "").strip()
        needs_clar = bool(
            params_in.get("needs_clarification")
            or params_in.get("needs_user_label"),
        )
        cand = int(params_in.get("candidate_count") or 1)
        if not label or needs_clar or cand > 1:
            return None, AdaptedBlocker(
                code="collection_entity_ambiguous",
                message=(
                    f"Paso #{index + 1}: no se identificó con claridad el "
                    "elemento de la lista. Confírmalo en Mission Review."
                ),
                step_index=index,
                step_type=t,
            )
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                human_label_for_uces_step,
            )
            hl = human_label_for_uces_step(params_in)
        except Exception:
            hl = f"Seleccionar {label}"
        return (
            MissionStep(
                id=step_id,
                kind="select_entity_from_collection",
                params=dict(params_in),
                block_id=block,
                human_label=hl,
            ),
            None,
        )

    if t == "select_profile":
        profile = str(params_in.get("profile") or params_in.get("profile_name") or "").strip()
        needs_label = bool(params_in.get("needs_user_label"))
        if not profile or needs_label:
            return None, AdaptedBlocker(
                code="empty_profile",
                message=(
                    f"Paso #{index + 1}: falta el nombre del perfil de Chrome. "
                    "Confírmalo en Mission Review antes de ejecutar."
                ),
                step_index=index,
                step_type=t,
            )
        ocr_params = {}
        if str(params_in.get("strategy") or "") == "ocr_collection":
            ocr_params = {
                "strategy": "ocr_collection",
                "label_text": profile,
                "precapture_frames": list(params_in.get("precapture_frames") or []),
                "collection_candidates": list(
                    params_in.get("collection_candidates") or []
                ),
                "candidates_digest": str(params_in.get("candidates_digest") or ""),
                "bbox_hint": dict(params_in.get("bbox_hint") or {}),
            }
        return (
            MissionStep(
                id=step_id,
                kind="select_profile",
                params={
                    "profile_name": profile,
                    "app": str(params_in.get("app") or "").strip() or "chrome",
                    **ocr_params,
                },
                block_id=block,
                human_label=f"Seleccionar perfil {profile}",
            ),
            None,
        )

    if t == "open_new_tab":
        return (
            MissionStep(
                id=step_id,
                kind="open_new_tab",
                params={
                    "app": str(params_in.get("app") or "").strip() or "chrome",
                },
                block_id=block,
                human_label="Abrir nueva pestaña",
            ),
            None,
        )

    if t == "open_url":
        url = str(params_in.get("url") or "").strip()
        alias = str(
            params_in.get("alias")
            or params_in.get("name")
            or ""
        ).strip()
        if not url and not alias:
            return None, AdaptedBlocker(
                code="empty_target",
                message=(
                    f"Paso #{index + 1}: open_url sin URL ni alias. "
                    "Define el destino antes de ejecutar."
                ),
                step_index=index,
                step_type=t,
            )
        params: Dict[str, Any] = {}
        if alias:
            params["alias"] = alias
        if url:
            params["url"] = url
        return (
            MissionStep(
                id=step_id,
                kind="open_url",
                params=params,
                block_id=block,
                human_label=f"Abrir {alias or url}",
            ),
            None,
        )

    if t in ("search", "search_youtube"):
        query = str(
            params_in.get("query")
            or params_in.get("val")
            or params_in.get("text")
            or ""
        ).strip()
        site = str(params_in.get("site") or "").strip().lower()
        target = str(
            params_in.get("target")
            or params_in.get("target_label")
            or ""
        ).strip()

        if not query:
            return None, AdaptedBlocker(
                code="empty_query",
                message=(
                    f"Paso #{index + 1}: búsqueda sin texto. "
                    "Añade el término a buscar."
                ),
                step_index=index,
                step_type=t,
            )

        # Para PRD 2026-05-03d §2: si no sabemos el site, bloqueamos
        # hasta que el usuario confirme (target vacío). Destinos YouTube
        # usan ``search_content`` canónico en el ejecutor.
        if not site and t != "search_youtube":
            return None, AdaptedBlocker(
                code="empty_target",
                message=(
                    f"Paso #{index + 1}: búsqueda sin sitio definido. "
                    "Marca el sitio (ej. youtube) en Mission Review."
                ),
                step_index=index,
                step_type=t,
            )

        if site and site != "youtube":
            # Sólo tenemos contrato de YouTube — aún no podemos ejecutar
            # búsquedas arbitrarias de forma segura.
            return None, AdaptedBlocker(
                code="unknown_kind",
                message=(
                    f"Paso #{index + 1}: búsquedas en '{site}' todavía no "
                    "están soportadas por el ejecutor inteligente."
                ),
                step_index=index,
                step_type=t,
            )

        if not target:
            target = "youtube_search_box"

        return (
            MissionStep(
                id=step_id,
                kind="search_content",
                params={
                    "query": query,
                    "provider": "youtube",
                    "site": site or "youtube",
                    "target": target,
                    "preferred_strategy": "search_content:smart_route",
                },
                block_id=block,
                human_label=f"Buscar en YouTube: {query}",
            ),
            None,
        )

    # Cualquier tipo que llegue aquí no lo soporta el Smart Executor.
    return None, AdaptedBlocker(
        code="unknown_kind",
        message=(
            f"Paso #{index + 1}: tipo '{t}' no soportado por el "
            "ejecutor inteligente. Revísalo."
        ),
        step_index=index,
        step_type=t,
    )


# ─── Helpers de routing ───────────────────────────────────────────────


def is_mission_semantic(mission: Mission) -> bool:
    """¿La misión trae al menos un step compilado reconocible como
    semántico (LAUNCH_APP, SELECT_PROFILE, NAVIGATE_OR_SEARCH,
    SUBMIT_SEARCH, OPEN_NEW_TAB, OPEN_BOOKMARK, SET_FIELD_VALUE con
    submit_after)?

    Es el criterio que usa el *runner bridge* para decidir entre Smart
    Executor y MissionPlayer legacy.

    PRD 2026-05-07 §1: si la misión trae un ``semantic_execution_plan``
    válido también cuenta como semántica — el grafo legacy puede estar
    sucio (CLICKs/TYPEs crudos) pero el plan es la fuente canónica.
    Delega la validación a ``has_valid_semantic_execution_plan`` para
    que adapter, gate y bridge usen exactamente el mismo criterio.
    """
    # Import diferido para evitar ciclo (semantic_execution_plan
    # importa intent_collapse_engine, que pasa por contracts).
    try:
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        if has_valid_semantic_execution_plan(mission):
            return True
    except Exception:
        # Defensivo: si la imp falla, caemos al chequeo legacy.
        pass

    semantic_actions = {
        ActionStrategy.LAUNCH_APP,
        ActionStrategy.SELECT_PROFILE,
        ActionStrategy.OPEN_NEW_TAB,
        ActionStrategy.OPEN_BOOKMARK,
        ActionStrategy.NAVIGATE_OR_SEARCH,
        ActionStrategy.SUBMIT_SEARCH,
        # V2: Intent Compiler V2 emite estos directamente.
        ActionStrategy.WAIT_FOR_STATE,
        ActionStrategy.SCROLL_UNTIL_VISIBLE,
    }
    for cs in mission.compiled_execution_graph or []:
        if cs.action_strategy in semantic_actions:
            return True
        # SET_FIELD_VALUE/TYPE_TEXT con submit_after y site es búsqueda
        # semántica enriquecida (YouTube, Google…).
        if cs.action_strategy in (
            ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT,
        ):
            pl = cs.action_payload or {}
            if pl.get("submit_after") and (
                pl.get("site") or pl.get("search_site")
            ):
                return True
    return False


# ─── Utilidades locales ───────────────────────────────────────────────


def _looks_like_url(text: str) -> bool:
    """Heurística ligera: ¿el texto parece URL?"""
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith(("http://", "https://", "www.")):
        return True
    try:
        parsed = urlparse(t if "://" in t else f"http://{t}")
        host = parsed.netloc or parsed.path
    except Exception:
        return False
    if not host or " " in host:
        return False
    # Dominio muy típico: contiene punto y TLD corto (2-6 letras).
    if "." not in host:
        return False
    tld = host.rsplit(".", 1)[-1]
    return 2 <= len(tld) <= 6 and tld.isalpha()


def _derive_step_id(
    entry: Dict[str, Any],
    *,
    mission_id: str,
    index: int,
    kind: str,
) -> str:
    """Reusa el id fuente si existe, si no sintetiza uno estable."""
    src = str(entry.get("_source_step_id") or entry.get("id") or "").strip()
    if src:
        return src
    return f"{mission_id}:s{index:02d}:{kind}"


__all__ = [
    "AdaptedMission",
    "AdaptedBlocker",
    "adapt_mission_to_steps",
    "is_mission_semantic",
]
