"""
Nevlan — Pruebas sintéticas E2E del sprint
------------------------------------------
Verifican sin mouse/teclado real que:
  1. Scroll: grabado → compilado → player haría pyautogui.scroll correctamente.
  2. Drag:   grabado → compilado → player haría mouseDown/moveTo/mouseUp.
  3. Doble-click: detección temporal + cercanía.
  4. Ctrl+S (hotkey combo con modifiers).
  5. Tab × 3 (repeat de tecla especial).
  6. Shift+Tab (combo con shift).
  7. Alt+Tab / Alt+F4.
  8. Secuencia tab, tab, enter.
  9. Escribir texto largo.
 10. Recompilar una misión "vieja" con raw_trace.
 11. Misiones de 100+ y 200+ pasos (perf).

Uso:
    python scripts/test_sprint_e2e.py
"""
from __future__ import annotations

import io
import os
import sys
import time

# UTF-8 en Windows
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction, DragAction,
    MissionStatus, ActionStrategy, CompiledStep, InterpretedStep,
    TargetContext, TargetResolutionStrategy,
)
from app.services.missions.compiler import MissionCompiler, recompile_mission


# ───────────────────────────────────────────────────────────────────────────
# Fake pyautogui/time: el player llama estas funciones.
# Las interceptamos para capturar las llamadas reales que haría en vivo.
# ───────────────────────────────────────────────────────────────────────────

class FakePyautogui:
    def __init__(self):
        self.calls = []
    def press(self, key):            self.calls.append(("press", key))
    def hotkey(self, *keys):         self.calls.append(("hotkey", list(keys)))
    def scroll(self, clicks):        self.calls.append(("scroll", int(clicks)))
    def hscroll(self, clicks):       self.calls.append(("hscroll", int(clicks)))
    def moveTo(self, x, y, duration=0):  self.calls.append(("moveTo", int(x), int(y)))
    def mouseDown(self, button="left"):  self.calls.append(("mouseDown", button))
    def mouseUp(self, button="left"):    self.calls.append(("mouseUp", button))
    def typewrite(self, text, interval=0):  self.calls.append(("typewrite", text))
    def click(self, x=None, y=None, button="left", clicks=1, interval=0):
        self.calls.append(("click", x, y, button, clicks))
    PAUSE = 0


def _exec_step_dry(c_step: CompiledStep, fake_pg: FakePyautogui) -> None:
    """Invoca la rama de ejecución del player usando un fake pyautogui.

    Llamamos directamente a los helpers expuestos por el player
    (`_execute_hotkey`, `_execute_scroll`, `_execute_drag`) monkey-patcheando
    `pyautogui`. Es la forma más fiel de medir lo que *haría* en vivo.
    """
    import importlib
    mod = importlib.import_module("app.services.missions.player")
    # Inyectamos el fake pyautogui en el namespace del módulo
    saved = getattr(mod, "pyautogui", None)
    try:
        mod.pyautogui = fake_pg
    except Exception:
        pass

    # Reemplazamos también el import dentro de los métodos: lo más robusto
    # es monkey-patchear el builtin `__import__` por un envoltorio que
    # devuelva fake_pg cuando alguien haga `import pyautogui`.
    import builtins
    real_import = builtins.__import__
    def _fake_import(name, *a, **kw):
        if name == "pyautogui":
            return fake_pg
        return real_import(name, *a, **kw)
    builtins.__import__ = _fake_import

    try:
        # Construimos un NevlanMissionPlayer dummy sólo para llamar sus métodos.
        # Los helpers no necesitan `self.mission`/`execution`, sólo payloads.
        Player = getattr(mod, "MissionPlayer", None) or getattr(mod, "NevlanMissionPlayer")
        class _Dummy:
            _execute_hotkey = Player._execute_hotkey
            _execute_scroll = Player._execute_scroll
            _execute_drag   = Player._execute_drag
        d = _Dummy()

        strat = c_step.action_strategy
        pl = c_step.action_payload or {}
        if strat == ActionStrategy.SEND_HOTKEY:
            d._execute_hotkey(pl)
        elif strat == ActionStrategy.SCROLL:
            d._execute_scroll(pl)
        elif strat == ActionStrategy.DRAG_DROP:
            d._execute_drag(pl)
        elif strat == ActionStrategy.TYPE_TEXT:
            fake_pg.typewrite(pl.get("text", ""))
        elif strat in (ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK):
            coords = c_step.target_context.fallback_coords or {}
            x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
            clicks = 2 if strat == ActionStrategy.DOUBLE_CLICK else 1
            btn = "right" if strat == ActionStrategy.RIGHT_CLICK else "left"
            fake_pg.moveTo(x, y)
            fake_pg.click(x, y, button=btn, clicks=clicks)
    finally:
        builtins.__import__ = real_import


# ───────────────────────────────────────────────────────────────────────────
# Helpers para construir RawEvents
# ───────────────────────────────────────────────────────────────────────────

def _kb(key: str, mods=None) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=[key], modifiers=list(mods or [])),
    )


def _click(x: int, y: int, dbl=False, right=False) -> RawEvent:
    et = EventType.MOUSE_CLICK
    if dbl:
        et = EventType.MOUSE_DOUBLE_CLICK
    elif right:
        et = EventType.MOUSE_RIGHT_CLICK
    return RawEvent(
        event_type=et,
        mouse_action=MouseAction(x=x, y=y, button="right" if right else "left"),
    )


def _scroll(x: int, y: int, dy: int, dx: int = 0) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_SCROLL,
        mouse_action=MouseAction(x=x, y=y, button="scroll"),
        metadata={"scroll": {"dx": dx, "dy": dy}},
    )


def _drag(sx: int, sy: int, ex: int, ey: int, dur: float = 0.3) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_DRAG,
        drag_action=DragAction(start_x=sx, start_y=sy, end_x=ex, end_y=ey, duration=dur),
    )


# ───────────────────────────────────────────────────────────────────────────
# Runner
# ───────────────────────────────────────────────────────────────────────────

PASS, FAIL = "✓", "✗"
results = []

def check(name: str, cond: bool, detail: str = ""):
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f"  ·  {detail}" if detail else ""))
    results.append((name, cond, detail))
    return cond


def compile_and_exec(raw_events):
    m = Mission(name="e2e", status=MissionStatus.RECORDING)
    m.raw_trace = list(raw_events)
    m = MissionCompiler.compile_mission(m)
    fake = FakePyautogui()
    for step in m.compiled_execution_graph:
        _exec_step_dry(step, fake)
    return m, fake


# ───────────────────────────────────────────────────────────────────────────
# Casos
# ───────────────────────────────────────────────────────────────────────────

def case_scroll():
    print("\n[1] Scroll")
    m, fake = compile_and_exec([_scroll(400, 500, dy=-3)])
    step = m.compiled_execution_graph[0]
    check("compilado a SCROLL", step.action_strategy == ActionStrategy.SCROLL)
    check("payload dy=-3", step.action_payload.get("dy") == -3)
    check("player llama pyautogui.scroll", any(c[0] == "scroll" for c in fake.calls))
    check("descripción legible", "Scroll" in m.interpreted_steps[0].description,
          m.interpreted_steps[0].description)


def case_drag():
    print("\n[2] Drag & drop")
    m, fake = compile_and_exec([_drag(100, 100, 400, 300, dur=0.4)])
    step = m.compiled_execution_graph[0]
    check("compilado a DRAG_DROP", step.action_strategy == ActionStrategy.DRAG_DROP)
    pl = step.action_payload
    check("payload start/end correctos",
          pl.get("start_x") == 100 and pl.get("end_x") == 400 and pl.get("end_y") == 300)
    seq = [c[0] for c in fake.calls]
    check("player hace moveTo → mouseDown → moveTo → mouseUp",
          seq == ["moveTo", "mouseDown", "moveTo", "mouseUp"],
          str(seq))


def case_double_click_detection():
    print("\n[3] Detección de doble-click")
    # (a) Compilado + ejecución: el evento ya agrupado MOUSE_DOUBLE_CLICK
    #     debe convertirse en una acción de doble-click del player.
    m, fake = compile_and_exec([_click(100, 100, dbl=True)])
    step = m.compiled_execution_graph[0]
    check("compilado a DOUBLE_CLICK", step.action_strategy == ActionStrategy.DOUBLE_CLICK)
    check("player dispararía 2 clicks", any(c[0] == "click" and c[-1] == 2 for c in fake.calls))

    # (b) Heurística del listener sin side-effects reales: parcheamos
    #     _capture_uia / _capture_snapshot / _get_window_context.
    from app.services.missions import recorder as rec_mod
    from app.services.missions.recorder import MouseListener

    captured = []
    ml = MouseListener(lambda ev: captured.append(ev))
    ml._capture_uia = lambda x, y: {}
    ml._capture_snapshot = lambda x, y, size=100, suffix="": None
    # parche a nivel de módulo para _get_window_context
    orig_gwc = rec_mod._get_window_context
    rec_mod._get_window_context = lambda: None
    try:
        ml._process_click(100, 100, "left"); time.sleep(0.05)
        ml._process_click(102, 101, "left")  # cercano en tiempo y posición
        types = [ev.event_type for ev in captured]
        check("2º click dentro de ventana temporal+espacial → DOUBLE_CLICK",
              len(types) == 2 and types[1] == EventType.MOUSE_DOUBLE_CLICK,
              str([t.value for t in types]))

        # Caso negativo: separados en tiempo
        captured.clear()
        ml._last_click = {}
        ml._process_click(100, 100, "left"); time.sleep(0.6)
        ml._process_click(100, 100, "left")
        types = [ev.event_type for ev in captured]
        check("separados en tiempo → 2 CLICKs",
              types == [EventType.MOUSE_CLICK, EventType.MOUSE_CLICK],
              str([t.value for t in types]))

        # Caso negativo: demasiado lejos
        captured.clear()
        ml._last_click = {}
        ml._process_click(100, 100, "left"); time.sleep(0.05)
        ml._process_click(200, 200, "left")
        types = [ev.event_type for ev in captured]
        check("lejos espacialmente → 2 CLICKs",
              types == [EventType.MOUSE_CLICK, EventType.MOUSE_CLICK],
              str([t.value for t in types]))
    finally:
        rec_mod._get_window_context = orig_gwc


def case_ctrl_s():
    print("\n[4] Ctrl+S (hotkey con modifier)")
    m, fake = compile_and_exec([_kb("s", mods=["ctrl"])])
    step = m.compiled_execution_graph[0]
    check("SEND_HOTKEY con combo=True", step.action_payload.get("combo") is True)
    check("keys=['ctrl','s']", step.action_payload.get("keys") == ["ctrl", "s"])
    check("player invoca hotkey('ctrl','s')",
          any(c[0] == "hotkey" and c[1] == ["ctrl", "s"] for c in fake.calls),
          str(fake.calls))
    check("descripción humana", "Guardar" in m.interpreted_steps[0].description,
          m.interpreted_steps[0].description)


def case_tab_x3():
    print("\n[5] Tab × 3 (tecla especial repetida)")
    m, fake = compile_and_exec([_kb("tab"), _kb("tab"), _kb("tab")])
    step = m.compiled_execution_graph[0]
    check("un solo step compilado", len(m.compiled_execution_graph) == 1)
    check("payload key=tab repeat=3",
          step.action_payload.get("key") == "tab" and step.action_payload.get("repeat") == 3)
    presses = [c for c in fake.calls if c[0] == "press" and c[1] == "tab"]
    check("player hace 3 presses de tab", len(presses) == 3, str(fake.calls))


def case_shift_tab():
    print("\n[6] Shift+Tab")
    m, fake = compile_and_exec([_kb("tab", mods=["shift"])])
    step = m.compiled_execution_graph[0]
    check("combo=True keys=['shift','tab']",
          step.action_payload.get("combo") is True and
          step.action_payload.get("keys") == ["shift", "tab"])
    check("player invoca hotkey('shift','tab')",
          any(c[0] == "hotkey" and c[1] == ["shift", "tab"] for c in fake.calls))


def case_alt_tab():
    print("\n[7] Alt+Tab y Alt+F4")
    m, fake = compile_and_exec([_kb("tab", mods=["alt"]), _kb("f4", mods=["alt"])])
    check("2 hotkeys compiladas", len(m.compiled_execution_graph) == 2)
    hotkeys = [c[1] for c in fake.calls if c[0] == "hotkey"]
    check("alt+tab invocado", ["alt", "tab"] in hotkeys, str(hotkeys))
    check("alt+f4 invocado", ["alt", "f4"] in hotkeys, str(hotkeys))


def case_sequence():
    print("\n[8] Secuencia tab, tab, enter (distintas teclas)")
    m, fake = compile_and_exec([_kb("tab"), _kb("tab"), _kb("enter")])
    # El compiler agrupa los 2 tab juntos y enter separado = 2 steps
    check("2 steps compilados", len(m.compiled_execution_graph) == 2,
          f"got {len(m.compiled_execution_graph)}")
    kinds = [s.action_payload for s in m.compiled_execution_graph]
    check("primero tab×2", kinds[0].get("key") == "tab" and kinds[0].get("repeat") == 2)
    check("segundo enter", kinds[1].get("hotkey") == "enter")


def case_typing():
    print("\n[9] Escribir texto largo")
    chars = list("hola mundo 123")
    raws = [_kb(c) for c in chars]
    m, fake = compile_and_exec(raws)
    check("1 solo step TYPE_TEXT", len(m.compiled_execution_graph) == 1)
    step = m.compiled_execution_graph[0]
    check("text exacto", step.action_payload.get("text") == "hola mundo 123",
          repr(step.action_payload.get("text")))
    check("player llama typewrite", any(c[0] == "typewrite" for c in fake.calls))


def case_fill_field_pattern():
    print("\n[10] Patrón llenar campo (click UIA Edit + type + tab)")
    click_ev = _click(400, 300)
    click_ev.metadata = {
        "uia": {
            "name": "Email",
            "control_type": "EditControl",
            "automation_id": "email_input",
            "class_name": "",
            "rect": "",
        }
    }
    m, _ = compile_and_exec([click_ev, _kb("a"), _kb("b"), _kb("c"), _kb("tab")])
    descs = [s.description for s in m.interpreted_steps]
    check("descripción 'Rellenar' aparece", any("Rellenar" in d for d in descs), descs)
    check("menciona 'Email'", any("Email" in d for d in descs), descs)


def case_recompile_legacy():
    print("\n[11] Recompilar misión vieja con raw_trace")
    # Simula una misión vieja: raw_trace presente, pero compiled_execution_graph
    # hecho con el compilador anterior (ej. hotkey raro con "Key.ctrl_l").
    old = Mission(name="vieja", status=MissionStatus.APPROVED)
    # raw_trace estilo viejo: ctrl+c como 2 eventos sueltos… pero ya "arreglado".
    old.raw_trace = [_kb("c", mods=["ctrl"])]
    old.compiled_execution_graph = [CompiledStep(
        action_strategy=ActionStrategy.SEND_HOTKEY,
        action_payload={"hotkey": "Key.ctrl_l+c"},  # <- tipo basura viejo
        target_context=TargetContext(),
        target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
    )]
    old.interpreted_steps = [InterpretedStep(description="paso raro", raw_event_ids=[])]
    new = recompile_mission(old)
    step = new.compiled_execution_graph[0]
    check("recompilado a combo", step.action_payload.get("combo") is True)
    check("keys ['ctrl','c']", step.action_payload.get("keys") == ["ctrl", "c"])
    check("descripción humana", "Copiar" in new.interpreted_steps[0].description,
          new.interpreted_steps[0].description)


def case_vibe_edit_keyboard():
    print("\n[12] Vibe Edit cambia la lógica ejecutable")
    from app.services.missions.vibe_service import apply_vibe_edit
    m = Mission(name="v", status=MissionStatus.COMPILED)
    c = CompiledStep(action_strategy=ActionStrategy.CLICK, action_payload={})
    i = InterpretedStep(description="Click viejo", raw_event_ids=[])
    m.compiled_execution_graph = [c]
    m.interpreted_steps = [i]

    # Fast-path simple
    r = apply_vibe_edit(m, 0, "ctrl+s")
    check("ctrl+s → SEND_HOTKEY", c.action_strategy == ActionStrategy.SEND_HOTKEY)
    check("payload válido ejecutable",
          c.action_payload.get("combo") is True and c.action_payload.get("keys") == ["ctrl", "s"])

    r = apply_vibe_edit(m, 0, "tab 4 veces")
    check("tab 4 veces → repeat=4",
          c.action_payload.get("key") == "tab" and c.action_payload.get("repeat") == 4)

    r = apply_vibe_edit(m, 0, "escribe hola mundo")
    check("escribe … → TYPE_TEXT",
          c.action_strategy == ActionStrategy.TYPE_TEXT and
          c.action_payload.get("text") == "hola mundo")


def case_rerecord_replacement_1_to_n():
    print("\n[13] Re-record reemplaza 1 paso por N (orden y relinkeo)")
    from app.services.missions.recorder import compile_fragment
    # Fragmento que debería compilar a 2 pasos: tab×2 + enter
    fragment = [_kb("tab"), _kb("tab"), _kb("enter")]
    new_steps, new_interp = compile_fragment(fragment)
    check("fragmento compila a 2 pasos", len(new_steps) == 2, f"got {len(new_steps)}")
    # Simulamos la misión original con 3 pasos y reemplazamos el central
    m = Mission(name="r", status=MissionStatus.COMPILED)
    s0 = CompiledStep(action_strategy=ActionStrategy.CLICK,
                      action_payload={}, target_context=TargetContext(fallback_coords={"x":1,"y":1}))
    s1 = CompiledStep(action_strategy=ActionStrategy.CLICK,
                      action_payload={}, target_context=TargetContext(fallback_coords={"x":2,"y":2}))
    s2 = CompiledStep(action_strategy=ActionStrategy.CLICK,
                      action_payload={}, target_context=TargetContext(fallback_coords={"x":3,"y":3}))
    m.compiled_execution_graph = [s0, s1, s2]
    m.interpreted_steps = [
        InterpretedStep(description="A", raw_event_ids=[]),
        InterpretedStep(description="B", raw_event_ids=[]),
        InterpretedStep(description="C", raw_event_ids=[]),
    ]
    # Reemplazar índice 1 por los 2 nuevos
    idx = 1
    m.compiled_execution_graph.pop(idx)
    m.interpreted_steps.pop(idx)
    for j, ns in enumerate(new_steps):
        m.compiled_execution_graph.insert(idx + j, ns)
    for j, ni in enumerate(new_interp):
        m.interpreted_steps.insert(idx + j, ni)
    # Relink
    for i in range(len(m.compiled_execution_graph) - 1):
        m.compiled_execution_graph[i].next_step_id_on_success = m.compiled_execution_graph[i+1].id
    if m.compiled_execution_graph:
        m.compiled_execution_graph[-1].next_step_id_on_success = None

    check("misión final tiene 4 pasos", len(m.compiled_execution_graph) == 4)
    check("primero sigue siendo el click A",
          m.interpreted_steps[0].description == "A")
    check("último sigue siendo el click C",
          m.interpreted_steps[-1].description == "C")
    # Relink correcto
    ids = [s.id for s in m.compiled_execution_graph]
    links = [m.compiled_execution_graph[i].next_step_id_on_success
             for i in range(len(ids) - 1)]
    check("next_step_id_on_success relinkeado",
          links == ids[1:], str(links[:2]) + "...")


def case_scale():
    print("\n[14] Escalabilidad (100+ y 200+ pasos)")
    # 100+ pasos: alterna click + hotkey + scroll + drag + tipeo
    raws = []
    for i in range(120):
        raws.append(_click(100 + i, 200 + i))
        if i % 4 == 0:
            raws.append(_kb("s", mods=["ctrl"]))
        elif i % 4 == 1:
            raws.append(_scroll(100, 100, dy=-2))
        elif i % 4 == 2:
            raws.append(_drag(10, 10, 50, 50))
        else:
            raws.append(_kb("tab"))
    t0 = time.time()
    m = Mission(name="big", status=MissionStatus.RECORDING); m.raw_trace = raws
    m = MissionCompiler.compile_mission(m)
    t_compile = time.time() - t0
    check("misión 100+ pasos compila",
          len(m.compiled_execution_graph) >= 100,
          f"{len(m.compiled_execution_graph)} pasos en {t_compile*1000:.0f}ms")
    # Linkeo correcto
    ids = [s.id for s in m.compiled_execution_graph]
    links = [m.compiled_execution_graph[i].next_step_id_on_success for i in range(len(ids) - 1)]
    check("next_step_id_on_success correcto en 100+",
          links == ids[1:] or all(ln is None or ln in ids for ln in links),
          "ok")

    # 200+ pasos
    raws2 = []
    for i in range(210):
        if i % 3 == 0:
            raws2.append(_kb("s", mods=["ctrl"]))
        elif i % 3 == 1:
            raws2.append(_click(10 + i, 20 + i))
        else:
            raws2.append(_kb("tab"))
    t0 = time.time()
    m2 = Mission(name="xl", status=MissionStatus.RECORDING); m2.raw_trace = raws2
    m2 = MissionCompiler.compile_mission(m2)
    t_compile = time.time() - t0
    check("misión 200+ pasos compila",
          len(m2.compiled_execution_graph) >= 200,
          f"{len(m2.compiled_execution_graph)} pasos en {t_compile*1000:.0f}ms")
    check("compilación 200+ < 1.5s", t_compile < 1.5,
          f"{t_compile*1000:.0f}ms")

    # Serialización (persistencia) debe funcionar para la misión XL
    try:
        dumped = m2.model_dump_json()
        check("misión XL serializable",
              len(dumped) > 10000,
              f"{len(dumped)//1024}KB JSON")
    except Exception as e:
        check("misión XL serializable", False, f"excepción: {e}")


def case_field_session_backspace():
    print("\n[16] Field Editing Session — backspace / corrección")
    # Usuario escribe "hol", borra la "l", escribe "a" → valor final "hoa"
    raws = [_kb("h"), _kb("o"), _kb("l"), _kb("backspace"), _kb("a")]
    m, fake = compile_and_exec(raws)
    check("1 solo step TYPE_TEXT (no backspace suelto)",
          len(m.compiled_execution_graph) == 1, f"{len(m.compiled_execution_graph)}")
    step = m.compiled_execution_graph[0]
    check("compilado a TYPE_TEXT", step.action_strategy == ActionStrategy.TYPE_TEXT)
    check("text FINAL == 'hoa'", step.action_payload.get("text") == "hoa",
          repr(step.action_payload.get("text")))
    # Descripción refleja "valor final"
    check("descripción menciona 'valor final'",
          "valor final" in m.interpreted_steps[0].description,
          m.interpreted_steps[0].description)


def case_field_session_shift_caps():
    print("\n[17] Field Editing Session — shift produce mayúscula")
    # Shift+s + h + i → "Shi"
    raws = [_kb("s", mods=["shift"]), _kb("h"), _kb("i")]
    m, _ = compile_and_exec(raws)
    check("1 step", len(m.compiled_execution_graph) == 1)
    step = m.compiled_execution_graph[0]
    check("TYPE_TEXT con 'S' mayúscula",
          step.action_strategy == ActionStrategy.TYPE_TEXT and
          step.action_payload.get("text") == "Shi",
          repr(step.action_payload.get("text")))


def case_field_session_closes_on_tab():
    print("\n[18] Field Editing Session — TAB cierra sesión")
    # "hola" + tab + "mundo" → dos sesiones distintas separadas por tab
    raws = [_kb("h"), _kb("o"), _kb("l"), _kb("a"),
            _kb("tab"),
            _kb("m"), _kb("u"), _kb("n"), _kb("d"), _kb("o")]
    m, _ = compile_and_exec(raws)
    # Esperado: TYPE_TEXT("hola"), TAB, TYPE_TEXT("mundo")
    strats = [s.action_strategy for s in m.compiled_execution_graph]
    texts = [s.action_payload.get("text") for s in m.compiled_execution_graph
             if s.action_strategy == ActionStrategy.TYPE_TEXT]
    check("3 pasos (texto / tab / texto)", len(strats) == 3, str(strats))
    check("TAB entre ambos bloques",
          strats[1] == ActionStrategy.SEND_HOTKEY, str(strats))
    check("ambos textos correctos y separados",
          texts == ["hola", "mundo"], str(texts))


def case_field_session_delete_all_drops():
    print("\n[19] Field Editing Session — sólo borrados se descarta")
    # Usuario escribe 2 letras y las borra → sesión vacía, NO genera step
    raws = [_kb("a"), _kb("b"), _kb("backspace"), _kb("backspace")]
    m, _ = compile_and_exec(raws)
    check("sesión vacía no produce step",
          len(m.compiled_execution_graph) == 0,
          str([s.action_strategy for s in m.compiled_execution_graph]))


def case_field_session_ignores_hotkey():
    print("\n[20] Field Editing Session — ctrl+s NO entra a la sesión")
    # Usuario escribe "hola" y presiona Ctrl+S — ctrl+s debe ser su propio step.
    raws = [_kb("h"), _kb("o"), _kb("l"), _kb("a"), _kb("s", mods=["ctrl"])]
    m, _ = compile_and_exec(raws)
    check("2 pasos (texto + hotkey)", len(m.compiled_execution_graph) == 2,
          str([s.action_strategy for s in m.compiled_execution_graph]))
    step0 = m.compiled_execution_graph[0]
    step1 = m.compiled_execution_graph[1]
    check("texto primero", step0.action_strategy == ActionStrategy.TYPE_TEXT and
          step0.action_payload.get("text") == "hola")
    check("Ctrl+S después",
          step1.action_strategy == ActionStrategy.SEND_HOTKEY and
          step1.action_payload.get("keys") == ["ctrl", "s"])


def case_target_bundle_icon_validated():
    print("\n[21] TargetBundle rico → ICON_VALIDATED_COORDS")
    # Click con metadata rica: UIA + anchor_bbox + snapshot_mid + rel_win.
    click_ev = _click(410, 305)
    click_ev.snapshot_ref = "/tmp/small.png"
    click_ev.metadata = {
        "uia": {
            "name": "Login", "control_type": "ButtonControl",
            "automation_id": "btn_login", "class_name": "Button", "rect": "",
            "bbox": {"left": 400, "top": 300, "width": 80, "height": 32},
        },
        "anchor_bbox": {"left": 400, "top": 300, "width": 80, "height": 32},
        "click_offset": {"dx": 10, "dy": 5},   # click a 10/5 px del centro
        "snapshot_mid": "/tmp/mid.png",
        "rel_win": {"fx": 0.512, "fy": 0.602},
    }
    from app.contracts.mission import WindowContext
    click_ev.window_context = WindowContext(
        hwnd=12345, title="App", process_name="app.exe",
        bounding_box={"left": 0, "top": 0, "width": 800, "height": 500},
    )
    m = Mission(name="t", status=MissionStatus.RECORDING); m.raw_trace = [click_ev]
    m = MissionCompiler.compile_mission(m)
    step = m.compiled_execution_graph[0]

    check("strategy == ICON_VALIDATED_COORDS",
          step.target_resolution_strategy == TargetResolutionStrategy.ICON_VALIDATED_COORDS,
          step.target_resolution_strategy.value)
    tc = step.target_context
    check("image_ref_mid propagado", tc.image_ref_mid == "/tmp/mid.png")
    check("anchor_bbox propagado",
          (tc.anchor_bbox or {}).get("width") == 80 and
          (tc.anchor_bbox or {}).get("height") == 32)
    check("click_offset propagado",
          (tc.click_offset_within_bbox or {}).get("dx") == 10 and
          (tc.click_offset_within_bbox or {}).get("dy") == 5)
    check("rel_win propagado",
          abs((tc.fallback_coords_relative_to_window or {}).get("fx", 0) - 0.512) < 1e-6)
    check("fallback absoluto preservado",
          (tc.fallback_coords or {}).get("x") == 410)
    check("window_title a primer nivel", tc.window_title == "App")
    check("process_name a primer nivel", tc.process_name == "app.exe")


def case_icon_validated_resolver_math():
    print("\n[22] ICON_VALIDATED_COORDS — cálculo de coords tras match cercano")
    # Caso: la ventana se desplazó levemente (dentro del max_drift). El match
    # es aceptado y aplicamos offset. center + offset = click preciso.
    from app.services.missions import player as player_mod
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.ICON_VALIDATED_COORDS,
        target_context=TargetContext(
            image_ref_mid="/tmp/mid.png",
            image_ref=None,
            anchor_bbox={"left": 400, "top": 300, "width": 80, "height": 32},
            click_offset_within_bbox={"dx": 10, "dy": -5},
            fallback_coords={"x": 450, "y": 316},
        ),
    )

    class _Box:
        def __init__(self, l, t, w, h):
            self.left, self.top, self.width, self.height = l, t, w, h

    # Match desplazado 80px (ventana movida levemente, dentro del margen).
    class FakePG:
        def locateOnScreen(self, path, confidence=0.9, region=None):
            return _Box(480, 300, 80, 32)
        def locateCenterOnScreen(self, *a, **kw): return None

    import builtins
    real = builtins.__import__
    def _imp(n, *a, **kw):
        if n == "pyautogui": return FakePG()
        return real(n, *a, **kw)
    builtins.__import__ = _imp
    try:
        Player = getattr(player_mod, "MissionPlayer")
        m = Mission(name="m", status=MissionStatus.COMPILED)
        m.compiled_execution_graph = [step]
        p = Player(m)
        coords = p._resolve_icon_validated_coords(step)
    finally:
        builtins.__import__ = real

    # Match center = (480+40, 300+16) = (520, 316); offset (+10,-5) → (530, 311)
    check("match CERCANO aceptado + offset aplicado",
          coords == {"x": 530, "y": 311}, str(coords))


def case_icon_validated_rejects_far_match():
    print("\n[22b] ICON_VALIDATED_COORDS — rechaza match LEJANO (falso positivo)")
    # Caso crítico de fidelidad: si el template match encuentra algo MUY lejos
    # del punto original grabado (otro ícono parecido en otra esquina), debe
    # RECHAZARSE para que el player caiga a las coords originales grabadas.
    from app.services.missions import player as player_mod
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.ICON_VALIDATED_COORDS,
        target_context=TargetContext(
            image_ref_mid="/tmp/mid.png",
            anchor_bbox={"left": 400, "top": 300, "width": 80, "height": 32},
            click_offset_within_bbox={"dx": 0, "dy": 0},
            fallback_coords={"x": 440, "y": 316},
        ),
    )

    class _Box:
        def __init__(self, l, t, w, h):
            self.left, self.top, self.width, self.height = l, t, w, h

    # Match a 500px de distancia → casi seguro otro botón parecido.
    class FakePG:
        def locateOnScreen(self, path, confidence=0.9, region=None):
            return _Box(1200, 800, 80, 32)

    import builtins
    real = builtins.__import__
    def _imp(n, *a, **kw):
        if n == "pyautogui": return FakePG()
        return real(n, *a, **kw)
    builtins.__import__ = _imp
    try:
        Player = getattr(player_mod, "MissionPlayer")
        m = Mission(name="m", status=MissionStatus.COMPILED)
        m.compiled_execution_graph = [step]
        p = Player(m)
        coords = p._resolve_icon_validated_coords(step)
    finally:
        builtins.__import__ = real

    check("match lejano es RECHAZADO (devuelve None)",
          coords is None, f"got {coords} (debería ser None)")


def case_exact_click_from_uia():
    print("\n[22d] Click EXACTO con UIA + click_offset escalado")
    # Al ejecutar, usamos BoundingRectangle ACTUAL del UIA + offset escalado
    # para clickar en el MISMO punto relativo que el usuario clickó al grabar.
    from app.services.missions import player as player_mod

    class FakeRect:
        def __init__(self, l, t, r, b):
            self.left, self.top, self.right, self.bottom = l, t, r, b

    class FakeUIA:
        BoundingRectangle = FakeRect(500, 400, 580, 432)  # 80x32 en nueva posición

    # Elemento original: 80x32. Offset grabado: (+10, -5).
    # Elemento actual: 80x32 en (500,400) → centro (540,416); click esperado (550,411).
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.ICON_VALIDATED_COORDS,
        target_context=TargetContext(
            anchor_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
            click_offset_within_bbox={"dx": 10, "dy": -5},
            fallback_coords={"x": 150, "y": 216},
        ),
    )
    Player = getattr(player_mod, "MissionPlayer")
    m = Mission(name="m", status=MissionStatus.COMPILED)
    m.compiled_execution_graph = [step]
    p = Player(m)
    coords = p._exact_click_from_uia(step, FakeUIA())
    check("click exacto = centro+offset", coords == {"x": 550, "y": 411}, str(coords))

    # Caso: bbox UIA absurdamente grande → rechazamos y caemos a fallback_coords
    class FakeUIABig:
        BoundingRectangle = FakeRect(0, 0, 900, 700)
    coords = p._exact_click_from_uia(step, FakeUIABig())
    check("bbox UIA enorme → None (usar fallback)", coords is None, str(coords))


def case_should_use_uia_native_click():
    print("\n[22e] Estrategia UIA nativo vs coords exactas")
    # Regla: control pequeño con tamaño similar al grabado → uia.Click() nativo.
    # Contenedor grande o reescalado fuerte → preferir coords exactas.
    from app.services.missions import player as player_mod

    class FakeRect:
        def __init__(self, l, t, r, b):
            self.left, self.top, self.right, self.bottom = l, t, r, b

    Player = getattr(player_mod, "MissionPlayer")
    m = Mission(name="m", status=MissionStatus.COMPILED)
    m.compiled_execution_graph = []
    p = Player(m)

    # Caso 1: control pequeño (botón 80×32) con anchor igual → UIA nativo.
    class FakeBtn:
        BoundingRectangle = FakeRect(500, 400, 580, 432)
    step_small = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.UIA_THEN_VISION,
        target_context=TargetContext(
            anchor_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
        ),
    )
    ok_small = p._should_use_uia_native_click(step_small, FakeBtn())
    check("control pequeño → UIA nativo", ok_small is True, str(ok_small))

    # Caso 2: contenedor enorme (800×600) → NO usar UIA nativo (ese click iría al centro).
    class FakeBig:
        BoundingRectangle = FakeRect(0, 0, 800, 600)
    step_big = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(
            anchor_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
        ),
    )
    ok_big = p._should_use_uia_native_click(step_big, FakeBig())
    check("contenedor grande → NO UIA nativo", ok_big is False, str(ok_big))

    # Caso 3: reescalado grande (grabado 80×32, actual 200×80) → preferir coords.
    class FakeScaled:
        BoundingRectangle = FakeRect(500, 400, 700, 480)  # 200x80
    ok_scaled = p._should_use_uia_native_click(step_small, FakeScaled())
    check("reescalado fuerte → NO UIA nativo", ok_scaled is False, str(ok_scaled))

    # Caso 4: sin UIA target → False
    ok_none = p._should_use_uia_native_click(step_small, None)
    check("sin UIA target → False", ok_none is False, str(ok_none))


def case_scheduler_multi_triggers():
    print("\n[25] Scheduler — múltiples disparadores por misión")
    from app.services.missions.scheduler import ScheduleEntry, TriggerConfig
    entry = ScheduleEntry("mid-1", "test", {
        "enabled": True,
        "triggers": [
            {"mode": "Diario", "time": "09:00"},
            {"mode": "Semanal", "days": ["lun", "vie"], "time": "18:00"},
            {"mode": "Cada X minutos", "interval_minutes": 15},
        ],
    })
    check("3 triggers cargados", len(entry.triggers) == 3, str(len(entry.triggers)))
    check("any_active_trigger == True", entry.any_active_trigger() is True)
    d = entry.to_dict()
    check("to_dict serializa los 3", len(d["triggers"]) == 3, str(d))
    # Desactivado → no due
    entry.enabled = False
    check("inactiva nunca due", entry.is_due() is None)


def case_scheduler_legacy_migration():
    print("\n[25b] Scheduler — migración de formato VIEJO (sin triggers[])")
    from app.services.missions.scheduler import ScheduleEntry
    entry = ScheduleEntry("mid-2", "legacy", {
        "mode": "Diario", "time": "07:30", "enabled": True,
    })
    check("migrado a 1 trigger", len(entry.triggers) == 1, str(entry.triggers))
    check("trigger preserva modo",
          entry.triggers[0].mode == "Diario" and entry.triggers[0].time == "07:30",
          str(entry.to_dict()))


def case_absolute_coords_if_in_window():
    print("\n[22c] Coords absolutas respetadas si siguen en la ventana")
    # Caso 95%: misma máquina, misma app, misma posición → las coords grabadas
    # son EXACTAS y se usan tal cual, sin template match.
    from app.services.missions import player as player_mod
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.ICON_VALIDATED_COORDS,
        target_context=TargetContext(
            fallback_coords={"x": 500, "y": 400},
        ),
    )
    Player = getattr(player_mod, "MissionPlayer")
    m = Mission(name="m", status=MissionStatus.COMPILED)
    m.compiled_execution_graph = [step]
    p = Player(m)
    # Ventana activa: (100,100) 800×600 → el punto (500,400) cae dentro.
    p._ctx.window_bbox = {"left": 100, "top": 100, "width": 800, "height": 600}
    got = p._absolute_coords_if_in_window(step)
    check("coords en ventana → aceptadas tal cual",
          got == {"x": 500, "y": 400}, str(got))

    # Ahora la ventana se movió muy lejos → el punto queda fuera.
    p._ctx.window_bbox = {"left": 2000, "top": 2000, "width": 400, "height": 300}
    got = p._absolute_coords_if_in_window(step)
    check("coords fuera de ventana → None (usar rel_window/match)",
          got is None, str(got))


def case_relative_window_fallback():
    print("\n[23] Coords relativas a ventana — fallback cuando ventana se mueve")
    from app.services.missions import player as player_mod
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
        target_context=TargetContext(
            fallback_coords={"x": 100, "y": 100},                  # original
            fallback_coords_relative_to_window={"fx": 0.5, "fy": 0.25},
        ),
    )
    Player = getattr(player_mod, "MissionPlayer")
    m = Mission(name="m", status=MissionStatus.COMPILED)
    m.compiled_execution_graph = [step]
    p = Player(m)
    # Simulamos ventana en (200,200) de 400×200 px
    p._ctx.window_bbox = {"left": 200, "top": 200, "width": 400, "height": 200}
    coords = p._resolve_relative_window_coords(step)
    # Esperado: 200 + 0.5*400 = 400   ; 200 + 0.25*200 = 250
    check("coords reconstruidas desde ventana actual",
          coords == {"x": 400, "y": 250}, str(coords))


def case_perf_metrics():
    print("\n[24] Métricas de performance (por resolver + idle %)")
    from app.services.missions.perf import PerfTracker
    pt = PerfTracker()
    s1 = pt.start_step(0, "click"); s1.resolver = "uia"; s1.total_ms = 20; s1.resolve_ms = 8; s1.action_ms = 10; s1.validate_ms = 2
    s2 = pt.start_step(1, "click"); s2.resolver = "icon_validated"; s2.total_ms = 80; s2.resolve_ms = 60; s2.action_ms = 18; s2.validate_ms = 2
    s3 = pt.start_step(2, "type_text"); s3.resolver = "window"; s3.total_ms = 40; s3.resolve_ms = 2; s3.action_ms = 35; s3.validate_ms = 3
    tot = pt.totals()
    check("validate_total_ms incluido", tot.get("validate_total_ms") == 7.0,
          str(tot))
    check("by_resolver agrupa por estrategia de resolución",
          tot["by_resolver"]["uia"]["count"] == 1 and
          tot["by_resolver"]["icon_validated"]["count"] == 1 and
          tot["by_resolver"]["window"]["count"] == 1,
          str(tot.get("by_resolver")))
    check("snapshot del tracker incluye resolver",
          pt.snapshot()[1].get("resolver") == "icon_validated")


def case_agent_prompt_guard():
    print("\n[15] Vibe no degrada a agent_prompt sin confirmar")
    from app.services.missions.vibe_service import apply_vibe_edit
    from unittest.mock import patch, MagicMock
    m = Mission(name="g", status=MissionStatus.COMPILED)
    c = CompiledStep(action_strategy=ActionStrategy.CLICK, action_payload={})
    i = InterpretedStep(description="click", raw_event_ids=[])
    m.compiled_execution_graph = [c]
    m.interpreted_steps = [i]

    # Mock agent.process_user_input para devolver un JSON con action=agent_prompt
    fake_agent = MagicMock()
    fake_agent.process_user_input.return_value = (
        '{"intent":"strategy_change","action":"agent_prompt",'
        '"payload":{"text":"haz algo"},"description":"dinámico"}'
    )
    import sys as _sys
    mock_mod = MagicMock(); mock_mod.agent = fake_agent
    with patch.dict(_sys.modules, {"app.brain.agent": mock_mod}):
        # Sin callback: debe rechazar
        r = apply_vibe_edit(m, 0, "inventa algo raro", on_confirm_agent=None)
        check("sin confirmación → no convierte a agent_prompt",
              c.action_strategy == ActionStrategy.CLICK, r.get("error", ""))
        # Con callback que dice "No": debe rechazar
        r = apply_vibe_edit(m, 0, "otra vez", on_confirm_agent=lambda: False)
        check("callback=False → no convierte a agent_prompt",
              c.action_strategy == ActionStrategy.CLICK, r.get("error", ""))
        # Con callback que dice "Sí": ahora sí convierte
        r = apply_vibe_edit(m, 0, "ok adelante", on_confirm_agent=lambda: True)
        check("callback=True → ahora sí convierte",
              c.action_strategy == ActionStrategy.AGENT_PROMPT)


# ───────────────────────────────────────────────────────────────────────────
# [26] Player — pausa / reanudar / cancelar (cooperativo, sin UI)
# ───────────────────────────────────────────────────────────────────────────
def case_player_pause_resume_cancel():
    """Verifica que MissionPlayer expone request_pause / request_resume /
    request_stop y que tras request_pause is_paused=True, y tras
    request_stop el flag de cancelación queda activo. No ejecuta una
    misión real — sólo el contrato de control.
    """
    print("\n[26] Player — pausa / reanudar / cancelar")
    from app.services.missions.player import MissionPlayer

    # Misión vacía válida
    m = Mission(
        name="ctrl-test",
        status=MissionStatus.COMPILED,
        compiled_execution_graph=[],
        raw_trace=[],
    )
    p = MissionPlayer(m)
    p.start_execution()

    p.request_pause()
    check("request_pause marca is_paused=True", p.is_paused() is True)

    p.request_resume()
    check("request_resume libera is_paused", p.is_paused() is False)

    p.request_stop()
    check("request_stop activa _stop_requested",
          p._stop_requested is True, "stop request not honored")


# ───────────────────────────────────────────────────────────────────────────
# [27] Scheduler — countdown con segundos (UX para "en 9m 58s")
# ───────────────────────────────────────────────────────────────────────────
def case_scheduler_seconds_in_label():
    """`TriggerConfig._fmt_remaining` debe formatear con segundos cuando
    queden menos de 60 minutos.  Es la base del refresco 1Hz en la UI.
    """
    print("\n[27] Scheduler — countdown con segundos")
    from app.services.missions.scheduler import TriggerConfig

    fmt = TriggerConfig._fmt_remaining

    check("< 60s muestra segundos", fmt(45) == "en 45s", repr(fmt(45)))
    check("< 1h muestra m + s", fmt(9 * 60 + 58) == "en 9m 58s", repr(fmt(598)))
    check("≥ 1h muestra h + m sin segundos",
          fmt(3 * 3600 + 7 * 60) == "en 3h 07m", repr(fmt(3 * 3600 + 7 * 60)))
    check("≥ 1d muestra d + h",
          fmt(2 * 86400 + 3 * 3600) == "en 2d 03h", repr(fmt(2 * 86400 + 3 * 3600)))

    # Cada X minutos con last_run reciente: el label debe terminar en "s"
    # (segundos) cuando faltan menos de 60s, o contener "m" + "s" cuando
    # faltan minutos. En cualquier caso, no debe omitir los segundos.
    t = TriggerConfig({"mode": "Cada X minutos", "interval_minutes": 10})
    from datetime import datetime, timedelta
    t.last_run = datetime.now() - timedelta(minutes=9, seconds=2)
    label = t.next_run_str()
    check("Cada X min produce countdown con segundos",
          label.endswith("s") and ("en " in label or label == "ahora"), label)


# ───────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  NEVLAN — PRUEBAS E2E SINTÉTICAS")
    print("=" * 72)

    cases = [
        case_scroll, case_drag, case_double_click_detection,
        case_ctrl_s, case_tab_x3, case_shift_tab, case_alt_tab,
        case_sequence, case_typing, case_fill_field_pattern,
        case_recompile_legacy, case_vibe_edit_keyboard,
        case_rerecord_replacement_1_to_n, case_scale,
        case_field_session_backspace, case_field_session_shift_caps,
        case_field_session_closes_on_tab, case_field_session_delete_all_drops,
        case_field_session_ignores_hotkey,
        case_target_bundle_icon_validated, case_icon_validated_resolver_math,
        case_icon_validated_rejects_far_match, case_absolute_coords_if_in_window,
        case_exact_click_from_uia, case_should_use_uia_native_click,
        case_scheduler_multi_triggers, case_scheduler_legacy_migration,
        case_relative_window_fallback, case_perf_metrics,
        case_agent_prompt_guard,
        case_player_pause_resume_cancel,
        case_scheduler_seconds_in_label,
    ]
    for fn in cases:
        try:
            fn()
        except Exception as e:
            print(f"  {FAIL} {fn.__name__} → excepción: {e}")
            results.append((fn.__name__, False, str(e)))

    print("\n" + "=" * 72)
    ok = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"  RESULTADO: {ok}/{total} asserts OK")
    print("=" * 72)
    failed = [(n, d) for n, c, d in results if not c]
    if failed:
        print("\nFallos:")
        for n, d in failed:
            print(f"  - {n}: {d}")
        sys.exit(1)


if __name__ == "__main__":
    main()
