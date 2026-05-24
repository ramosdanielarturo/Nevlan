"""
ArthurOS Services - WebRecorder
-------------------------------
Captura semántica de elementos web durante la grabación de misiones.

Cuando el evento crudo del MouseListener cae sobre Chrome / Edge / Firefox,
este recorder intenta:

1. Conectarse a la sesión CDP que ya gestiona ``app.skills.tools.web``
   (puerto 9222 por defecto).
2. CONVERTIR el punto absoluto de pantalla al sistema de coordenadas
   del viewport del navegador. NO se clampea: si la conversión cae
   fuera del viewport (browser minimizado, en otro monitor, etc.) se
   abandona la captura web y dejamos que el pipeline UIA/visión se
   encargue. Reglas:
       a) Si el ``window_context`` trae el HWND raíz del navegador,
          usamos ``GetClientRect``+``ClientToScreen`` para encontrar
          el origen (en pantalla) del área cliente.
       b) Calculamos ``vx, vy = x - client_origin_x, y - client_origin_y``.
       c) Validamos: ``elementFromPoint(vx, vy)`` devuelve un elemento
          cuyo ``bbox`` contiene a (vx, vy). Si no, devolvemos None
          con ``low_confidence: True`` para que el TargetResolver lo
          ignore como primary.
3. Localizar el elemento bajo el punto convertido usando
   ``document.elementFromPoint`` dentro de la página correcta (incluyendo
   iframes).
4. Extraer atributos semánticos (role, name, label, placeholder, test id,
   texto, etc.) y CALCULAR varios locators candidatos ordenados por
   especificidad/legibilidad:

       1. get_by_role(role, name=...)
       2. get_by_label(label)
       3. get_by_placeholder(placeholder)
       4. get_by_test_id(testid)
       5. get_by_text(text)        # restringido y exacto
       6. css selector corto
       7. xpath fallback

   Cada locator se VALIDA contra la página: debe matchear ≥1 elemento
   visible, idealmente único. Si matchea varios, lo desambiguamos
   con ``.nth(index)`` solo si es estable.

Devuelve un dict serializable que se guarda en
``CompiledStep.target_context.web_data`` (TargetBundle 2.0).

El recorder es **best-effort y silencioso**: cualquier fallo devuelve
``None`` y permite al pipeline normal (UIA + visión + coords) seguir
funcionando.
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List, Tuple

from app.core.logger import log

# Win32: para convertir coords absolutas → viewport del navegador con
# precisión real (no clamping). Si no está disponible, abandonamos la
# captura web y dejamos que UIA/visión se encarguen.
try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _HAS_W32 = True
except Exception:
    _user32 = None
    _HAS_W32 = False


# Procesos que consideramos "web" para activar el WebRecorder
_WEB_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}


def is_web_process(process_name: Optional[str]) -> bool:
    if not process_name:
        return False
    return process_name.strip().lower() in _WEB_PROCESSES


def _client_origin(hwnd: int) -> Optional[Tuple[int, int]]:
    """Devuelve la esquina superior-izquierda del área CLIENTE del HWND
    en coordenadas de pantalla. Esto es lo que necesita el JS
    (``elementFromPoint`` trabaja en coords del viewport, que es
    relativo al área cliente del browser, no a la ventana entera).

    Devuelve None si no se puede calcular (no hay W32, hwnd inválido).
    """
    if not _HAS_W32 or not hwnd:
        return None
    try:
        pt = wintypes.POINT(0, 0)
        # ClientToScreen mapea (0,0) cliente → coords pantalla.
        ok = _user32.ClientToScreen(int(hwnd), ctypes.byref(pt))
        if not ok:
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None


def _client_size(hwnd: int) -> Optional[Tuple[int, int]]:
    """Devuelve (width, height) del área cliente del HWND, en píxeles."""
    if not _HAS_W32 or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        ok = _user32.GetClientRect(int(hwnd), ctypes.byref(rect))
        if not ok:
            return None
        return int(rect.right - rect.left), int(rect.bottom - rect.top)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# JS extractor — corre dentro de la página y devuelve un payload rico
# del elemento bajo el punto. Cubre frames anidados.
# ──────────────────────────────────────────────────────────────────────

_JS_EXTRACT = r"""
(args) => {
  const x = args.x, y = args.y;
  function pickElement(rootDoc, px, py) {
    let el = rootDoc.elementFromPoint(px, py);
    if (!el) return null;
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(px, py);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }
  function shortCss(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id && /^[a-zA-Z][\w-]*$/.test(el.id)) return "#" + el.id;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      let seg = cur.tagName.toLowerCase();
      if (cur.id && /^[a-zA-Z][\w-]*$/.test(cur.id)) {
        seg += "#" + cur.id;
        parts.unshift(seg);
        break;
      }
      if (cur.classList && cur.classList.length) {
        const cls = Array.from(cur.classList)
          .filter(c => /^[a-zA-Z][\w-]{1,40}$/.test(c) && !c.startsWith("ng-"))
          .slice(0, 2);
        if (cls.length) seg += "." + cls.join(".");
      }
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) seg += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      parts.unshift(seg);
      if (parent && parent.id && /^[a-zA-Z][\w-]*$/.test(parent.id)) {
        parts.unshift("#" + parent.id);
        break;
      }
      cur = parent;
    }
    return parts.join(" > ");
  }
  function xpath(el) {
    if (!el || el.nodeType !== 1) return "";
    const parts = [];
    while (el && el.nodeType === 1) {
      let idx = 1;
      let sib = el.previousSibling;
      while (sib) {
        if (sib.nodeType === 1 && sib.tagName === el.tagName) idx += 1;
        sib = sib.previousSibling;
      }
      parts.unshift(`${el.tagName.toLowerCase()}[${idx}]`);
      el = el.parentElement;
      if (parts.length > 8) break;
    }
    return "/" + parts.join("/");
  }
  function ariaRole(el) {
    if (!el) return "";
    const r = el.getAttribute && el.getAttribute("role");
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute && el.getAttribute("type") || "").toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "input") {
      if (["text", "email", "search", "tel", "url", "password", ""].includes(type)) return "textbox";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button") return "button";
    }
    if (tag === "img") return "img";
    if (tag === "h1" || tag === "h2" || tag === "h3" || tag === "h4" || tag === "h5" || tag === "h6") return "heading";
    return "";
  }
  function accessibleName(el) {
    if (!el) return "";
    const al = el.getAttribute && el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    const labelledBy = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const ids = labelledBy.split(/\s+/);
      const txts = ids.map(id => {
        const ref = document.getElementById(id);
        return ref ? (ref.innerText || ref.textContent || "").trim() : "";
      }).filter(Boolean);
      if (txts.length) return txts.join(" ");
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      if (el.id) {
        const lbl = document.querySelector(`label[for="${el.id}"]`);
        if (lbl) return (lbl.innerText || lbl.textContent || "").trim();
      }
      const wrap = el.closest("label");
      if (wrap) return (wrap.innerText || wrap.textContent || "").trim();
    }
    const t = (el.innerText || el.textContent || "").trim();
    if (t.length > 0 && t.length <= 80) return t;
    const title = el.getAttribute && el.getAttribute("title");
    if (title) return title.trim();
    return "";
  }
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (!r || r.width < 1 || r.height < 1) return false;
    const cs = window.getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0") return false;
    return true;
  }

  // Intenta también iframes anidados.
  function findInFrames(rootDoc, px, py, framePath) {
    const el = pickElement(rootDoc, px, py);
    if (!el) return null;
    if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
      try {
        const inner = el.contentDocument;
        if (inner) {
          const r = el.getBoundingClientRect();
          const inX = px - r.left;
          const inY = py - r.top;
          framePath.push({ src: el.src || "", id: el.id || "", name: el.name || "" });
          const found = findInFrames(inner, inX, inY, framePath);
          if (found) return found;
        }
      } catch (e) { /* cross-origin */ }
    }
    return { el: el, framePath: framePath };
  }

  const hit = findInFrames(document, x, y, []);
  if (!hit || !hit.el) return null;
  const el = hit.el;
  const rect = el.getBoundingClientRect();
  const ctxText = ((el.parentElement && el.parentElement.innerText) || "").trim().slice(0, 200);
  return {
    url: location.href,
    domain: location.hostname,
    frame_path: hit.framePath,
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute("type") || "").toLowerCase(),
    role: ariaRole(el),
    accessible_name: accessibleName(el),
    label: (function() {
      if (el.id) {
        const lbl = document.querySelector(`label[for="${el.id}"]`);
        if (lbl) return (lbl.innerText || "").trim();
      }
      const wrap = el.closest && el.closest("label");
      if (wrap) return (wrap.innerText || "").trim();
      return "";
    })(),
    placeholder: el.getAttribute("placeholder") || "",
    test_id: el.getAttribute("data-testid") || el.getAttribute("data-test-id") || el.getAttribute("data-test") || "",
    aria_label: el.getAttribute("aria-label") || "",
    text: (el.innerText || el.textContent || "").trim().slice(0, 120),
    nearby_text: ctxText,
    href: el.getAttribute("href") || "",
    name_attr: el.getAttribute("name") || "",
    css: shortCss(el),
    xpath: xpath(el),
    visible: visible(el),
    bbox: {
      left: Math.round(rect.left + (window.scrollX || 0)),
      top: Math.round(rect.top + (window.scrollY || 0)),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
    viewport_xy: { x: Math.round(x - rect.left), y: Math.round(y - rect.top) },
    scroll_x: Math.round(window.scrollX || 0),
    scroll_y: Math.round(window.scrollY || 0),
    viewport: { width: window.innerWidth, height: window.innerHeight },
  };
}
"""


# ──────────────────────────────────────────────────────────────────────
# Locator candidate builder + validator
# ──────────────────────────────────────────────────────────────────────

def _build_candidates(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Genera lista ordenada de specs de locator. Cada spec es un dict:
        {"kind": "...", "name": "...", "value": "..."}
    Esto se serializa tal cual en TargetContext.web_data["locators"].
    """
    cands: List[Dict[str, Any]] = []

    role = (info.get("role") or "").strip()
    name = (info.get("accessible_name") or "").strip()
    label = (info.get("label") or "").strip()
    placeholder = (info.get("placeholder") or "").strip()
    test_id = (info.get("test_id") or "").strip()
    text = (info.get("text") or "").strip()
    css = (info.get("css") or "").strip()
    xpath = (info.get("xpath") or "").strip()

    # 1) get_by_role (idealmente con name)
    if role:
        if name and len(name) <= 80:
            cands.append({"kind": "role", "role": role, "name": name})
        cands.append({"kind": "role", "role": role})

    # 2) get_by_label
    if label and len(label) <= 80:
        cands.append({"kind": "label", "value": label})

    # 3) get_by_placeholder
    if placeholder:
        cands.append({"kind": "placeholder", "value": placeholder})

    # 4) get_by_test_id (más estable)
    if test_id:
        cands.append({"kind": "test_id", "value": test_id})

    # 5) get_by_text (restringimos a texto corto y único-ish)
    if text and 1 < len(text) <= 60:
        cands.append({"kind": "text", "value": text})

    # 6) css corto
    if css:
        cands.append({"kind": "css", "value": css})

    # 7) xpath fallback
    if xpath:
        cands.append({"kind": "xpath", "value": xpath})

    return cands


def _validate_locator(page, spec: Dict[str, Any]) -> Tuple[bool, int, bool]:
    """Devuelve (ok, count, is_unique). ok=True si matchea ≥1 visible."""
    try:
        loc = _build_locator(page, spec)
        if loc is None:
            return False, 0, False
        n = loc.count()
        if n == 0:
            return False, 0, False
        is_unique = (n == 1)
        # Comprobamos visibilidad del primero (rápido)
        try:
            first = loc.first
            if not first.is_visible(timeout=300):
                # Algunos test_id apuntan a wrappers invisibles → desambiguamos
                # buscando el primer visible.
                for i in range(min(n, 5)):
                    if loc.nth(i).is_visible(timeout=200):
                        return True, n, is_unique
                return False, n, is_unique
        except Exception:
            return True, n, is_unique
        return True, n, is_unique
    except Exception:
        return False, 0, False


def _build_locator(page, spec: Dict[str, Any]):
    try:
        kind = (spec.get("kind") or "").lower()
        if kind == "role":
            role = spec.get("role")
            name = spec.get("name")
            if not role:
                return None
            return page.get_by_role(role, name=name) if name else page.get_by_role(role)
        if kind == "label":
            return page.get_by_label(spec.get("value", ""))
        if kind == "placeholder":
            return page.get_by_placeholder(spec.get("value", ""))
        if kind == "test_id":
            return page.get_by_test_id(spec.get("value", ""))
        if kind == "text":
            return page.get_by_text(spec.get("value", ""), exact=False)
        if kind == "css":
            return page.locator(spec.get("value", ""))
        if kind == "xpath":
            return page.locator(f"xpath={spec.get('value', '')}")
    except Exception:
        return None
    return None


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def capture_web_target(x: int, y: int,
                       process_name: Optional[str],
                       hwnd: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Captura datos web del elemento bajo (x, y).

    Solo actúa si:
      - process_name pertenece a un navegador soportado.
      - Existe una sesión CDP accesible (puerto 9222 / playwright).
      - La conversión screen → viewport produce coords razonables.

    Devuelve un dict listo para asignar a ``CompiledStep.target_context.web_data``
    o None si no se pudo (silencioso). El dict incluye ``low_confidence: True``
    cuando la conversión de coords fue heurística (no había HWND), para que
    el TargetResolverV2 NO lo use como primary y caiga a UIA/visión.
    """
    if not is_web_process(process_name):
        return None

    try:
        from app.skills.tools.web import _get_page  # type: ignore
    except Exception as e:
        log.debug(f"web_recorder: bridge web no disponible: {e}")
        return None

    # ⚠️ connect_only=True es CRÍTICO para no lanzar Chrome durante la
    # grabación. Sin este flag, cada click sobre Chrome cerraba el
    # browser CDP global del agente (porque el grabador corre en otro
    # thread) y arrancaba uno nuevo con ``subprocess.Popen``, abriendo
    # múltiples ventanas de Chrome encima del usuario. Bug real
    # reportado el 2026-05-01.
    try:
        page = _get_page(connect_only=True)
    except TypeError:
        # Versión antigua de _get_page sin parámetro: degrada a None
        # para no abrir Chrome.
        page = None
    except Exception as e:
        log.debug(f"web_recorder: no hay página activa: {e}")
        return None
    if page is None:
        return None

    # ── Conversión absoluta → viewport ────────────────────────────
    # Estrategia (sin clamping):
    #   1. Si tenemos HWND del browser, usamos ClientToScreen para
    #      obtener el origen del área cliente y restamos.
    #   2. Si NO tenemos HWND, intentamos via Playwright
    #      (window.screenX/Y); si tampoco, devolvemos low_confidence.
    #   3. Validamos que el punto convertido esté DENTRO del viewport.
    #      Si no, no devolvemos nada — la grabación caerá a UIA.
    vx: Optional[int] = None
    vy: Optional[int] = None
    conversion_method = "unknown"

    if hwnd:
        origin = _client_origin(int(hwnd))
        client = _client_size(int(hwnd))
        if origin and client:
            ox, oy = origin
            cw, ch = client
            cand_x = int(x) - ox
            cand_y = int(y) - oy
            # Validamos que el punto cae dentro del área cliente; si no,
            # la captura no es fiable (tab activa diferente, ventana
            # parcialmente cubierta, etc.).
            if 0 <= cand_x < cw and 0 <= cand_y < ch:
                vx, vy = cand_x, cand_y
                conversion_method = "win32_client_rect"

    if vx is None:
        # Fallback: usar window.screenX/Y desde la página. Esto es
        # fiable en navegadores normales pero puede fallar bajo CDP
        # cuando hay múltiples ventanas. NO clampeamos.
        try:
            screen = page.evaluate(
                "() => ({sx: window.screenX, sy: window.screenY,"
                " iw: window.innerWidth, ih: window.innerHeight,"
                " ox: window.outerWidth, oy: window.outerHeight})"
            ) or {}
            sx = int(screen.get("sx", 0) or 0)
            sy = int(screen.get("sy", 0) or 0)
            iw = int(screen.get("iw", 0) or 0)
            ih = int(screen.get("ih", 0) or 0)
            ox = int(screen.get("ox", 0) or 0)
            oy = int(screen.get("oy", 0) or 0)
            # Aproximación: el viewport empieza después de la barra de
            # título y la barra de pestañas. Estimamos `oy - ih` para
            # el alto del cromo. Si no podemos, usamos 0.
            chrome_top = max(0, oy - ih)
            chrome_left = max(0, (ox - iw) // 2)
            cand_x = int(x) - sx - chrome_left
            cand_y = int(y) - sy - chrome_top
            if iw > 0 and ih > 0 and 0 <= cand_x < iw and 0 <= cand_y < ih:
                vx, vy = cand_x, cand_y
                conversion_method = "js_screen_origin"
        except Exception as e:
            log.debug(f"web_recorder: js screen origin falló: {e}")

    if vx is None or vy is None:
        log.debug(
            f"web_recorder: no se pudo convertir ({x},{y}) a viewport. "
            f"Captura web abandonada (low_confidence)."
        )
        return None

    try:
        info = page.evaluate(_JS_EXTRACT, {"x": int(vx), "y": int(vy)})
    except Exception as e:
        log.debug(f"web_recorder: evaluate failed: {e}")
        return None

    if not info:
        return None

    # Validación de coherencia: el bbox del elemento devuelto por
    # `elementFromPoint` debe contener al punto convertido (en coords
    # del viewport, que es lo que el JS reporta antes de sumar scroll).
    try:
        bbox = info.get("bbox") or {}
        sx = info.get("scroll_x", 0) or 0
        sy = info.get("scroll_y", 0) or 0
        # bbox en el JS lo guardamos en coords absolutas al documento
        # (incluye scrollX/Y), restamos para volver al viewport.
        vbox_left = int(bbox.get("left", 0)) - int(sx)
        vbox_top = int(bbox.get("top", 0)) - int(sy)
        vbox_right = vbox_left + int(bbox.get("width", 0))
        vbox_bottom = vbox_top + int(bbox.get("height", 0))
        contains = (vbox_left <= int(vx) <= vbox_right
                    and vbox_top <= int(vy) <= vbox_bottom)
        if not contains:
            log.debug(
                f"web_recorder: elemento devuelto NO contiene el punto convertido"
                f" ({vx},{vy}) ∉ ({vbox_left},{vbox_top},{vbox_right},{vbox_bottom})"
                f". Marcando low_confidence."
            )
            info["low_confidence"] = True
    except Exception:
        pass

    # Construimos y validamos candidatos.
    candidates = _build_candidates(info)
    valid_locators: List[Dict[str, Any]] = []
    seen_keys = set()
    for spec in candidates:
        key = (spec.get("kind"), spec.get("role"),
               spec.get("name"), spec.get("value"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ok, count, unique = _validate_locator(page, spec)
        if not ok:
            continue
        spec_out = dict(spec)
        spec_out["match_count"] = count
        spec_out["unique"] = unique
        # Si no es único pero matchea pocos, indexamos con nth(0)
        if not unique and count <= 5:
            spec_out["nth"] = 0
        valid_locators.append(spec_out)

    # Si tras toda la captura no logramos NINGÚN locator válido y la
    # conversión fue heurística, mejor no devolvemos web_data: el
    # resolver caería intentando locators rotos. Dejamos que UIA/visión
    # cubran el caso.
    if not valid_locators and (info.get("low_confidence") or
                                conversion_method != "win32_client_rect"):
        log.debug(
            "web_recorder: sin locators válidos + conversión heurística"
            " → abandonamos captura web."
        )
        return None

    # Si la conversión NO fue por client rect (Win32), o el bbox no
    # contiene el punto, marcamos input_value_at_capture vacío y
    # confidence baja para que el resolver no use web como primary.
    low_conf = bool(info.get("low_confidence")) or \
                conversion_method != "win32_client_rect"

    # Capturamos input_value_at_capture si el elemento es editable, para
    # que el compiler pueda comparar antes/después de SET_FIELD_VALUE.
    input_value_at_capture: str = ""
    try:
        if (info.get("tag") or "").lower() in ("input", "textarea", "select"):
            v = page.evaluate(
                "(args) => {"
                " const el = document.elementFromPoint(args.x, args.y);"
                " return el ? (el.value != null ? String(el.value) : '') : '';"
                "}", {"x": int(vx), "y": int(vy)})
            if v is not None:
                input_value_at_capture = str(v)
    except Exception:
        pass

    web_data: Dict[str, Any] = {
        "url": info.get("url"),
        "domain": info.get("domain"),
        "frame_path": info.get("frame_path") or [],
        "role": info.get("role") or "",
        "accessible_name": info.get("accessible_name") or "",
        "label": info.get("label") or "",
        "placeholder": info.get("placeholder") or "",
        "test_id": info.get("test_id") or "",
        "text": info.get("text") or "",
        "nearby_text": info.get("nearby_text") or "",
        "tag": info.get("tag") or "",
        "type": info.get("type") or "",
        "name_attr": info.get("name_attr") or "",
        "href": info.get("href") or "",
        "css": info.get("css") or "",
        "xpath": info.get("xpath") or "",
        "bbox": info.get("bbox") or {},
        "locators": valid_locators,
        "conversion_method": conversion_method,
        "low_confidence": low_conf,
        "input_value_at_capture": input_value_at_capture,
        "viewport_xy": info.get("viewport_xy") or {},
        "scroll_x": info.get("scroll_x", 0),
        "scroll_y": info.get("scroll_y", 0),
    }
    return web_data


__all__ = ["capture_web_target", "is_web_process"]
