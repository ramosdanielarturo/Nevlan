"""
Nevlan — Vibe Parser
--------------------
Normaliza instrucciones de usuario y payloads de teclado para que el Player
pueda ejecutarlas con precisión, sin depender siempre del LLM.

Soporta tres familias de teclado bajo `action_strategy = send_hotkey`:
  1. Combo simultáneo:   {"combo": True, "keys": ["ctrl", "s"]}
  2. Secuencia de teclas: {"keys": ["tab", "tab", "enter"]}  (combo omitido/False)
  3. Tecla repetida N:   {"key": "tab", "repeat": 3}
  4. Legacy (1 tecla):    {"hotkey": "tab"}

`describe_hotkey_payload` convierte el payload en texto humano para UI.
`parse_keyboard_intent` intenta entender frases como "dame 3 tabs",
"presiona enter", "ctrl+s", "alt+f4", sin llamar al LLM.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Any, List, Optional, Tuple

# Alias canónicos → nombre aceptado por pyautogui.press/hotkey
KEY_ALIASES: Dict[str, str] = {
    "tab": "tab", "tabulador": "tab", "tabulacion": "tab",
    "enter": "enter", "return": "enter", "intro": "enter", "return_": "enter",
    "esc": "esc", "escape": "esc",
    "space": "space", "espacio": "space", "barra espaciadora": "space",
    "backspace": "backspace", "retroceso": "backspace", "borrar": "backspace",
    "delete": "delete", "del": "delete", "supr": "delete", "suprimir": "delete",
    "home": "home", "inicio": "home",
    "end": "end", "fin": "end",
    "pageup": "pageup", "pgup": "pageup", "repag": "pageup", "paginaarriba": "pageup",
    "pagedown": "pagedown", "pgdn": "pagedown", "avpag": "pagedown", "paginaabajo": "pagedown",
    "up": "up", "arriba": "up", "flechaarriba": "up",
    "down": "down", "abajo": "down", "flechaabajo": "down",
    "left": "left", "izquierda": "left", "flechaizquierda": "left",
    "right": "right", "derecha": "right", "flechaderecha": "right",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt",
    "shift": "shift", "mayus": "shift", "mayusculas": "shift",
    "win": "win", "windows": "win", "meta": "win", "cmd": "win",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5",
    "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10",
    "f11": "f11", "f12": "f12",
    "caps": "capslock", "capslock": "capslock", "bloqmayus": "capslock",
    "insert": "insert", "ins": "insert",
    "printscreen": "printscreen", "prtsc": "printscreen", "impr pant": "printscreen",
}

MODIFIERS = {"ctrl", "alt", "shift", "win"}

# Números escritos (es) hasta 20 — suficiente para "presiona tab cinco veces"
_SPANISH_NUM = {
    "una": 1, "uno": 1, "un": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13,
    "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20,
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _normalize_token(tok: str) -> str:
    tok = _strip_accents(tok).strip().lower()
    tok = tok.replace("key.", "")
    return KEY_ALIASES.get(tok, tok)


def normalize_keys(keys: List[str]) -> List[str]:
    out = []
    for k in keys or []:
        if not isinstance(k, str):
            continue
        k2 = _normalize_token(k)
        if k2:
            out.append(k2)
    return out


def describe_hotkey_payload(payload: Dict[str, Any]) -> str:
    """Texto humano para mostrar en la UI (preview del paso)."""
    if not payload:
        return "Presionar tecla"
    combo = bool(payload.get("combo"))
    keys = normalize_keys(payload.get("keys") or [])
    repeat = int(payload.get("repeat") or 0)
    single_key = _normalize_token(str(payload.get("key") or ""))
    legacy = _normalize_token(str(payload.get("hotkey") or ""))

    if combo and keys:
        return "Combinación: " + " + ".join(k.upper() for k in keys)
    if single_key and repeat > 1:
        return f"Presionar {single_key.upper()} {repeat} veces"
    if keys and len(keys) > 1:
        return "Secuencia: " + " → ".join(k.upper() for k in keys)
    if keys:
        return f"Presionar {keys[0].upper()}"
    if single_key:
        return f"Presionar {single_key.upper()}"
    if legacy:
        return f"Presionar {legacy.upper()}"
    return "Presionar tecla"


def validate_hotkey_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza y valida el payload de teclado. Devuelve copia saneada."""
    out: Dict[str, Any] = {}
    if not isinstance(payload, dict):
        return {"hotkey": "enter"}

    keys = payload.get("keys")
    if isinstance(keys, list):
        keys = normalize_keys(keys)
        if keys:
            out["keys"] = keys
            if payload.get("combo"):
                out["combo"] = True

    if "key" in payload:
        k = _normalize_token(str(payload.get("key")))
        if k:
            out["key"] = k

    if "repeat" in payload:
        try:
            r = int(payload.get("repeat") or 0)
            if r > 1:
                out["repeat"] = r
        except Exception:
            pass

    if "interval_ms" in payload:
        try:
            iv = int(payload.get("interval_ms") or 0)
            if iv >= 0:
                out["interval_ms"] = iv
        except Exception:
            pass

    if "hotkey" in payload and "keys" not in out and "key" not in out:
        h = _normalize_token(str(payload.get("hotkey")))
        if h:
            out["hotkey"] = h

    if not out:
        legacy = _normalize_token(str(payload.get("hotkey") or "enter"))
        out["hotkey"] = legacy or "enter"
    return out


def parse_keyboard_intent(text: str) -> Optional[Dict[str, Any]]:
    """
    Intenta convertir una instrucción humana en un payload de send_hotkey
    SIN llamar al LLM. Retorna None si no logra interpretarla con confianza.

    Ejemplos reconocidos:
      - "3 tabs"            → {"key":"tab","repeat":3}
      - "presiona tab tres veces" → idem
      - "tab, tab, tab"     → {"keys":["tab","tab","tab"]}
      - "tab tab tab"       → idem (si toda la cadena son teclas conocidas)
      - "ctrl+s"            → {"combo":True,"keys":["ctrl","s"]}
      - "alt+f4"            → idem
      - "enter"             → {"hotkey":"enter"}
    """
    if not text:
        return None
    raw = text.strip()
    norm = _strip_accents(raw).lower().strip()
    norm = norm.replace("  ", " ")

    # Limpieza de verbos iniciales redundantes para evitar confusión
    # ("presiona enter" → "enter", "haz 3 tabs" → "3 tabs")
    VERBS = (
        "presiona", "pulsa", "presionar", "pulsar",
        "oprime", "oprimir", "teclea", "tipea", "tipear",
        "dame", "da", "haz", "hacer", "repite", "repetir",
        "manda", "mandar", "envia", "enviar",
    )
    tokens_raw = norm.split()
    if tokens_raw and tokens_raw[0] in VERBS:
        tokens_raw = tokens_raw[1:]
        norm = " ".join(tokens_raw).strip()
        if not norm:
            return None

    # 1. Combo con '+'
    if (
        "+" in norm
        and "," not in norm
        and "\n" not in norm
        and " y " not in f" {norm} "
        and " luego " not in f" {norm} "
    ):
        parts = [p.strip() for p in norm.split("+") if p.strip()]
        parts = [_normalize_token(p) for p in parts]
        if len(parts) >= 2 and all(parts):
            has_modifier = any(p in MODIFIERS for p in parts[:-1])
            # Cada parte debe ser razonable como tecla: alias conocido,
            # letra sola, número, o función (f1..f12).
            def _is_keylike(p: str) -> bool:
                if p in KEY_ALIASES.values():
                    return True
                if len(p) == 1 and p.isalnum():
                    return True
                if re.fullmatch(r"f\d{1,2}", p):
                    return True
                return False
            all_known = all(_is_keylike(p) for p in parts)
            if has_modifier and all_known:
                return {"combo": True, "keys": parts}

    # 2. Secuencia con comas → tab, tab, enter
    if "," in norm:
        toks = [_normalize_token(t) for t in re.split(r"[,;]", norm) if t.strip()]
        toks = [t for t in toks if t in KEY_ALIASES.values() or t in KEY_ALIASES]
        if len(toks) >= 2 and all(len(t) > 0 for t in toks):
            return {"keys": toks}

    # 3a. Patrón "N veces <tecla>" o "<N> veces <tecla>"
    m = re.search(
        r"^\s*(?P<n>\d+|[a-z]+)\s+ve(?:z|ces)\s+(?:la\s+tecla\s+)?(?P<key>[a-z0-9]+)\s*$",
        norm,
    )
    if m:
        n_raw = m.group("n") or ""
        key = _normalize_token(m.group("key") or "")
        rep = None
        if n_raw.isdigit():
            rep = int(n_raw)
        elif n_raw in _SPANISH_NUM:
            rep = _SPANISH_NUM[n_raw]
        if key and rep and rep >= 1:
            if rep == 1:
                return {"hotkey": key}
            return {"key": key, "repeat": rep}

    # 3b. Patrón "<tecla> N veces" / "<tecla> x N"
    m = re.search(
        r"(?:la\s+tecla\s+)?"
        r"(?P<key>[a-z0-9]+)\s*"
        r"(?:(?:x|×|\*)\s*(?P<rep1>\d+)"
        r"|(?:\s+(?P<rep2>\d+)\s+ve(?:z|ces))"
        r"|(?:\s+(?P<rep2w>[a-z]+)\s+ve(?:z|ces)))",
        norm,
    )
    if m:
        key = _normalize_token(m.group("key") or "")
        rep_s = m.group("rep1") or m.group("rep2")
        rep_w = m.group("rep2w")
        rep = None
        if rep_s:
            try:
                rep = int(rep_s)
            except Exception:
                rep = None
        elif rep_w and rep_w in _SPANISH_NUM:
            rep = _SPANISH_NUM[rep_w]
        if (
            key
            and rep
            and rep >= 1
            and (key in KEY_ALIASES.values() or key in KEY_ALIASES)
        ):
            if rep == 1:
                return {"hotkey": key}
            return {"key": key, "repeat": rep}

    # 4. Caso "3 tabs" / "5 enters"
    m = re.match(r"^\s*(\d+)\s+([a-z]+s?)\s*$", norm)
    if m:
        n = int(m.group(1))
        k = _normalize_token(m.group(2).rstrip("s"))
        if n >= 1 and k and (k in KEY_ALIASES.values() or k in KEY_ALIASES):
            return {"key": k, "repeat": n} if n > 1 else {"hotkey": k}

    # 5. Frase "tab tab tab" (sin comas)
    toks = [_normalize_token(t) for t in norm.split() if t.strip()]
    known = [t for t in toks if t in KEY_ALIASES.values()]
    if toks and known and len(known) == len(toks) and len(known) >= 2:
        if len(set(known)) == 1:
            return {"key": known[0], "repeat": len(known)}
        return {"keys": known}

    # 6. Una tecla sola
    if len(norm.split()) == 1:
        k = _normalize_token(norm)
        if k in KEY_ALIASES.values():
            return {"hotkey": k}

    return None


def parse_type_text_intent(text: str) -> Optional[Dict[str, Any]]:
    """
    Si el usuario pide "escribe ...", "tipea ...", devuelve payload type_text.
    """
    if not text:
        return None
    m = re.match(
        r"^\s*(?:escribe|tipea|teclea|pon|escribir|tipear)\s+(?:el\s+texto\s+)?[\"'`]?(.+?)[\"'`]?\s*$",
        text.strip(), re.IGNORECASE,
    )
    if not m:
        return None
    return {"text": m.group(1)}
