"""
Nevlan — Profile Resolver (PRD 2026-05-07c)
============================================

Resuelve el ``profile_name`` REAL de un ``select_profile`` en el plan
semántico, evitando bloquear la misión con ``profile_name=""`` cuando
hay evidencia recuperable. Es la pieza que permite que Nevlan cumpla
la promesa "grabar una vez, ejecutar siempre" para el flujo Chrome
profile picker, donde la captura UIA suele ser basura
(``GroupControl``, ``video-stream``, ``html5-main-video``, name vacío)
pero el contexto humano y la metadata del browser SÍ contienen el
nombre real.

Filosofía
---------

    Nevlan no debe ejecutar lo que grabó.
    Nevlan debe ejecutar lo que entendió.

El ``select_profile`` con label genérico/vacío JAMÁS puede aprobarse,
pero tampoco puede pedir regrabar. La única herramienta válida es:

  1. autorrepar usando evidencia (texto humano, OCR, alias, metadata
     local del browser) — sin tocar al usuario.
  2. en último caso, una confirmación mínima en Mission Review que
     se guarda como **alias permanente** para futuras misiones.

Capacidades públicas
~~~~~~~~~~~~~~~~~~~~

* :func:`extract_canonical_profile_name` — extrae el nombre real
  ("Daniel Arturo Ramos") de una etiqueta sucia ("Daniel Chrome D
  Daniel Arturo Ramos", "Sign in as Maria Lopez", etc.).
* :func:`generate_aliases` — produce el conjunto de aliases que
  asociamos a un nombre canónico ("Daniel", "Daniel Arturo",
  "Daniel Arturo Ramos", "Daniel Chrome", "Daniel Ramos", …).
* :func:`learn_profile_alias` — persiste alias → canonical en disco
  (``var/profile_aliases.json``).
* :func:`lookup_profile_by_alias` — devuelve el canonical asociado
  a una observación ya vista.
* :func:`resolve_profile` — pipeline completo: prueba todas las
  fuentes en orden y devuelve un :class:`ProfileResolution` con la
  decisión + razón + needs_user_confirmation.

Diseño deliberado
~~~~~~~~~~~~~~~~~

* **Pure-Python.** Sin dependencias de UI / Qt; totalmente testeable
  sin display.
* **Idempotente.** Llamarlo varias veces sobre la misma misión
  produce el mismo resultado.
* **Local-first.** El alias store vive en disco como JSON simple; el
  caller (UI) puede sincronizarlo si quiere, pero el resolver no
  necesita red.
* **Side-effect-free por defecto.** ``resolve_profile`` NO escribe
  al store; eso lo hace ``learn_profile_alias`` cuando el usuario
  confirma.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.config import settings
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

# Tokens que NUNCA forman parte de un nombre de perfil. Los filtramos
# antes de extraer el nombre canónico (vienen del label del browser:
# "Daniel Chrome D Daniel Arturo Ramos" — el "Chrome" es el browser,
# la "D" es el avatar inicial).
_APP_NAME_TOKENS: frozenset = frozenset({
    "chrome", "edge", "brave", "opera", "firefox",
    "google", "microsoft", "msedge", "safari",
    "browser", "navegador", "navigator",
    "perfil", "perfiles", "profile", "profiles",
    "default", "user", "users", "usuario", "usuarios",
    "guest", "invitado", "invitada",
    "trabajo", "personal", "work", "home",
})

# Nombres de SITIOS web — un perfil JAMÁS se llama así (a menos que el
# usuario realmente se llame "YouTube", lo cual asumimos imposible).
# Los rechazamos como single-token para evitar falsos positivos
# cuando el window_title del browser cambia tras seleccionar perfil
# (ej. "YouTube - Google Chrome" no es un nombre de perfil).
_SITE_NAME_TOKENS: frozenset = frozenset({
    "youtube", "google", "facebook", "twitter", "instagram",
    "github", "gmail", "drive", "outlook", "linkedin",
    "reddit", "tiktok", "spotify", "netflix", "amazon",
    "x", "x.com",
    "nueva", "pestaña", "tab", "new",
})

# Conectores en cualquier idioma — si aparecen como tokens en
# minúsculas dentro de una secuencia, terminan la corrida (no son
# parte de un nombre propio).
_CONNECTOR_TOKENS: frozenset = frozenset({
    "de", "del", "la", "el", "los", "las", "y", "para", "por",
    "en", "con", "sin", "un", "una", "unos", "unas",
    "of", "the", "and", "for", "with", "to", "in", "on", "at",
    "is", "are", "vs", "vs.",
})

# Generic labels que NO son nombres reales.
_GENERIC_LABELS: frozenset = frozenset({
    "perfil del navegador",
    "perfil del browser",
    "perfil de chrome",
    "perfil de edge",
    "el perfil",
    "perfil",
    "profile",
    "browser profile",
    "chrome profile",
    "default",
    "unknown",
    "user",
    "usuario",
    # Labels de UI del propio Chrome profile picker — NO son
    # nombres reales del usuario, son CTAs.
    "seleccionar perfil",
    "selecciona un perfil",
    "selecciona perfil",
    "select profile",
    "select a profile",
    "elige un perfil",
    "elegir perfil",
    "agregar perfil",
    "add profile",
    "agregar persona",
    "add person",
    "nuevo perfil",
    "new profile",
})

# Tokens que provienen de la torre de UIA (control types, automation
# ids genéricos) o de targets web/Polymer técnicos. Como TIPOS UIA
# pueden venir en CamelCase y "parecer" nombres propios; los
# rechazamos explícitamente para evitar producir
# ``profile = "GroupControl"``.
_UIA_TECHNICAL_TOKENS: frozenset = frozenset({
    "groupcontrol", "panecontrol", "documentcontrol", "buttoncontrol",
    "editcontrol", "listitemcontrol", "treeitemcontrol",
    "windowcontrol", "menuitemcontrol", "tabcontrol", "tabitemcontrol",
    "groupbox", "spinnercontrol", "imagecontrol", "textcontrol",
    "listcontrol", "headercontrol", "headeritemcontrol",
    "guideservice", "guide-service",
    "videostream", "video-stream",
    "html5mainvideo", "html5-main-video",
    "mainpane", "mainframe", "mainwindow",
    "clickfallback", "click-fallback",
    # Polymer / YouTube DOM (PRD 2026-05-08d §A): los recorders a
    # veces capturan UIA del CONTENIDO posterior al click del
    # profile picker (porque el picker se cierra demasiado rápido)
    # y terminamos viendo cosas como ``rendering-content`` o
    # ``style-scope ytd-in-feed-ad-layout-renderer``. JAMÁS son
    # evidencia válida de un perfil de Chrome.
    "renderingcontent", "rendering-content",
    "stylescope", "style-scope",
    "ytdapp", "ytd-app",
    "ytdmainpage", "ytd-main-page",
    "ytdfeedadlayoutrenderer", "ytd-in-feed-ad-layout-renderer",
    "ytdrichgridrenderer", "ytd-rich-grid-renderer",
})

# Substrings (case-insensitive) que NUNCA deben aparecer dentro del
# texto que tomamos como evidencia de perfil. Si la observación
# contiene cualquiera de estos fragmentos, sabemos que viene del
# CONTENIDO web posterior al click (YouTube, Polymer, frames del
# DOM Polymer) y NO del profile picker; la rechazamos completa antes
# de intentar extraer un canonical.
#
# PRD 2026-05-08d §A.2: "Si el UIA del supuesto profile step trae
# algo como ytd-in-feed-ad-layout-renderer, guide-service,
# rendering-content, contenido web posterior → NO debe tomarse
# como evidencia del perfil."
#
# IMPORTANTE: NO incluimos "- google chrome" / "- chrome" /
# "google chrome" porque esos son sufijos LEGÍTIMOS del window
# title del browser ("Daniel Arturo Ramos - Google Chrome") y el
# extractor canónico ya sabe limpiarlos con split(). Tampoco
# incluimos "youtube" como token completo: el rechazo de site
# names ("YouTube", "Google", "Facebook"…) lo hace el extractor
# vía ``_SITE_NAME_TOKENS``. Aquí solo bloqueamos firmas
# inequívocamente Polymer/web-content.
_INVALID_PROFILE_EVIDENCE_FRAGMENTS: tuple = (
    "ytd-",          # cualquier custom-element de YouTube (ytd-app, ytd-rich-grid…)
    "style-scope",   # selector Polymer omnipresente en YouTube
    "guide-service", # automation id del menú lateral de YouTube
    "rendering-content",
    "html5-main-video",
    "video-stream",
)


def _looks_like_invalid_profile_evidence(value: Optional[str]) -> bool:
    """¿``value`` viene del contenido web/YouTube y NO de un picker real?

    True ⇒ debe descartarse antes de pasar al extractor canónico.

    Esta función es usada por:
      * :func:`extract_canonical_profile_name` (defensa interna).
      * El intent_collapse_engine al construir
        ``ctx.profile_name_ocr`` / ``ctx.profile_human_observation``
        para evitar arrastrar evidencia inválida hasta aquí.

    Diseño deliberado:
      * Solo bloqueamos firmas inequívocamente Polymer/YouTube
        (``ytd-*``, ``rendering-content``, ``style-scope``, …).
      * NO bloqueamos los control types UIA puros como
        "GroupControl" o "PaneControl" — esos son rechazados por
        el extractor canónico (``_looks_like_name_token`` los
        descarta) pero PUEDEN persistir en el alias_store si el
        usuario los confirmó manualmente. Ese flujo lo cubre el
        test ``test_select_profile_saves_confirmed_alias_for_future_runs``.
    """
    if not value:
        return False
    norm = " ".join(str(value).strip().lower().split())
    if not norm:
        return False
    for frag in _INVALID_PROFILE_EVIDENCE_FRAGMENTS:
        if frag in norm:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Modelo de salida
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ProfileResolution:
    """Resultado de :func:`resolve_profile`.

    Attributes:
        profile_name: nombre canónico resuelto (vacío si NO se pudo).
        source: cuál de las fuentes ganó (``"observation"``, ``"alias"``,
            ``"ocr"``, ``"window_title"``, ``"chrome_local_state"``,
            ``"already_loaded"``, ``"none"``).
        needs_user_confirmation: ``True`` cuando el resolver NO encontró
            evidencia robusta y el caller debe pedir confirmación
            mínima (no regrabar).
        confirmation_prompt: pregunta sugerida si toca confirmar.
        candidate_aliases: aliases que el caller puede ofrecer en el
            prompt de confirmación (todas las observaciones humanas
            que detectamos pero no nos atrevimos a usar como
            canónicas).
        reason: descripción legible de por qué se eligió esta fuente.
    """

    profile_name: str = ""
    source: str = "none"
    needs_user_confirmation: bool = False
    confirmation_prompt: str = ""
    candidate_aliases: List[str] = field(default_factory=list)
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        """¿Tenemos un nombre real listo para ejecutar (sin confirmar)?"""
        return bool(self.profile_name) and not self.needs_user_confirmation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "source": self.source,
            "needs_user_confirmation": self.needs_user_confirmation,
            "confirmation_prompt": self.confirmation_prompt,
            "candidate_aliases": list(self.candidate_aliases),
            "reason": self.reason,
        }


# ──────────────────────────────────────────────────────────────────────
# Extracción de nombre canónico
# ──────────────────────────────────────────────────────────────────────


def _clean_token(tok: str) -> str:
    """Quita puntuación común sin tocar acentos."""
    return tok.strip(",.;:()[]{}\"'`*“”‘’")


def _looks_like_name_token(tok: str) -> bool:
    """¿``tok`` parece un token de nombre propio?

    Reglas:
      * No vacío y >= 2 chars (las iniciales de avatar — "D", "M" — se
        descartan: son separadores, no nombres).
      * Empieza con mayúscula (acepta acentuadas: É, Á, Í, etc.).
      * El resto es alfa (acepta apóstrofes y guiones para apellidos
        compuestos: "O'Brien", "García-López").
      * NO está en :data:`_APP_NAME_TOKENS` (Chrome, Edge, Google…).
      * NO está en :data:`_CONNECTOR_TOKENS` (incluso si viene con
        mayúscula accidental).
    """
    if not tok or len(tok) < 2:
        return False
    first = tok[0]
    if not first.isalpha() or not first.isupper():
        return False
    low = tok.lower()
    if low in _APP_NAME_TOKENS:
        return False
    if low in _CONNECTOR_TOKENS:
        return False
    # Aceptamos guion/apóstrofe; el resto debe ser alfa.
    cleaned = tok.replace("-", "").replace("'", "").replace("´", "")
    if not cleaned:
        return False
    if not all(c.isalpha() for c in cleaned):
        return False
    # Tokens técnicos UIA (CamelCase) jamás son nombres propios
    # — los rechazamos explícitamente. Comparamos sin guiones
    # para que ``video-stream`` se trate igual que ``videostream``.
    cleaned_low = cleaned.lower()
    if cleaned_low in _UIA_TECHNICAL_TOKENS:
        return False
    if low in _UIA_TECHNICAL_TOKENS:
        return False
    # Heurística: si el token termina en ``Control`` (UIA típico:
    # ``GroupControl``, ``PaneControl``, ``ListItemControl``…) lo
    # rechazamos. Ningún apellido legítimo en Latinoamérica/España/
    # Inglés acaba en "Control".
    if cleaned.endswith("Control") and len(cleaned) > len("Control"):
        return False
    return True


def _extract_runs(observation: str) -> List[List[str]]:
    """Recorre el texto y extrae las "corridas" de tokens-nombre.

    Una corrida termina cuando aparece:
      * un token en :data:`_APP_NAME_TOKENS` (Chrome, Edge…),
      * un token <2 chars (iniciales de avatar),
      * un conector minúscula (de, the, of…),
      * un token con caracteres no alfabéticos (números, símbolos).
    """
    runs: List[List[str]] = []
    cur: List[str] = []
    for raw in (observation or "").split():
        tok = _clean_token(raw)
        if not tok:
            if cur:
                runs.append(cur)
                cur = []
            continue
        if _looks_like_name_token(tok):
            cur.append(tok)
        else:
            if cur:
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)
    return runs


def extract_canonical_profile_name(observation: Optional[str]) -> str:
    """Extrae el nombre de perfil canónico de una observación humana.

    Ejemplos
    ~~~~~~~~

    >>> extract_canonical_profile_name("Daniel Chrome D Daniel Arturo Ramos")
    'Daniel Arturo Ramos'

    >>> extract_canonical_profile_name("Daniel Arturo Ramos - Google Chrome")
    'Daniel Arturo Ramos'

    >>> extract_canonical_profile_name("Maria Lopez")
    'Maria Lopez'

    >>> extract_canonical_profile_name("Sign in as Maria Lopez")
    'Maria Lopez'

    >>> extract_canonical_profile_name("perfil del navegador")
    ''

    Estrategia:
      * Tokenizar respetando puntuación común.
      * Construir corridas de tokens-nombre (mayúscula inicial + alfa,
        ignorando ``Chrome``/``Edge``, conectores, iniciales).
      * Elegir la corrida MÁS LARGA (en cantidad de tokens). Empate
        a tokens → la última (suele ser el nombre real, no el alias
        del browser).
      * Si no hay corridas con ≥2 tokens y la observación entera está
        en :data:`_GENERIC_LABELS`, devuelve ``""``.
    """
    if not observation:
        return ""
    obs = " ".join(observation.strip().split())
    if not obs:
        return ""
    if obs.lower() in _GENERIC_LABELS:
        return ""
    # PRD 2026-05-08d §A.2: rechazo defensivo de evidencia
    # claramente proveniente de YouTube / Polymer / contenido web
    # posterior al click del profile picker. Aunque el intent_collapse_
    # engine debería filtrarla antes de llegar acá, esta segunda
    # barrera nos protege contra cualquier path nuevo que pase
    # observation cruda al extractor.
    if _looks_like_invalid_profile_evidence(obs):
        return ""

    # Si la observación contiene un separador de browser ("- Google
    # Chrome", "- Microsoft Edge"), nos quedamos con la parte de la
    # izquierda. Si la izquierda mete el nombre real ("Daniel Arturo
    # Ramos - Google Chrome"), aprovechamos. Si la izquierda es un
    # tab title ("YouTube"), las heurísticas de runs lo descartarán.
    for sep in (" - Google Chrome", " - Chrome", " - Microsoft Edge",
                " - Edge", " - Brave", " - Opera"):
        if sep in obs:
            obs = obs.split(sep, 1)[0].strip()
            break

    runs = _extract_runs(obs)
    if not runs:
        return ""

    # Preferimos la corrida más larga; si empatan, la última (heurística
    # del profile picker — el nombre real suele venir tras la inicial
    # del avatar).
    multi_token = [r for r in runs if len(r) >= 2]
    if multi_token:
        best = multi_token[0]
        for r in multi_token:
            if len(r) > len(best):
                best = r
            elif len(r) == len(best):
                # empate: nos quedamos con la última.
                best = r
        return " ".join(best)

    # Sin corridas multi-token: si solo tenemos una corrida de 1 token
    # y parece nombre propio Y NO es un site name, lo aceptamos
    # (caso "Daniel" sin más). Site names ("YouTube", "Google",
    # "Facebook"…) siempre los rechazamos: nunca son perfiles.
    if len(runs) == 1 and len(runs[0]) == 1:
        single = runs[0][0]
        if single.lower() in _SITE_NAME_TOKENS:
            return ""
        return single
    return ""


def generate_aliases(
    canonical: str,
    *,
    observations: Optional[Iterable[str]] = None,
) -> List[str]:
    """Genera el conjunto de aliases asociados a un nombre canónico.

    Para ``"Daniel Arturo Ramos"`` produce:

      * "Daniel Arturo Ramos" (canonical)
      * "Daniel"
      * "Daniel Arturo"
      * "Daniel Ramos"   (primer + último)
      * "Arturo Ramos"   (suffix de 2 tokens)
      * más cualquier observación cruda que detectamos en el flujo
        ("Daniel Chrome D Daniel Arturo Ramos" del profile picker).

    Cada alias se devuelve normalizado (``" ".join(value.split())``)
    para que la búsqueda en el alias store sea determinística.
    """
    if not canonical or not canonical.strip():
        return []
    canonical = " ".join(canonical.strip().split())
    aliases: set = {canonical}
    tokens = canonical.split()
    if len(tokens) >= 2:
        for i in range(1, len(tokens) + 1):
            aliases.add(" ".join(tokens[:i]))
        aliases.add(f"{tokens[0]} {tokens[-1]}")
        if len(tokens) >= 3:
            aliases.add(" ".join(tokens[-2:]))
    for obs in (observations or []):
        if not obs:
            continue
        norm = " ".join(str(obs).strip().split())
        if norm:
            aliases.add(norm)
    return sorted(aliases)


# ──────────────────────────────────────────────────────────────────────
# Alias store (persistente)
# ──────────────────────────────────────────────────────────────────────


_STORE_LOCK = threading.RLock()


def _alias_store_path() -> Path:
    """Ruta del archivo JSON con los aliases aprendidos."""
    return settings.VAR_PATH / "profile_aliases.json"


def _load_alias_store() -> Dict[str, str]:
    """Lee el alias store del disco. Estructura:

        {
          "Daniel Chrome D Daniel Arturo Ramos": "Daniel Arturo Ramos",
          "Daniel Arturo": "Daniel Arturo Ramos",
          "Daniel": "Daniel Arturo Ramos"
        }

    Devuelve dict vacío si el archivo no existe o está corrupto.
    """
    path = _alias_store_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # normalizamos claves (no el valor — esos son nombres
            # canonicos sensibles a casing).
            return {
                _normalize_alias_key(k): str(v).strip()
                for k, v in data.items()
                if k and v
            }
    except Exception as e:
        log.debug(f"[profile_resolver] alias store corrupto: {e}")
    return {}


def _save_alias_store(store: Dict[str, str]) -> None:
    """Escribe atómicamente el alias store al disco."""
    path = _alias_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(path)
    except Exception as e:
        log.warning(f"[profile_resolver] no pude persistir aliases: {e}")


def _normalize_alias_key(value: str) -> str:
    """Normaliza una clave de alias para búsqueda case/space-insensitive.

    Conservamos acentos (parte de la identidad del nombre). Solo
    bajamos casing y colapsamos espacios.
    """
    return " ".join(str(value or "").strip().lower().split())


def _should_overwrite_alias(old: str, new: str) -> bool:
    """¿Debemos reemplazar el canonical aprendido ``old`` con ``new``?

    Regla de oro (PRD 2026-05-09 fix profile canonicalization):

        Nunca degradar un canonical más completo a un alias corto.

    Casos:
      * ``old == new``                          → no escribir (idempotente).
      * ``new`` tiene MÁS tokens que ``old``    → escribir (upgrade).
      * Igual cantidad de tokens                → escribir (re-mapeo).
      * ``new`` tiene MENOS tokens y todos sus
        tokens están contenidos en ``old``      → NO escribir (degradación).
      * ``new`` más corto pero con tokens que
        no están en ``old`` (es OTRO nombre)    → escribir (re-mapeo a
                                                  un perfil distinto).

    Ejemplos:
      * old="Daniel Arturo Ramos", new="Daniel"
        → subset, NO sobrescribir (degradación).
      * old="Daniel Arturo Ramos", new="Maria Lopez"
        → distinto, sobrescribir (re-mapeo legítimo).
      * old="Daniel", new="Daniel Arturo Ramos"
        → más completo, sobrescribir (upgrade).
    """
    if old == new:
        return False
    old_tokens = old.split()
    new_tokens = new.split()
    if len(new_tokens) >= len(old_tokens):
        return True
    new_set = {t.lower() for t in new_tokens}
    old_set = {t.lower() for t in old_tokens}
    if new_set.issubset(old_set):
        return False
    return True


def learn_profile_alias(
    canonical: str,
    aliases: Optional[Iterable[str]] = None,
) -> int:
    """Persiste alias → canonical en el store local.

    Args:
        canonical: nombre canónico (ej. "Daniel Arturo Ramos"). No
            puede ser vacío ni genérico (ver :data:`_GENERIC_LABELS`).
        aliases: iterable opcional con aliases adicionales que el
            usuario quiere asociar (la observación cruda del flow,
            otras formas como "Daniel Ramos"…). Si es ``None`` o
            vacío, se genera automáticamente con
            :func:`generate_aliases`.

    Returns:
        Número de aliases NUEVOS añadidos al store.

    Garantía anti-degradación (fix PRD 2026-05-09):
        Si una clave del store ya apunta a un canonical MÁS COMPLETO
        que ``canonical`` y los tokens de ``canonical`` son subconjunto
        del existente, NO sobrescribimos. Así llamar
        ``learn_profile_alias("Daniel")`` con
        ``store["daniel"] = "Daniel Arturo Ramos"`` deja el store
        intacto (no degrada el canónico aprendido).
    """
    canonical = " ".join((canonical or "").strip().split())
    if not canonical or canonical.lower() in _GENERIC_LABELS:
        return 0
    aliases = list(aliases or [])
    full = generate_aliases(canonical, observations=aliases)

    with _STORE_LOCK:
        store = _load_alias_store()
        added = 0
        for a in full:
            key = _normalize_alias_key(a)
            if not key:
                continue
            existing = store.get(key)
            if existing is not None and not _should_overwrite_alias(
                existing, canonical,
            ):
                continue
            if existing == canonical:
                continue
            store[key] = canonical
            added += 1
        if added:
            _save_alias_store(store)
    return added


def lookup_profile_by_alias(observation: str) -> Optional[str]:
    """Busca ``observation`` en el alias store.

    Devuelve el ``canonical`` aprendido o ``None`` si nunca lo vimos.
    La búsqueda es case-insensitive y space-tolerant.
    """
    if not observation:
        return None
    key = _normalize_alias_key(observation)
    if not key:
        return None
    with _STORE_LOCK:
        store = _load_alias_store()
    direct = store.get(key)
    if direct:
        return direct
    # Búsqueda parcial: si alguna clave contiene la observación
    # como subcadena (o viceversa), devolvemos el canonical más
    # común. Útil cuando el alias guardado es "Daniel Arturo Ramos"
    # y observamos "Daniel Arturo".
    candidates: Dict[str, int] = {}
    for k, v in store.items():
        if key in k or k in key:
            candidates[v] = candidates.get(v, 0) + 1
    if not candidates:
        return None
    # Devolvemos el más frecuente; empate → el primero por orden
    # alfabético del canonical (estable).
    return sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def reset_alias_store() -> None:
    """Borra el alias store (útil en tests). No-op si no existe."""
    path = _alias_store_path()
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        log.debug(f"[profile_resolver] no pude resetear store: {e}")


# ──────────────────────────────────────────────────────────────────────
# Pipeline de resolución
# ──────────────────────────────────────────────────────────────────────


def _richer_canonical_from_other_sources(
    *,
    base_tokens: int,
    candidates: Iterable[Optional[str]],
) -> str:
    """Devuelve el primer canonical multi-token extraíble de
    ``candidates`` que tenga estrictamente MÁS tokens que ``base_tokens``.

    Uso: cuando ``raw_profile`` produce un canonical truncado (1 token,
    típicamente OCR cortado a la primera palabra) preferimos darle una
    oportunidad a otras fuentes antes de aceptar la versión corta. Esto
    evita el bug clásico donde el OCR dice "Daniel" pero el window_title
    real era "Daniel Arturo Ramos - Google Chrome".

    Devuelve ``""`` si ninguna fuente produce algo más rico (entonces el
    caller debe quedarse con el canonical original).
    """
    for cand in candidates:
        if not cand:
            continue
        canonical = extract_canonical_profile_name(cand)
        if not canonical:
            continue
        if canonical.lower() in _GENERIC_LABELS:
            continue
        if len(canonical.split()) > base_tokens:
            return canonical
    return ""


def _promote_via_alias_store(
    canonical: str,
    observation: Optional[str] = None,
) -> Tuple[str, str]:
    """Promueve ``canonical`` al canónico aprendido en el alias_store
    si éste último es **estrictamente más rico** (más tokens).

    Esta es la garantía operativa de "nunca degradar" para casos como
    ``mission_1778307670``: si el ICE solo cazó "Daniel" (OCR truncado /
    label parcial del picker) y el alias_store del usuario aprendió en
    una misión previa que el perfil real es "Daniel Arturo Ramos", el
    canonical aprendido GANA. Si el aprendido es más corto o igual,
    devolvemos el canonical original (no degradamos jamás).

    Args:
        canonical: nombre que el extractor canónico produjo.
        observation: texto crudo (si existe) — usado para una segunda
            búsqueda en el alias_store por si el observation está en
            store con una clave distinta a ``canonical``.

    Returns:
        ``(profile_name, source)``. ``source`` es
        ``"alias_store_promoted"`` si la promoción ocurrió; cadena vacía
        en caso contrario.
    """
    if not canonical:
        return canonical or "", ""
    try:
        learned_can = lookup_profile_by_alias(canonical)
        learned_obs = (
            lookup_profile_by_alias(observation) if observation else None
        )
    except Exception:  # pragma: no cover - defensivo
        return canonical, ""
    learned = learned_obs or learned_can
    if not learned:
        return canonical, ""
    # Sólo promovemos si el aprendido es estrictamente más rico
    # (más tokens). Esto cubre el caso real:
    #   canonical="Daniel" (1 token) → aprendido="Daniel Arturo Ramos"
    #   (3 tokens) → promovemos.
    # Y bloquea el contra-caso (degradación):
    #   canonical="Daniel Arturo Ramos" (3 tokens) → aprendido="Daniel"
    #   (1 token) → conservamos los 3 tokens originales.
    if len(learned.split()) > len(canonical.split()):
        return learned, "alias_store_promoted"
    return canonical, ""


def resolve_profile(
    *,
    raw_profile: Optional[str] = None,
    human_observation: Optional[str] = None,
    ocr_observation: Optional[str] = None,
    window_title: Optional[str] = None,
    chrome_already_loaded: bool = False,
    chrome_active_profile: Optional[str] = None,
) -> ProfileResolution:
    """Pipeline completo de resolución del perfil.

    Orden de fuentes (PRD 2026-05-07c §A.2):

      a) ``raw_profile`` — lo que el ICE ya tenía detectado.
      b) ``chrome_already_loaded`` + ``chrome_active_profile`` —
         skip_if_loaded: si Chrome ya está abierto en el perfil
         correcto, no hace falta tocar el picker.
      c) ``human_observation`` — la frase humana
         (``"Click en perfil que dice ..."``). El extractor saca
         el canonical aunque venga con basura ("Daniel Chrome D
         Daniel Arturo Ramos").
      d) ``window_title`` — título de la ventana del browser
         (``"Daniel Arturo Ramos - Google Chrome"``).
      e) ``ocr_observation`` — texto OCR cerca del click en el
         picker.
      f) alias store — si vimos antes una observación parecida.

    Si ninguna fuente da un canonical, devolvemos
    ``needs_user_confirmation=True`` con el mejor candidato visible
    para que el UI lo pueda preseleccionar (NO pedir regrabar).
    """
    candidate_aliases: List[str] = []

    def _record(obs: Optional[str]) -> None:
        if obs:
            norm = " ".join(str(obs).strip().split())
            if norm and norm not in candidate_aliases:
                candidate_aliases.append(norm)

    # PRD 2026-05-08d §A.2: barrera dura — si una observación es
    # claramente inválida (YouTube / Polymer / contenido web), la
    # neutralizamos ANTES de propagarla. Así nunca termina:
    #   - guardada como "candidate_aliases" visible al usuario,
    #   - persistida en el alias_store,
    #   - usada como nombre de perfil ejecutable.
    def _scrub(value: Optional[str]) -> Optional[str]:
        if value and _looks_like_invalid_profile_evidence(value):
            log.debug(
                f"[profile_resolver] descartada evidencia inválida='{value}'"
            )
            return None
        return value

    raw_profile = _scrub(raw_profile)
    human_observation = _scrub(human_observation)
    ocr_observation = _scrub(ocr_observation)
    window_title = _scrub(window_title)
    chrome_active_profile = _scrub(chrome_active_profile)

    _record(raw_profile)
    _record(human_observation)
    _record(window_title)
    _record(ocr_observation)
    _record(chrome_active_profile)

    # 1) raw_profile válido y NO genérico → ganador (con cuidado).
    #
    # PRD 2026-05-09 (mission_1778307670): este path debe:
    #   a) Consultar el alias_store para promover canónicos truncados.
    #      El recorder a veces solo captura "Daniel" (1 token, OCR
    #      cortado). Si el alias_store aprendió en una misión previa
    #      "Daniel Arturo Ramos", esa promoción gana.
    #   b) Si el canonical extraído es de un único token Y otra fuente
    #      (human_observation / window_title / OCR) tiene un canonical
    #      con MÁS tokens, NO bloquearse aquí — fall through. Esto
    #      evita que un OCR truncado "Daniel" tape un window_title
    #      legítimo "Daniel Arturo Ramos - Google Chrome".
    #
    # NUNCA degradamos: si el canonical de raw_profile ya es
    # multi-token, ese es el ganador (con promoción opcional al store).
    if raw_profile:
        canonical = extract_canonical_profile_name(raw_profile)
        if canonical and canonical.lower() not in _GENERIC_LABELS:
            preferred, override_source = _promote_via_alias_store(
                canonical, raw_profile,
            )
            single_token_truncated = (
                len(preferred.split()) <= 1
                and override_source != "alias_store_promoted"
            )
            if single_token_truncated:
                richer = _richer_canonical_from_other_sources(
                    base_tokens=len(preferred.split()),
                    candidates=(
                        human_observation,
                        window_title,
                        ocr_observation,
                        chrome_active_profile,
                    ),
                )
                if richer:
                    log.debug(
                        f"[profile_resolver] raw_profile='{raw_profile}' "
                        f"truncado a 1 token; uso fuente más rica "
                        f"='{richer}'"
                    )
                    # Aplicamos la promoción del store sobre la fuente
                    # más rica también (idempotente).
                    promoted, _ = _promote_via_alias_store(richer, richer)
                    return ProfileResolution(
                        profile_name=promoted,
                        source="raw_profile_supplemented",
                        candidate_aliases=candidate_aliases,
                        reason=(
                            f"raw_profile='{raw_profile}' truncado; usé "
                            f"fuente más rica='{richer}' → '{promoted}'"
                        ),
                    )
            return ProfileResolution(
                profile_name=preferred,
                source=override_source or "raw_profile",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"raw_profile='{raw_profile}' → canonical='{preferred}'"
                ),
            )

    # 2) Chrome ya cargado en el perfil correcto.
    if chrome_already_loaded and chrome_active_profile:
        canonical = extract_canonical_profile_name(chrome_active_profile) \
            or chrome_active_profile.strip()
        if canonical:
            return ProfileResolution(
                profile_name=canonical,
                source="already_loaded",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"Chrome ya está activo con perfil '{canonical}'; "
                    "skip_if_loaded permite no tocar el picker."
                ),
            )

    def _prefer_alias_or(canonical: str, observation: str) -> Tuple[str, str]:
        """Wrapper local de :func:`_promote_via_alias_store` que mantiene
        la etiqueta histórica ``"alias_store"`` para los callers de las
        rutas (3)–(5). La lógica de promoción real vive a nivel de
        módulo para que la ruta (1) ``raw_profile`` también la pueda
        usar sin duplicar reglas.
        """
        promoted, src = _promote_via_alias_store(canonical, observation)
        if src == "alias_store_promoted":
            return promoted, "alias_store"
        return canonical, ""

    # 3) Observación humana del flow (entrada explícita del usuario o
    #    label adjunto al click del picker).
    if human_observation:
        canonical = extract_canonical_profile_name(human_observation)
        if canonical:
            preferred, override_source = _prefer_alias_or(
                canonical, human_observation,
            )
            return ProfileResolution(
                profile_name=preferred,
                source=override_source or "human_observation",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"observación humana='{human_observation}' → "
                    f"canonical='{preferred}'"
                ),
            )

    # 4) Window title del browser tras seleccionar perfil.
    if window_title:
        canonical = extract_canonical_profile_name(window_title)
        if canonical:
            preferred, override_source = _prefer_alias_or(
                canonical, window_title,
            )
            return ProfileResolution(
                profile_name=preferred,
                source=override_source or "window_title",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"window_title='{window_title}' → canonical='{preferred}'"
                ),
            )

    # 5) OCR cerca del picker.
    if ocr_observation:
        canonical = extract_canonical_profile_name(ocr_observation)
        if canonical:
            preferred, override_source = _prefer_alias_or(
                canonical, ocr_observation,
            )
            return ProfileResolution(
                profile_name=preferred,
                source=override_source or "ocr",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"ocr='{ocr_observation}' → canonical='{preferred}'"
                ),
            )

    # 6) Alias store (aprendizaje persistente).
    for obs in (human_observation, raw_profile, ocr_observation,
                window_title, chrome_active_profile):
        if not obs:
            continue
        learned = lookup_profile_by_alias(obs)
        if learned:
            return ProfileResolution(
                profile_name=learned,
                source="alias_store",
                candidate_aliases=candidate_aliases,
                reason=(
                    f"alias_store: observación='{obs}' → "
                    f"canonical aprendido='{learned}'"
                ),
            )

    # 7) Sin nombre real recuperable → confirmación mínima.
    best_candidate = ""
    for obs in candidate_aliases:
        # Preferimos cualquier candidato que parezca tener nombres
        # propios extraíbles; el extractor ya filtra basura.
        partial = extract_canonical_profile_name(obs)
        if partial:
            best_candidate = partial
            break
    return ProfileResolution(
        profile_name=best_candidate,
        source="needs_confirmation" if best_candidate else "none",
        needs_user_confirmation=True,
        confirmation_prompt=(
            "¿Qué perfil debo usar? "
            f"[{best_candidate}]" if best_candidate
            else "¿Qué perfil debo usar?"
        ),
        candidate_aliases=candidate_aliases,
        reason=(
            "ninguna fuente dio un canonical robusto; "
            "se necesita confirmación mínima del usuario."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Helpers públicos para callers que vienen del intent_collapse_engine
# ──────────────────────────────────────────────────────────────────────


def is_generic_profile_label(value: Optional[str]) -> bool:
    """¿``value`` es un label genérico que NO identifica un perfil real?

    Reexportada para que :mod:`semantic_execution_plan` la use sin
    duplicar la blacklist. Mantiene compatibilidad con la versión
    legacy de la misma función.
    """
    if not value:
        return True
    norm = " ".join(value.strip().lower().split())
    if not norm:
        return True
    if norm in _GENERIC_LABELS:
        return True
    if norm.startswith(("perfil ", "el perfil ")) and len(norm) <= 14:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Canonicalización single-shot (fix PRD 2026-05-09)
# ──────────────────────────────────────────────────────────────────────
#
# ``resolve_canonical_profile_name`` es el punto único que llama la UI
# de Mission Review (``confirm_profile``) para garantizar que el valor
# que el usuario escribió/confirmó queda promovido al canonical más
# completo conocido. Nunca debe quedar guardado "Daniel" si en el
# alias store ya existe "Daniel Arturo Ramos".
#
# Difiere de :func:`resolve_profile` en que ESTA función opera sobre
# UN solo string crudo (lo que el usuario escribió o lo que ya estaba
# en el plan) — no sobre la cascada completa de fuentes
# (``raw_profile`` + ``human_observation`` + ``window_title`` + …).

# Tokens que NUNCA deben aceptarse como canonical final aunque pasen
# como "single token capitalizado". Cubren: app names, control types
# UIA típicos, labels de UI del propio Chrome profile picker que
# por error podrían quedar guardados como nombre.
_FORBIDDEN_AS_FINAL_CANONICAL: frozenset = frozenset({
    "chrome", "google", "googlechrome", "google chrome",
    "edge", "microsoftedge", "microsoft edge",
    "brave", "opera", "firefox", "safari",
    "groupcontrol", "panecontrol", "buttoncontrol",
    "editcontrol", "documentcontrol", "windowcontrol",
    "perfil", "profile", "default", "user", "usuario",
    "seleccionar perfil", "select profile",
    "daniel chrome",
})


def _is_forbidden_final_canonical(value: str) -> bool:
    """¿``value`` está prohibido como canonical FINAL del perfil?"""
    if not value:
        return True
    norm = " ".join(value.strip().lower().split())
    if not norm:
        return True
    if norm in _FORBIDDEN_AS_FINAL_CANONICAL:
        return True
    if is_generic_profile_label(norm):
        return True
    return False


def _find_richer_canonical_for_token(token: str) -> Optional[str]:
    """Busca en el alias store el canonical MÁS LARGO que contenga
    ``token`` como uno de sus nombres.

    Ej: token="Daniel", store contiene "daniel arturo": "Daniel Arturo Ramos"
    → devuelve "Daniel Arturo Ramos".
    """
    if not token:
        return None
    token_low = token.lower()
    with _STORE_LOCK:
        store = _load_alias_store()
    if not store:
        return None
    best: Optional[str] = None
    seen: set = set()
    for canonical in store.values():
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        c_tokens = canonical.split()
        if len(c_tokens) <= 1:
            continue
        if token_low not in {t.lower() for t in c_tokens}:
            continue
        if best is None or len(c_tokens) > len(best.split()):
            best = canonical
    return best


def resolve_canonical_profile_name(raw_value: Optional[str]) -> Dict[str, Any]:
    """Resuelve el canonical FINAL para un valor crudo de perfil.

    Pensada para el caller de Mission Review: cuando el usuario
    escribe/confirma un nombre de perfil, esta función decide qué
    valor debe quedar persistido en
    ``semantic_execution_plan.steps[*].params.profile_name``.

    Reglas (fix PRD 2026-05-09):

      1. Limpia espacios. Si queda vacío → ``rejected``.
      2. Si es un label genérico / app name / control UIA / label
         del propio profile picker → ``rejected``.
      3. Consulta el alias store con el valor crudo. Si el canonical
         aprendido es MÁS COMPLETO (más tokens) que el extraído del
         crudo, gana.
      4. Si no hay alias directo, extrae canonical con
         :func:`extract_canonical_profile_name`.
      5. Si el canonical extraído es single-token (ej. "Daniel"),
         busca en el alias store cualquier canonical más completo
         que contenga ese token y promuévelo.
      6. NUNCA devuelve un canonical que esté en
         :func:`_is_forbidden_final_canonical` cuando existe alguno
         más completo.

    Returns:
        ``{
            "canonical":  str  - el nombre final a persistir,
            "original":   str  - el valor crudo limpiado,
            "source":     str  - "alias_store" | "extracted" | "as_is" | "rejected",
            "promoted":   bool - True si canonical != original,
            "confidence": float,
        }``
    """
    original = " ".join((raw_value or "").strip().split())
    if not original:
        return {
            "canonical": "",
            "original": "",
            "source": "rejected",
            "promoted": False,
            "confidence": 0.0,
        }
    if is_generic_profile_label(original):
        return {
            "canonical": "",
            "original": original,
            "source": "rejected",
            "promoted": False,
            "confidence": 0.0,
        }

    # 1) Lookup directo en alias store sobre el valor crudo.
    learned = lookup_profile_by_alias(original)
    if learned and not _is_forbidden_final_canonical(learned):
        if len(learned.split()) > len(original.split()):
            return {
                "canonical": learned,
                "original": original,
                "source": "alias_store",
                "promoted": True,
                "confidence": 0.95,
            }

    # 2) Extracción canónica del crudo (limpia "Daniel Chrome D
    #    Daniel Arturo Ramos" → "Daniel Arturo Ramos").
    extracted = extract_canonical_profile_name(original)
    if not extracted:
        # El crudo no produjo nada extraíble (era app name, UIA,
        # label genérico, etc.). Si el lookup directo dio algo
        # legible (no prohibido), úsalo; si no, rechazo.
        if learned and not _is_forbidden_final_canonical(learned):
            return {
                "canonical": learned,
                "original": original,
                "source": "alias_store",
                "promoted": (learned != original),
                "confidence": 0.9,
            }
        return {
            "canonical": "",
            "original": original,
            "source": "rejected",
            "promoted": False,
            "confidence": 0.0,
        }

    # 3) Si extracted es single-token, intenta promover a un canonical
    #    más completo conocido (ej. "Daniel" → "Daniel Arturo Ramos").
    if len(extracted.split()) == 1:
        richer = _find_richer_canonical_for_token(extracted)
        if richer and not _is_forbidden_final_canonical(richer):
            return {
                "canonical": richer,
                "original": original,
                "source": "alias_store",
                "promoted": True,
                "confidence": 0.95,
            }

    # 4) Lookup del valor extraído (por si el alias store conoce un
    #    canonical más completo asociado a esa forma).
    learned_ext = lookup_profile_by_alias(extracted)
    if (learned_ext
            and not _is_forbidden_final_canonical(learned_ext)
            and len(learned_ext.split()) > len(extracted.split())):
        return {
            "canonical": learned_ext,
            "original": original,
            "source": "alias_store",
            "promoted": True,
            "confidence": 0.95,
        }

    # 5) Fallback: devolvemos el extraído tal cual (si no es forbidden).
    if _is_forbidden_final_canonical(extracted):
        return {
            "canonical": "",
            "original": original,
            "source": "rejected",
            "promoted": False,
            "confidence": 0.0,
        }
    return {
        "canonical": extracted,
        "original": original,
        "source": "extracted" if extracted != original else "as_is",
        "promoted": (extracted != original),
        "confidence": 0.9,
    }


class ProfileResolver:
    """Fachada estable para producto/tests sobre :func:`resolve_profile`.

    Expone verbos explícitos de healing sin inventar nombres ni pedir
    regrabación: perfil ya activo vs etiqueta ambigua.
    """

    resolve = staticmethod(resolve_profile)

    @staticmethod
    def skip_if_already_satisfied(
        *,
        profile_name: str,
        reason: str = "profile_already_active",
    ) -> ProfileResolution:
        name = str(profile_name or "").strip()
        return ProfileResolution(
            profile_name=name,
            source="already_loaded",
            needs_user_confirmation=False,
            reason=reason,
        )

    @staticmethod
    def needs_ambiguous_label(
        *,
        reason: str = "profile_name_ambiguous",
    ) -> ProfileResolution:
        return ProfileResolution(
            profile_name="",
            source="none",
            needs_user_confirmation=True,
            confirmation_prompt=(
                "Confirma el nombre exacto que muestra el selector "
                "de perfiles."
            ),
            reason=reason,
        )

    @staticmethod
    def needs_specific_profile_confirmation(
        *,
        candidate_name: str,
        reason: str = "profile_ambiguous",
    ) -> ProfileResolution:
        """Confirmación explícita tipo «¿Este perfil es …?» (sin regrabar)."""
        nm = str(candidate_name or "").strip()
        prompt = (
            f"¿Este perfil es {nm}?"
            if nm
            else "Confirma el nombre exacto que muestra el selector de perfiles."
        )
        return ProfileResolution(
            profile_name=nm,
            source="ambiguous",
            needs_user_confirmation=True,
            confirmation_prompt=prompt,
            reason=reason,
        )

    @staticmethod
    def select_visible_option(
        *,
        option_type: str,
        label: str,
        reason: str = "visible_option_pick",
    ) -> ProfileResolution:
        """Representa una selección visible (p.ej. tarjeta de perfil)."""
        _ = option_type
        nm = str(label or "").strip()
        return ProfileResolution(
            profile_name=nm,
            source="visible_option",
            needs_user_confirmation=False,
            reason=reason,
        )


__all__ = [
    "ProfileResolution",
    "ProfileResolver",
    "extract_canonical_profile_name",
    "generate_aliases",
    "learn_profile_alias",
    "lookup_profile_by_alias",
    "reset_alias_store",
    "resolve_profile",
    "resolve_canonical_profile_name",
    "is_generic_profile_label",
    "is_invalid_profile_evidence",
]


def is_invalid_profile_evidence(value: Optional[str]) -> bool:
    """Versión pública de :func:`_looks_like_invalid_profile_evidence`.

    Reexportada para que ``intent_collapse_engine`` y otros callers
    puedan filtrar evidencia ANTES de poblarla en el contexto.
    """
    return _looks_like_invalid_profile_evidence(value)
