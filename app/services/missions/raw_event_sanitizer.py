"""
Nevlan — Raw Event Sanitizer (V2)
==================================

Primera barrera de calidad sobre el ``raw_trace``: ANTES de que el
compiler vea los eventos, este módulo elimina ruido que jamás debería
entrar al pipeline semántico.

Filosofía
---------

    "Evento débil + contexto débil = eliminar."

El compiler V3 hace muchas cosas bien (sesiones de campo, fusión
semántica, captura visual…) pero está sobrecargado: parchea eventos
basura caso por caso. Resultado: misiones inflables con clicks sobre
``guide-service``/``main`` que rompen la ejecución.

Este sanitizer corre como Pass 0 (antes de ``_resolve_text_sessions``)
y aplica reglas estrictas de descarte:

* CapsLock accidental — keystrokes con shift "atascado" que producen
  texto en MAYÚSCULAS no intencional.
* Clicks duplicados — dos ``MOUSE_CLICK`` con la misma coord/ventana
  dentro de 120 ms (típico de doble-click humano que ya capturó
  ``MOUSE_DOUBLE_CLICK``).
* Clicks en contenedores genéricos — ``GroupControl`` / ``PaneControl``
  vacíos, name='guide-service' / 'main' / 'click_fallback'.
* Scrolls repetidos — N scrolls consecutivos en la misma ventana se
  colapsan en uno solo con ``count`` agregado.
* Texto fragmentado — múltiples ``KEYBOARD_TYPE_TEXT`` que terminan
  pisándose: conservamos el ÚLTIMO de la sesión más completo.
* Eventos redundantes — ``WINDOW_ACTIVATE`` duplicado, ``MOUSE_MOVE``
  pre-click (no debería entrar pero por si acaso).

NO hace
-------

* No fusiona texto + Enter (lo hace ``field_session_manager``).
* No reescribe targets (lo hace ``invalid_target_resolver``).
* No promueve a semantic step (lo hace ``intent_compiler_v2``).

API
---

>>> trace = sanitize_raw_trace(mission.raw_trace)
>>> # o, in-place sobre la misión:
>>> sanitize_mission_in_place(mission)

Cada paso devuelve un ``SanitizeReport`` con contadores para telemetría
(útil para descubrir patrones de ruido y mejorar los listeners).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from app.contracts.mission import EventType, Mission, RawEvent
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Configuración (todas en módulo para test fácil)
# ──────────────────────────────────────────────────────────────────────

# Ventana de duplicación para clicks: dos clicks dentro de este intervalo
# en la misma coordenada (igualdad exacta) se consideran duplicado del
# mismo gesto del listener pynput. Ajustado a 80ms (más estricto que
# el reflejo humano de doble-click típico de ~250ms): solo capturamos
# ecos del listener, no clicks rápidos legítimos.
DUP_CLICK_WINDOW_MS = 80

# Tolerancia espacial para "mismo punto" (en px). Estricto (=0) para
# evitar falsos positivos con clicks legítimos cercanos (e.g. dos
# botones adyacentes en una toolbar).
DUP_CLICK_PIXEL_TOL = 0

# Ventana de coalescencia de scrolls (ms): scrolls consecutivos en la
# misma ventana / mismo eje dentro de este intervalo se colapsan.
SCROLL_COALESCE_WINDOW_MS = 350

# Nombres UIA "técnicos" / contenedores que JAMÁS son targets
# legítimos. Si el evento llega con uno de estos como target principal,
# lo descartamos siempre que NO traiga semántica de rescate (texto
# cercano fuerte, image_ref, web_data util).
INVALID_TARGET_NAMES = frozenset({
    "",
    "guide-service",
    "main",
    "click_fallback",
    "groupcontrol",
    "panecontrol",
    "documentcontrol",
    "windowcontrol",
    "customcontrol",
})

INVALID_AUTOMATION_IDS = frozenset({
    "",
    "guide-service",
    "main",
    "click_fallback",
})


# ──────────────────────────────────────────────────────────────────────
# Reporte de telemetría
# ──────────────────────────────────────────────────────────────────────

@dataclass
class SanitizeReport:
    """Contadores de cada categoría de evento descartado o fusionado."""
    input_count: int = 0
    output_count: int = 0
    dropped_dup_clicks: int = 0
    dropped_invalid_target: int = 0
    coalesced_scrolls: int = 0
    dropped_capslock_artifacts: int = 0
    dropped_dup_window_activate: int = 0
    text_fragments_collapsed: int = 0

    @property
    def removed(self) -> int:
        return max(0, self.input_count - self.output_count)

    def as_dict(self) -> Dict[str, int]:
        return {
            "input": self.input_count,
            "output": self.output_count,
            "removed": self.removed,
            "dup_clicks": self.dropped_dup_clicks,
            "invalid_targets": self.dropped_invalid_target,
            "coalesced_scrolls": self.coalesced_scrolls,
            "capslock_artifacts": self.dropped_capslock_artifacts,
            "dup_window_activate": self.dropped_dup_window_activate,
            "text_fragments": self.text_fragments_collapsed,
        }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _ts_diff_ms(a: RawEvent, b: RawEvent) -> int:
    """Diferencia en ms entre dos eventos (b-a). Devuelve 0 si falta ts."""
    if not a.timestamp or not b.timestamp:
        return 0
    delta: timedelta = b.timestamp - a.timestamp
    return int(delta.total_seconds() * 1000.0)


def _click_xy(ev: RawEvent) -> Optional[Tuple[int, int]]:
    if ev.event_type not in (
        EventType.MOUSE_CLICK,
        EventType.MOUSE_DOUBLE_CLICK,
        EventType.MOUSE_RIGHT_CLICK,
    ):
        return None
    if not ev.mouse_action:
        return None
    return (int(ev.mouse_action.x), int(ev.mouse_action.y))


def _is_invalid_uia_target(meta: Dict) -> bool:
    """Devuelve ``True`` si la metadata UIA del evento apunta a un
    contenedor genérico/técnico SIN señal semántica de rescate.

    REGLAS CONSERVADORAS:
      * Solo descartamos cuando HAY evidencia POSITIVA de target malo
        (name explícito en blacklist, o control_type genérico explícito).
      * Eventos SIN metadata UIA poblada → válidos por defecto (la
        captura puede haber fallado silenciosamente; rechazarlos rompe
        misiones legacy y tests sintéticos).
      * Cualquier rescate (web_data, OCR fuerte, no-click) → válido.
    """
    if not meta:
        return False

    # Rescate 1: el evento es no-click → target irrelevante.
    if meta.get("event_kind") in {"keyboard", "hotkey", "scroll", "drag"}:
        return False

    # Rescate 2: web_data con locators → es un target web válido.
    web = meta.get("web_data") or meta.get("web") or {}
    if isinstance(web, dict) and (web.get("locators") or web.get("css_selector")):
        return False

    # Rescate 3: vision/OCR cercano fuerte.
    vis = meta.get("vision_data") or {}
    if isinstance(vis, dict):
        ocr_text = str(vis.get("ocr_text_around") or "").strip()
        anchor = str(vis.get("nearest_text_anchor") or "").strip()
        if len(ocr_text) >= 3 or len(anchor) >= 3:
            return False

    uia = meta.get("uia_data") or meta.get("desktop_uia") or {}
    if not isinstance(uia, dict) or not uia:
        # Sin metadata UIA: NO sabemos si es target inválido. Default
        # conservador → válido. Esto preserva eventos legacy y tests
        # sintéticos que no rellenan uia_data.
        return False

    name = str(uia.get("name") or "").strip().lower()
    aid = str(uia.get("automation_id") or "").strip().lower()
    ct = str(uia.get("control_type") or "").strip().lower()

    # Caso 1: name o automation_id explícitamente en blacklist (no
    # vacío). Los strings vacíos NO disparan descarte aquí — eso
    # rechazaría eventos legítimos sin captura completa.
    explicit_bad_names = {"guide-service", "main", "click_fallback"}
    explicit_bad_aids = {"guide-service", "main", "click_fallback"}
    if name in explicit_bad_names:
        return True
    if aid and aid in explicit_bad_aids:
        return True

    # Caso 2: control_type genérico + sin nombre + sin automation_id.
    # Esto es la firma clásica de un GroupControl/PaneControl vacío.
    if ct in {"groupcontrol", "panecontrol", "customcontrol"} and not name and not aid:
        return True

    return False


def _is_capslock_artifact(ev: RawEvent) -> bool:
    """Detecta keystrokes que probablemente son ruido de CapsLock.

    Heurística defensiva — solo descartamos cuando estamos MUY seguros:
    metadata explícita ``capslock_artifact=True`` puesta por el listener.
    NO hacemos detección estadística aquí (sería incorrecta para un
    usuario que escribe legítimamente en MAYÚSCULAS).
    """
    if ev.event_type != EventType.KEYBOARD_KEY_PRESS:
        return False
    return bool(ev.metadata.get("capslock_artifact"))


# ──────────────────────────────────────────────────────────────────────
# Pasadas de sanitización
# ──────────────────────────────────────────────────────────────────────

def _drop_dup_window_activates(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Elimina ``WINDOW_ACTIVATE`` consecutivos sobre el mismo hwnd."""
    out: List[RawEvent] = []
    last_hwnd: Optional[int] = None
    for ev in events:
        if ev.event_type == EventType.WINDOW_ACTIVATE:
            hwnd = ev.window_context.hwnd if ev.window_context else None
            if hwnd is not None and hwnd == last_hwnd:
                rep.dropped_dup_window_activate += 1
                continue
            last_hwnd = hwnd
        else:
            # Cualquier otro evento "rompe" la racha de activates.
            last_hwnd = None
        out.append(ev)
    return out


def _uia_id_of(ev: RawEvent) -> str:
    """Identidad UIA del evento (automation_id+name+control_type)
    como string. Útil para comparar si dos clicks apuntan al MISMO
    elemento o solo cayeron en pixels cercanos."""
    meta = ev.metadata or {}
    uia = meta.get("uia_data") or meta.get("uia") or meta.get("desktop_uia") or {}
    if not isinstance(uia, dict):
        return ""
    return "|".join([
        str(uia.get("automation_id") or ""),
        str(uia.get("name") or ""),
        str(uia.get("control_type") or ""),
    ])


def _drop_dup_clicks(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Drop de clicks duplicados: dos clicks en la coord EXACTA dentro
    de DUP_CLICK_WINDOW_MS Y apuntando al MISMO elemento UIA se
    consideran un único gesto (eco del listener pynput).

    Reglas estrictas para evitar falsos positivos:
      * Coordenadas IGUALES (sin tolerancia píxel — evita perder
        clicks adyacentes legítimos).
      * UIA target equivalente (automation_id+name+ctrl_type) — si los
        UIA son distintos, NO son duplicados aunque las coords coincidan
        (raro pero posible en webapps con overlays).

    Casos:
      * second = DOUBLE_CLICK + first = CLICK → drop del simple.
      * ambos simples → drop del segundo.
    """
    out: List[RawEvent] = []
    for ev in events:
        if not out:
            out.append(ev)
            continue
        prev = out[-1]
        prev_xy = _click_xy(prev)
        cur_xy = _click_xy(ev)
        if prev_xy and cur_xy:
            same_xy = (prev_xy == cur_xy) if DUP_CLICK_PIXEL_TOL == 0 else (
                abs(prev_xy[0] - cur_xy[0]) <= DUP_CLICK_PIXEL_TOL
                and abs(prev_xy[1] - cur_xy[1]) <= DUP_CLICK_PIXEL_TOL
            )
            same_target = _uia_id_of(prev) == _uia_id_of(ev)
            if same_xy and same_target:
                dt = _ts_diff_ms(prev, ev)
                if 0 <= dt <= DUP_CLICK_WINDOW_MS:
                    # Caso 1: el segundo es double-click → drop del simple.
                    if ev.event_type == EventType.MOUSE_DOUBLE_CLICK \
                            and prev.event_type == EventType.MOUSE_CLICK:
                        out[-1] = ev
                        rep.dropped_dup_clicks += 1
                        continue
                    # Caso 2: ambos simples → drop del segundo.
                    if ev.event_type == EventType.MOUSE_CLICK \
                            and prev.event_type == EventType.MOUSE_CLICK:
                        rep.dropped_dup_clicks += 1
                        continue
        out.append(ev)
    return out


def _drop_invalid_targets(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Descarta clicks sobre contenedores genéricos sin valor semántico.

    Implementa la regla "No guardar clicks sin valor semántico" del
    spec. Si el contexto del evento incluye señales de rescate (web,
    OCR, vision_data) NO descartamos — esos clicks SÍ tienen
    información que ``invalid_target_resolver`` puede aprovechar.
    """
    out: List[RawEvent] = []
    for ev in events:
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
            EventType.MOUSE_RIGHT_CLICK,
        ):
            if _is_invalid_uia_target(ev.metadata or {}):
                rep.dropped_invalid_target += 1
                log.debug(
                    "[sanitizer] drop click sobre target inválido "
                    f"name={(ev.metadata.get('uia_data') or {}).get('name')!r}"
                )
                continue
        out.append(ev)
    return out


def _coalesce_scrolls(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Colapsa scrolls consecutivos en la misma ventana/eje.

    Mantenemos el PRIMER scroll de la racha y aumentamos su ``count``
    en metadata. El compiler downstream interpreta ``count`` como
    cantidad de "rondas" de scroll a reproducir (en el resolver
    semántico V2 esto será reemplazado por ``scroll_until_visible``).
    """
    out: List[RawEvent] = []
    for ev in events:
        if ev.event_type != EventType.MOUSE_SCROLL:
            out.append(ev)
            continue
        if not out or out[-1].event_type != EventType.MOUSE_SCROLL:
            ev.metadata = {**(ev.metadata or {}), "count": 1}
            out.append(ev)
            continue
        prev = out[-1]
        # Misma dirección y ventana, dentro del intervalo.
        same_window = bool(
            ev.window_context and prev.window_context and
            ev.window_context.hwnd == prev.window_context.hwnd
        )
        prev_dy = (prev.metadata or {}).get("dy", 0)
        cur_dy = (ev.metadata or {}).get("dy", 0)
        same_dir = (prev_dy >= 0 and cur_dy >= 0) or (prev_dy < 0 and cur_dy < 0)
        dt = _ts_diff_ms(prev, ev)
        if same_window and same_dir and 0 <= dt <= SCROLL_COALESCE_WINDOW_MS:
            prev.metadata = {
                **(prev.metadata or {}),
                "count": int((prev.metadata or {}).get("count", 1)) + 1,
                "total_dy": int((prev.metadata or {}).get("total_dy", prev_dy)) + cur_dy,
            }
            rep.coalesced_scrolls += 1
            continue
        ev.metadata = {**(ev.metadata or {}), "count": 1}
        out.append(ev)
    return out


def _drop_capslock_artifacts(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Eventos de teclado marcados explícitamente como ruido de CapsLock."""
    out: List[RawEvent] = []
    for ev in events:
        if _is_capslock_artifact(ev):
            rep.dropped_capslock_artifacts += 1
            continue
        out.append(ev)
    return out


def _same_window(a: RawEvent, b: RawEvent) -> bool:
    """¿Dos eventos están en la MISMA ventana lógica?

    Comparamos por:
      1. hwnd si ambos lo tienen (canónico).
      2. (process_name, title) si no hay hwnd (fallback robusto).
      3. Si nada de eso, NO asumimos que son la misma ventana — más
         seguro tratarlas como distintas para no fusionar fragmentos
         entre apps.
    """
    wa = a.window_context
    wb = b.window_context
    if not wa or not wb:
        return False
    if wa.hwnd is not None and wb.hwnd is not None:
        return wa.hwnd == wb.hwnd
    return bool(
        wa.process_name and wa.process_name == wb.process_name
        and (wa.title or "") == (wb.title or "")
    )


def _collapse_text_fragments_within_session(
    events: List[RawEvent], rep: SanitizeReport
) -> List[RawEvent]:
    """Colapsa ``KEYBOARD_TYPE_TEXT`` consecutivos en la MISMA sesión
    lógica (misma ventana, sin click intermedio). Conserva el último
    (más completo) — esto resuelve el bug "DevueDevue" en su raíz.

    NOTA: la fusión semántica completa (texto + Enter, sesiones
    múltiples, ventana cambiante) la hace ``field_session_manager``;
    este pase es solo de eliminación grosera de duplicados pre-compiler.
    """
    out: List[RawEvent] = []
    cursor: Optional[RawEvent] = None
    for ev in events:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            if cursor is None:
                cursor = ev
                out.append(cursor)
                continue
            # Misma sesión: misma ventana lógica, sin click intermedio.
            if _same_window(cursor, ev):
                # Si el nuevo texto contiene al anterior (escritura
                # progresiva), el segundo gana.
                prev_text = "".join(cursor.keyboard_action.keys) if cursor.keyboard_action else ""
                new_text = "".join(ev.keyboard_action.keys) if ev.keyboard_action else ""
                if prev_text and new_text:
                    if new_text.startswith(prev_text) and len(new_text) >= len(prev_text):
                        out[out.index(cursor)] = ev
                        cursor = ev
                        rep.text_fragments_collapsed += 1
                        continue
                    if prev_text.startswith(new_text) and len(prev_text) > len(new_text):
                        rep.text_fragments_collapsed += 1
                        continue
            # Sesión diferente o textos no compatibles: nuevo cursor.
            cursor = ev
            out.append(ev)
            continue
        # Cualquier evento que NO sea typing rompe la sesión.
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
            EventType.MOUSE_RIGHT_CLICK,
            EventType.WINDOW_ACTIVATE,
        ):
            cursor = None
        out.append(ev)
    return out


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def sanitize_raw_trace(
    events: List[RawEvent],
) -> Tuple[List[RawEvent], SanitizeReport]:
    """Aplica todas las pasadas de sanitización al trace.

    Devuelve ``(eventos_limpios, reporte)``. El orden de ejecución es:

      1. Drop CapsLock artifacts (limpia teclado primero).
      2. Drop window_activate duplicados.
      3. Drop clicks sobre target inválido.
      4. Drop clicks duplicados.
      5. Coalesce scrolls.
      6. Collapse text fragments dentro de la misma sesión.

    El orden importa: queremos eliminar ruido grosero antes de mirar
    relaciones temporales, y los text fragments al final porque ya no
    quedan clicks duplicados que rompan sesiones espuriamente.
    """
    rep = SanitizeReport(input_count=len(events))
    if not events:
        return [], rep

    # Pasadas en orden.
    work = list(events)
    work = _drop_capslock_artifacts(work, rep)
    work = _drop_dup_window_activates(work, rep)
    work = _drop_invalid_targets(work, rep)
    work = _drop_dup_clicks(work, rep)
    work = _coalesce_scrolls(work, rep)
    work = _collapse_text_fragments_within_session(work, rep)

    rep.output_count = len(work)
    return work, rep


def sanitize_mission_in_place(mission: Mission) -> SanitizeReport:
    """Atajo: sanitiza el ``raw_trace`` de la misión in-place y devuelve
    el reporte. No toca ``compiled_execution_graph`` ni
    ``interpreted_steps`` — es responsabilidad del compiler recompilar.
    """
    if not mission or not mission.raw_trace:
        return SanitizeReport()
    cleaned, rep = sanitize_raw_trace(mission.raw_trace)
    mission.raw_trace = cleaned
    log.info(f"[sanitizer] {rep.as_dict()}")
    return rep


__all__ = [
    "SanitizeReport",
    "sanitize_raw_trace",
    "sanitize_mission_in_place",
    "DUP_CLICK_WINDOW_MS",
    "DUP_CLICK_PIXEL_TOL",
    "SCROLL_COALESCE_WINDOW_MS",
    "INVALID_TARGET_NAMES",
    "INVALID_AUTOMATION_IDS",
]
