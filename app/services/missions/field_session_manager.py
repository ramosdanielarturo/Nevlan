"""
Nevlan — Field Session Manager V2
==================================

Convierte secuencias de teclado/clicks en sesiones lógicas de
escritura. Su salida es la representación CANÓNICA de "el usuario
escribió X en Y, y opcionalmente confirmó con Enter".

Bug raíz que arregla
--------------------

El recorder genera ``KEYBOARD_TYPE_TEXT`` por *cada* batch del listener
(pynput emite varias veces durante la captura). Eso producía:

* "Devue" + "Devuelveme" + "Devuelveme el amor"
  → al compilar sin cuidado quedaba "DevueDevuelveme..." (concat).
* Texto antes y después de un Enter terminaba en sesiones distintas.
* Texto en Windows Search se mezclaba con texto en YouTube.

Concepto: sesión lógica
-----------------------

Una sesión es una región contigua donde:

  * Misma ventana / mismo proceso (hwnd).
  * Mismo target inferido (mismo automation_id / mismo locator
    web / misma área de la pantalla).
  * Sin click intermedio que cambie el foco.
  * Sin window_activate que cambie de app.

Dentro de una sesión, el TEXTO FINAL es el último ``KEYBOARD_TYPE_TEXT``
de la racha (que ya incluye el progreso anterior — el listener emite
acumulado).  Si vemos texto FRAGMENTADO (ej. "Devue" + "Devuélveme")
mantenemos el más largo cuando uno es prefijo del otro; en otro caso,
los CONCATENAMOS — pero solo si las longitudes lo justifican.

Output
------

``build_field_sessions(raw_trace)`` devuelve una lista de
``FieldSession`` ordenadas. Cada sesión tiene:

  * ``window`` (hwnd, title, process_name)
  * ``target_signature`` (UIA/web/visual fingerprint)
  * ``final_text``  (string compacto y único)
  * ``submitted``  (bool — había Enter en la sesión)
  * ``raw_event_ids``  (qué eventos se consumieron)
  * ``site``  (youtube/google/None — heurística contextual)

El compiler V2 NO emite múltiples TYPE_TEXT por sesión; emite UN solo
``SET_FIELD_VALUE`` (si no hubo enter) o ``SUBMIT_SEARCH`` (si lo hubo).

Reglas críticas
---------------

* **Nunca producir texto duplicado.** Si "Devue" y "Devuelveme"
  pertenecen a la misma sesión, el resultado es "Devuelveme".
* **No mezclar sesiones.** Texto en Windows Search ≠ texto en YouTube
  (distintas ventanas / distintos procesos / distinto target).
* **Enter pertenece a la sesión activa.** Si tras escribir el usuario
  pulsa Enter sin cambiar de foco, se marca ``submitted=True`` y se
  consume el evento Enter (no queda como ``SEND_HOTKEY`` huérfano).
* **Texto parcial se elimina.** Si una sesión tiene "h" + "he" + "hello",
  resultado: "hello".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import EventType, RawEvent
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Tipos
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WindowFingerprint:
    """Identidad mínima de la ventana donde ocurre la sesión."""
    hwnd: Optional[int] = None
    title: str = ""
    process_name: str = ""

    @classmethod
    def from_event(cls, ev: RawEvent) -> "WindowFingerprint":
        wc = ev.window_context
        if not wc:
            return cls()
        return cls(
            hwnd=wc.hwnd,
            title=str(wc.title or ""),
            process_name=str(wc.process_name or ""),
        )

    def matches(self, other: "WindowFingerprint") -> bool:
        """Misma ventana lógica. Preferimos hwnd; si no, mismo proceso
        y mismo título; si nada de eso, sin match."""
        if self.hwnd is not None and other.hwnd is not None:
            return self.hwnd == other.hwnd
        if self.process_name and other.process_name:
            if self.process_name != other.process_name:
                return False
            return self.title == other.title
        return False


@dataclass
class TargetFingerprint:
    """Identidad mínima del campo donde se escribe.

    Preferimos identificadores estables (automation_id, web test_id,
    css selector) sobre nombres y bbox. Sin ninguno, devolvemos un
    fingerprint "vacío" — entonces solo separamos por ventana.
    """
    automation_id: str = ""
    web_locator: str = ""
    name: str = ""
    bbox_key: str = ""

    @classmethod
    def from_event(cls, ev: RawEvent) -> "TargetFingerprint":
        meta = ev.metadata or {}
        uia = meta.get("uia_data") or meta.get("desktop_uia") or {}
        web = meta.get("web_data") or meta.get("web") or {}
        aid = str((uia or {}).get("automation_id") or "").strip()
        name = str((uia or {}).get("name") or "").strip()
        # web locator preferido: test_id > role+name > css.
        wloc = ""
        if isinstance(web, dict):
            wloc = (
                str(web.get("test_id") or "").strip()
                or str(web.get("css_selector") or "").strip()
                or str(web.get("role") or "").strip() + ":" + str(web.get("accessible_name") or "").strip()
            )
            if wloc.startswith(":"):
                wloc = ""
        bbox_key = ""
        bbox = (uia or {}).get("bbox") or (web or {}).get("bounding_rect")
        if isinstance(bbox, dict):
            # Round to 25px buckets — pequeñas variaciones siguen siendo
            # el mismo campo (cursor blink, etc.).
            try:
                x = int(bbox.get("x", 0)) // 25 * 25
                y = int(bbox.get("y", 0)) // 25 * 25
                w = int(bbox.get("w", bbox.get("width", 0))) // 25 * 25
                bbox_key = f"{x}x{y}+{w}"
            except Exception:
                pass
        return cls(automation_id=aid, web_locator=wloc, name=name, bbox_key=bbox_key)

    def is_valid(self) -> bool:
        """Tenemos información suficiente para identificar el target?"""
        return bool(
            self.automation_id or self.web_locator or self.name or self.bbox_key
        )

    def matches(self, other: "TargetFingerprint") -> bool:
        # Si ambos tienen automation_id, debe coincidir.
        if self.automation_id and other.automation_id:
            return self.automation_id == other.automation_id
        if self.web_locator and other.web_locator:
            return self.web_locator == other.web_locator
        if self.name and other.name:
            # Caso especial: name "" en algunos contenedores genéricos.
            return self.name == other.name and self.bbox_key == other.bbox_key
        if self.bbox_key and other.bbox_key:
            return self.bbox_key == other.bbox_key
        # Si uno está vacío y el otro no, NO matcheamos: preferimos
        # crear sesión nueva si hay duda.
        return not self.is_valid() and not other.is_valid()


@dataclass
class FieldSession:
    """Sesión lógica de escritura: lo que el usuario "tipó en un campo".

    Esta es la unidad que el compiler V2 promueve a:
      * ``SET_FIELD_VALUE`` (si ``submitted=False``)
      * ``SUBMIT_SEARCH`` / ``NAVIGATE_OR_SEARCH`` (si ``submitted=True``)
    """
    window: WindowFingerprint
    target: TargetFingerprint
    final_text: str = ""
    submitted: bool = False
    raw_event_ids: List[str] = field(default_factory=list)
    site: Optional[str] = None  # youtube | google | none
    field_label: str = ""
    started_at: Optional[Any] = None
    ended_at: Optional[Any] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "window": {
                "hwnd": self.window.hwnd,
                "title": self.window.title,
                "process": self.window.process_name,
            },
            "target": {
                "automation_id": self.target.automation_id,
                "web_locator": self.target.web_locator,
                "name": self.target.name,
            },
            "final_text": self.final_text,
            "submitted": self.submitted,
            "site": self.site,
            "field_label": self.field_label,
            "events_consumed": len(self.raw_event_ids),
        }


# ──────────────────────────────────────────────────────────────────────
# Heurísticas de sitio
# ──────────────────────────────────────────────────────────────────────

_SITE_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube"),
    "google": ("google.com", "google."),
    "twitter": ("twitter.com", "x.com"),
    "github": ("github.com",),
}


def _detect_site(window: WindowFingerprint, ev_meta: Dict) -> Optional[str]:
    """Detecta el sitio de una sesión a partir de la ventana o metadata."""
    title = (window.title or "").lower()
    web = (ev_meta or {}).get("web_data") or {}
    url = str(web.get("url") or "").lower() if isinstance(web, dict) else ""
    for site, hosts in _SITE_HOSTS.items():
        for h in hosts:
            if h in url or h in title:
                return site
    # Heurística adicional: Windows Search → site=None pero process=explorer
    if "explorer" in (window.process_name or "").lower() and "search" in title:
        return None  # explícito
    return None


def _looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith(("http://", "https://", "www.")):
        return True
    if "." in t and " " not in t:
        tld = t.rsplit(".", 1)[-1]
        return 2 <= len(tld) <= 6 and tld.isalpha()
    return False


# ──────────────────────────────────────────────────────────────────────
# Combinación de texto (anti-DevueDevue)
# ──────────────────────────────────────────────────────────────────────

def _combine_progressive_text(prev: str, new: str) -> str:
    """Combina dos snapshots de texto de la MISMA sesión.

    Reglas:
      1. Si uno es prefijo del otro (case-sensitive), gana el más largo.
      2. **Corrección case-insensitive / por acento** (PRD 2026-05-10j):
         si uno es prefijo del otro ignorando case y diacríticos, gana
         la versión MÁS LARGA y MEJOR formada (con acentos / case
         original). Esto evita los falsos "chr Chrome" (case shift) y
         "Devuelveme Devuélveme..." (acentos añadidos).
      3. Si ``new`` ya contiene ``prev`` como subsecuencia inicial extendida
         (autocompletes / typing IME), gana ``new``.
      4. Si son completamente distintos pero la longitud crece (escritura
         legítima), concatenamos con espacio si ``prev`` no termina en
         espacio — esto cubre el caso raro "Hola" + "mundo" del listener
         emitiendo dos batches. NO concatenamos si ``new`` es muy corto
         (probablemente eco del listener, no escritura adicional).
      5. Si ``new`` es claramente "escritura distinta" (longitud similar
         o menor), gana ``new`` (último wins) — el usuario corrigió.
    """
    p = prev or ""
    n = new or ""
    if not p:
        return n
    if not n:
        return p
    # 1) prefijo exacto
    if n.startswith(p):
        return n
    if p.startswith(n):
        return p
    # 2) prefijo normalizado (case + acentos): "chr" → "Chrome",
    # "Devuelveme" → "Devuélveme el amor", "hola" → "HOLA mundo".
    p_norm = _normalize_for_prefix(p)
    n_norm = _normalize_for_prefix(n)
    if p_norm and n_norm:
        if n_norm.startswith(p_norm):
            # New incluye lo escrito antes, mejor formado.
            return n
        if p_norm.startswith(n_norm):
            # Prev ya era el texto completo; el nuevo es un eco corto.
            return p
    # 3) IME / autocomplete: misma raíz pero divergencias menores.
    common = _common_prefix_len(p, n)
    if common >= max(3, int(len(p) * 0.6)):
        # Mantenemos el más largo (último o el más completo).
        return n if len(n) >= len(p) else p
    # 4) Concatenación cuidadosa: solo si new es lo bastante largo.
    if len(n) >= 3 and len(p) >= 3 and len(n) + len(p) >= 8:
        sep = "" if p.endswith(" ") else " "
        return p + sep + n
    # 5) Wins el último (corrección).
    return n


# Mapa mínimo de fold (no usamos unicodedata para conservar 100% pure-py
# y para que el comportamiento sea predecible bajo edge cases). Solo
# diacríticos latinos comunes: cualquier idioma que use acentos en
# español, portugués, francés, alemán o italiano queda cubierto.
_DIACRITIC_FOLD = str.maketrans({
    "á": "a", "à": "a", "ä": "a", "â": "a", "ã": "a", "å": "a",
    "Á": "A", "À": "A", "Ä": "A", "Â": "A", "Ã": "A", "Å": "A",
    "é": "e", "è": "e", "ë": "e", "ê": "e",
    "É": "E", "È": "E", "Ë": "E", "Ê": "E",
    "í": "i", "ì": "i", "ï": "i", "î": "i",
    "Í": "I", "Ì": "I", "Ï": "I", "Î": "I",
    "ó": "o", "ò": "o", "ö": "o", "ô": "o", "õ": "o",
    "Ó": "O", "Ò": "O", "Ö": "O", "Ô": "O", "Õ": "O",
    "ú": "u", "ù": "u", "ü": "u", "û": "u",
    "Ú": "U", "Ù": "U", "Ü": "U", "Û": "U",
    "ñ": "n", "Ñ": "N", "ç": "c", "Ç": "C",
})


def _normalize_for_prefix(s: str) -> str:
    """Versión lowercase + sin acentos, para comparar prefijos."""
    if not s:
        return ""
    return s.translate(_DIACRITIC_FOLD).lower()


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


# ──────────────────────────────────────────────────────────────────────
# Estado del session builder
# ──────────────────────────────────────────────────────────────────────

class _SessionBuilder:
    """State machine sobre el raw_trace que produce sesiones."""

    def __init__(self) -> None:
        self.sessions: List[FieldSession] = []
        self._cur: Optional[FieldSession] = None
        self._cur_window: Optional[WindowFingerprint] = None
        self._cur_target: Optional[TargetFingerprint] = None

    def _close_current(self) -> None:
        if self._cur is not None and self._cur.final_text:
            self.sessions.append(self._cur)
        self._cur = None
        self._cur_target = None

    def _start_new(self, ev: RawEvent) -> None:
        win = WindowFingerprint.from_event(ev)
        tgt = TargetFingerprint.from_event(ev)
        site = _detect_site(win, ev.metadata or {})
        self._cur = FieldSession(
            window=win, target=tgt, site=site,
            started_at=ev.timestamp,
        )
        self._cur_window = win
        self._cur_target = tgt

    def _is_same_session(self, ev: RawEvent) -> bool:
        if self._cur is None:
            return False
        win = WindowFingerprint.from_event(ev)
        tgt = TargetFingerprint.from_event(ev)
        if not win.matches(self._cur_window or WindowFingerprint()):
            return False
        # Si el target del nuevo evento NO matchea, asumimos sesión nueva
        # (el usuario cambió de campo dentro de la misma ventana).
        # Excepción: si el evento es teclado puro (no click), heredamos
        # el target de la sesión actual.
        if ev.event_type in (
            EventType.KEYBOARD_TYPE_TEXT, EventType.KEYBOARD_KEY_PRESS
        ):
            return True
        return tgt.matches(self._cur_target or TargetFingerprint())

    def feed(self, ev: RawEvent) -> None:
        # 1) WINDOW_ACTIVATE → cierra sesión si cambia de ventana.
        if ev.event_type == EventType.WINDOW_ACTIVATE:
            new_win = WindowFingerprint.from_event(ev)
            if self._cur and not new_win.matches(self._cur_window or WindowFingerprint()):
                self._close_current()
            return

        # 2) Click de mouse → cierra sesión si cambia de target.
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
            EventType.MOUSE_RIGHT_CLICK,
        ):
            if self._cur is not None and not self._is_same_session(ev):
                self._close_current()
            return

        # 3) KEYBOARD_TYPE_TEXT → alimenta o crea sesión.
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            text = "".join(ev.keyboard_action.keys) if ev.keyboard_action else ""
            if not text:
                return
            if self._cur is None or not self._is_same_session(ev):
                if self._cur is not None:
                    self._close_current()
                self._start_new(ev)
            assert self._cur is not None
            self._cur.final_text = _combine_progressive_text(
                self._cur.final_text, text
            )
            self._cur.raw_event_ids.append(ev.id)
            self._cur.ended_at = ev.timestamp
            # Si no teníamos site, intentar deducirlo del evento
            # (puede traer web_data más tarde aunque la ventana inicial
            # no lo tuviera).
            if not self._cur.site:
                self._cur.site = _detect_site(
                    self._cur.window, ev.metadata or {}
                )
            return

        # 4) KEYBOARD_HOTKEY/KEY_PRESS Enter → submit de sesión actual.
        if ev.event_type in (
            EventType.KEYBOARD_HOTKEY, EventType.KEYBOARD_KEY_PRESS
        ):
            keys = []
            if ev.keyboard_action:
                keys = [str(k).lower() for k in (ev.keyboard_action.keys or [])]
                mods = [str(m).lower() for m in (ev.keyboard_action.modifiers or [])]
            else:
                mods = []
            is_enter = (keys == ["enter"] and not mods) or keys == ["return"]
            if is_enter and self._cur is not None and self._cur.final_text:
                if self._is_same_session(ev):
                    self._cur.submitted = True
                    self._cur.raw_event_ids.append(ev.id)
                    self._cur.ended_at = ev.timestamp
                    self._close_current()
                    return
            # Otros hotkeys (Ctrl+L, Ctrl+T, Alt+F4) cierran la sesión
            # sin marcar submit — probablemente cambian de contexto.
            if self._cur is not None and (mods or keys):
                self._close_current()
            return

    def finalize(self) -> List[FieldSession]:
        self._close_current()
        return self.sessions


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def build_field_sessions(raw_trace: List[RawEvent]) -> List[FieldSession]:
    """Recorre el ``raw_trace`` y agrupa los eventos de teclado en
    sesiones lógicas. Devuelve solo sesiones con texto NO vacío.

    El caller (``intent_compiler_v2``) usa estas sesiones para emitir
    los pasos semánticos correspondientes (``fill_field`` /
    ``search``) sin pasar por la heurística clásica del compiler.
    """
    if not raw_trace:
        return []
    builder = _SessionBuilder()
    for ev in raw_trace:
        builder.feed(ev)
    sessions = builder.finalize()

    # Etiqueta semántica de cada sesión (heurística): YouTube/Windows
    # Search/Chrome address bar. La etiqueta vive en ``field_label``
    # y se usa para las descripciones del Mission Review.
    for s in sessions:
        s.field_label = _label_session(s)
    log.info(
        f"[field_session_v2] sesiones detectadas={len(sessions)}, "
        f"submitted={sum(1 for s in sessions if s.submitted)}"
    )
    return sessions


def _label_session(s: FieldSession) -> str:
    title = (s.window.title or "").lower()
    proc = (s.window.process_name or "").lower()
    if "explorer" in proc and "search" in title:
        return "windows_search"
    if "chrome" in proc or "edge" in proc or "brave" in proc:
        if s.site == "youtube":
            return "youtube_search_box"
        if _looks_like_url(s.final_text):
            return "browser_address_bar"
        return "browser_search_box"
    return s.target.name or "input"


def event_ids_consumed(sessions: List[FieldSession]) -> set:
    """Devuelve el conjunto de ``raw_event_ids`` consumidos por las
    sesiones. Los callers lo usan para decidir qué eventos NO emitir
    como pasos primitivos (porque ya están representados como sesión).
    """
    consumed = set()
    for s in sessions:
        consumed.update(s.raw_event_ids)
    return consumed


__all__ = [
    "FieldSession",
    "WindowFingerprint",
    "TargetFingerprint",
    "build_field_sessions",
    "event_ids_consumed",
]
