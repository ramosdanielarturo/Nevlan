"""
ArthurOS Skills - Desktop Tools (Window Management)
---------------------------------------------------
Control de ventanas robusto: Listar, Enfocar, Minimizar, Cerrar.
Vital para asegurar que las acciones de mouse/teclado vayan a la app correcta.
"""
from __future__ import annotations
import time
from app.contracts.tool_result import ToolResult
from app.skills.registry import registry

try:
    import pygetwindow as gw
except ImportError:
    gw = None

def _check_gw(call_id):
    if gw is None:
        return ToolResult(call_id=call_id, success=False, error="pygetwindow no instalado.")
    return None

@registry.register(name="desktop.window.list")
def desktop_list_windows(call_id: str) -> ToolResult:
    """Lista los títulos de todas las ventanas visibles."""
    err = _check_gw(call_id)
    if err: return err
    try:
        titles = [w.title for w in gw.getAllWindows() if w.title]
        return ToolResult(call_id=call_id, result=str(titles[:20])) # Limitamos a 20 para no saturar
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="desktop.window.focus")
def desktop_focus_window(call_id: str, title_snippet: str) -> ToolResult:
    """
    IMPORTANTE: Trae una ventana al frente. 
    Usar ANTES de escribir o hacer clic en una app.
    """
    err = _check_gw(call_id)
    if err: return err
    try:
        windows = gw.getWindowsWithTitle(title_snippet)
        if not windows:
            return ToolResult(call_id=call_id, success=False, error=f"Ventana no encontrada: {title_snippet}")
        
        win = windows[0]
        if win.isMinimized:
            win.restore()
        win.activate()
        time.sleep(0.5) # Esperar a que la animación de Windows termine
        return ToolResult(call_id=call_id, result=f"Ventana enfocada: {win.title}")
    except Exception as e:
        # En Windows a veces activate() falla si ya tienes el foco, lo ignoramos si es eso
        return ToolResult(call_id=call_id, result=f"Intento de foco en '{title_snippet}' realizado (Check visual).")

@registry.register(name="desktop.window.close")
def desktop_close_window(call_id: str, title_snippet: str) -> ToolResult:
    """Cierra una aplicación."""
    err = _check_gw(call_id)
    if err: return err
    try:
        windows = gw.getWindowsWithTitle(title_snippet)
        if not windows:
            return ToolResult(call_id=call_id, success=False, error="Ventana no encontrada.")
        windows[0].close()
        return ToolResult(call_id=call_id, result=f"Ventana cerrada: {windows[0].title}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="desktop.scroll")
def desktop_scroll(call_id: str, direction: str = "down", amount: int = 1, speed: str = "normal") -> ToolResult:
    """
    Realiza scroll en la ventana activa del escritorio.
    - direction: 'down', 'up' (approx 100px / click per default)
    - amount: número de 'clicks' de rueda o magnitud relativa.
    - speed: 'normal', 'slow' (afecta delay entre ticks)
    """
    import pyautogui
    try:
        clicks = int(amount)
        if direction == "down":
            clicks = -clicks
        
        # PyAutoGui scroll: positive = up, negative = down
        
        interval = 0.05
        if speed == "slow": interval = 0.2
        elif speed == "fast": interval = 0.01

        # Smooth scroll loop
        # Split heavy scrolls into chunks
        chunk_size = 1 # 1 click per tick for smoothness
        if abs(clicks) > 10 and speed == "fast": chunk_size = 5
        
        steps = abs(clicks)
        sign = -1 if clicks < 0 else 1
        
        for _ in range(0, steps, chunk_size):
            # pyautogui.scroll(clicks)
            # Windows: 1 click = 120 usually, but pyautogui abstract it? 
            # In pyautogui docs: scroll(clicks). On windows, 1 click is often just 1 unit of scroll which is small.
            # Let's assume input amount is "notches".
            pyautogui.scroll(int(sign * 100 * chunk_size)) 
            time.sleep(interval)
            
        return ToolResult(call_id=call_id, result=f"Desktop Scroll {direction} ({abs(clicks)} steps)")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="desktop.scan_context")
def desktop_scan_context(call_id: str) -> ToolResult:
    """Escanea el árbol de accesibilidad (UI Automation) de la VENTANA ACTIVA."""
    try:
        import uiautomation as auto
    except ImportError:
        return ToolResult(call_id=call_id, success=False, error="Librería 'uiautomation' no instalada.")

    try:
        # 1. Obtener ventana activa real
        window = auto.GetForegroundControl().GetTopLevelControl()
        if not window.Exists(0, 0):
            return ToolResult(call_id=call_id, result="No active window found.")

        title = window.Name
        classname = window.ClassName
        
        # 2. Recorrer árbol (BFS limitado para velocidad)
        controls = []
        
        def collect_elements(control, depth):
            if depth > 3: return
            
            children = control.GetChildren()
            for child in children:
                if not child.IsOffscreen:
                    ctype = child.ControlTypeName
                    name = child.Name.strip()
                    auto_id = child.AutomationId
                    
                    is_interesting = ctype in ["ButtonControl", "EditControl", "ListControl", "ListItemControl", "HyperlinkControl", "CheckBoxControl"]
                    
                    if is_interesting and (name or auto_id):
                        controls.append(f"- [{ctype.replace('Control','')}] Name: '{name}' ID: '{auto_id}'")
                    
                    collect_elements(child, depth + 1)

        collect_elements(window, 0)
        
        summary = [f"APP: {title} (Class: {classname})"]
        if controls:
            summary.append(f"CONTROLS ({len(controls)} found):")
            summary.extend(controls[:50])
            if len(controls) > 50:
                summary.append(f"... (+{len(controls)-50} more)")
        else:
            summary.append("No common interactive controls found.")

        return ToolResult(call_id=call_id, result="\n".join(summary))

    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

# Helpers for finding UIA controls
def _find_control(name: str = None, automation_id: str = None, timeout: int = 3):
    """
    Finds a control in the active window hierarchy. 
    Optimized for speed: Uses BFS with limited depth/count to avoid hanging.
    """
    import uiautomation as auto
    import time
    
    # Set global timeout to something small for this operation
    auto.SetGlobalSearchTimeout(0.5) 
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        window = auto.GetForegroundControl().GetTopLevelControl()
        if not window.Exists(0, 0): 
            time.sleep(0.5)
            continue
            
        # Strategy:
        # 1. Try explicit search on window (usually slow if tree is big)
        # 2. Manual BFS to control depth and avoid freezing
        
        queue = [(window, 0)]
        checked = 0
        MAX_CHECK = 100 # Max elements to check
        MAX_DEPTH = 3
        
        target_found = None
        
        while queue and checked < MAX_CHECK:
            ctrl, depth = queue.pop(0)
            checked += 1
            
            # Check match
            # Note: getattr is safer than direct property access if elements are stale, though UIA handles it mostly.
            try:
                c_name = ctrl.Name
                c_id = ctrl.AutomationId
                
                if (automation_id and c_id == automation_id) or \
                   (name and c_name and name.lower() in c_name.lower()): # Fuzzy case-insensitive name match
                    target_found = ctrl
                    break
            except:
                pass # Stale element
            
            if depth < MAX_DEPTH:
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        queue.append((child, depth + 1))
                except:
                    pass
                    
        if target_found:
            return target_found
            
        time.sleep(0.5)
        
    return None

@registry.register(name="desktop.uia.click")
def desktop_uia_click(call_id: str, name: str = "", automation_id: str = "") -> ToolResult:
    try:
        import uiautomation as auto
    except ImportError:
        return ToolResult(call_id=call_id, success=False, error="uiautomation missing.")

    if not name and not automation_id:
        return ToolResult(call_id=call_id, success=False, error="Provide 'name' or 'automation_id'.")

    try:
        # Optimistic approach: try finding strict first, then relax?
        # For now, rely on our optimized _find_control
        control = _find_control(name, automation_id, timeout=4)
        
        if control:
            # Check if offscreen
            if control.IsOffscreen:
                 # Try to bring into view?
                 try: control.ScrollIntoView()
                 except: pass
                 
            try:
                control.SetFocus()
            except: pass # Sometimes focus fails but click works
            
            # Click
            # simulateMove=False uses UIA InvokePattern or internal events if possible, 
            # or jumps mouse instantly. True moves mouse visibly.
            # User complained about slowness, so False is faster.
            control.Click(simulateMove=False) 
            return ToolResult(call_id=call_id, result=f"Clicked control: {name or automation_id}")
        else:
            return ToolResult(call_id=call_id, success=False, error=f"Control not found: {name or automation_id}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="desktop.uia.set_text")
def desktop_uia_set_text(call_id: str, value: str, name: str = "", automation_id: str = "") -> ToolResult:
    try:
        import uiautomation as auto
    except ImportError:
        return ToolResult(call_id=call_id, success=False, error="uiautomation missing.")

    try:
        control = _find_control(name, automation_id)
        if control:
            control.SetFocus()
            if control.GetCurrentPattern(auto.PatternId.ValuePattern):
                control.GetValuePattern().SetValue(value)
            else:
                 control.SendKeys(value)
            return ToolResult(call_id=call_id, result=f"Set text '{value}' in: {name or automation_id}")
        else:
            return ToolResult(call_id=call_id, success=False, error=f"Control not found: {name or automation_id}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))