"""
ArthurOS Skills - Desktop Automation (Eagle Eye Edition)
--------------------------------------------------------
Control de Mouse/Teclado + Visión Computarizada Robusta.
Usa OpenCV para detectar botones aunque cambien de tamaño o color.
"""
from typing import Optional, List, Tuple
from pathlib import Path
import time
import math

from app.contracts.tool_result import ToolResult
from app.core.config import settings
from app.skills.registry import registry

# --- IMPORTS DE VISIÓN (CRÍTICOS) ---
try:
    import pyautogui
    import cv2
    import numpy as np
except ImportError:
    pyautogui = None
    cv2 = None
    np = None

def _require_pyautogui(call_id: str) -> Optional[ToolResult]:
    if pyautogui is None or cv2 is None:
        return ToolResult(call_id=call_id, success=False, error="Faltan librerías. Ejecuta: pip install pyautogui opencv-python numpy")
    pyautogui.FAILSAFE = True
    return None

# --- HERRAMIENTAS BÁSICAS (MANOS) ---

@registry.register(name="desktop.screen.size")
def get_screen_size(call_id: str) -> ToolResult:
    err = _require_pyautogui(call_id)
    if err: return err
    w, h = pyautogui.size()
    return ToolResult(call_id=call_id, result=f"Width: {w}, Height: {h}")

@registry.register(name="desktop.mouse.move")
def mouse_move(call_id: str, x: int, y: int) -> ToolResult:
    err = _require_pyautogui(call_id)
    if err: return err
    pyautogui.moveTo(x, y, duration=0.5)
    return ToolResult(call_id=call_id, result=f"Mouse en ({x}, {y})")

@registry.register(name="desktop.mouse.click")
def mouse_click(call_id: str, x: int = None, y: int = None) -> ToolResult:
    err = _require_pyautogui(call_id)
    if err: return err
    if x is not None and y is not None:
        pyautogui.moveTo(x, y)
    pyautogui.click()
    return ToolResult(call_id=call_id, result="Click realizado")

@registry.register(name="desktop.keyboard.type")
def keyboard_type(call_id: str, text: str) -> ToolResult:
    err = _require_pyautogui(call_id)
    if err: return err
    pyautogui.write(text, interval=0.05)
    return ToolResult(call_id=call_id, result=f"Escribí: {text}")

@registry.register(name="desktop.keyboard.hotkey")
def keyboard_hotkey(call_id: str, keys: str) -> ToolResult:
    """Ej: 'ctrl,c' o 'win,r'."""
    err = _require_pyautogui(call_id)
    if err: return err
    try:
        key_list = [k.strip() for k in keys.split(",")]
        pyautogui.hotkey(*key_list)
        return ToolResult(call_id=call_id, result=f"Hotkey: {keys}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

# --- MOTOR DE VISIÓN "OJOS DE ÁGUILA" ---

def _find_image_robust(image_path: str) -> Tuple[Optional[Tuple[int, int]], float, float]:
    """
    Busca imagen ignorando color y probando varios tamaños (80% a 120%).
    Retorna: (Coordenadas, Confianza, Escala)
    """
    # 1. Resolver ruta (Root > assets > sandbox)
    target_path = Path(image_path)
    if not target_path.exists():
        target_path = Path("assets") / image_path
    
    if not target_path.exists():
        return None, 0.0, 1.0

    # 2. Captura y Conversión
    try:
        screenshot = pyautogui.screenshot()
        screen_mat = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        template = cv2.imread(str(target_path), 0) # 0 = Grayscale
    except Exception:
        return None, 0.0, 1.0
        
    if template is None:
        return None, 0.0, 1.0

    best_match = -1
    best_loc = None
    best_scale = 1.0

    # 3. Bucle Multi-Escala
    # Probamos 10 tamaños diferentes para encontrar el botón
    for scale in np.linspace(0.8, 1.2, 10):
        try:
            # Redimensionar template
            w_original, h_original = template.shape[::-1]
            new_width = int(w_original * scale)
            new_height = int(h_original * scale)
            
            # Si se hace muy pequeño o muy grande, saltar
            if new_width < 10 or new_height < 10: continue
            if new_height > screen_mat.shape[0] or new_width > screen_mat.shape[1]: continue

            resized = cv2.resize(template, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            # Matching
            res = cv2.matchTemplate(screen_mat, resized, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

            if max_val > best_match:
                best_match = max_val
                # Centro del botón encontrado
                center_x = max_loc[0] + new_width // 2
                center_y = max_loc[1] + new_height // 2
                best_loc = (center_x, center_y)
                best_scale = scale
        except Exception:
            continue

    return best_loc, best_match, best_scale

@registry.register(name="vision.click_image")
def vision_click_image(call_id: str, image_name: str, timeout: int = 5) -> ToolResult:
    """
    Busca una imagen de forma ROBUSTA (ignorando color y variaciones de tamaño) y hace clic.
    Espera hasta 'timeout' segundos.
    """
    err = _require_pyautogui(call_id)
    if err: return err

    start_time = time.time()
    
    while time.time() - start_time < timeout:
        loc, confidence, scale = _find_image_robust(image_name)
        
        # UMBRAL DE DECISIÓN
        # > 0.85 = Seguro (Clic)
        # 0.70 - 0.85 = Duda (Movemos mouse para que usuario confirme, o arriesgamos)
        
        if loc and confidence > 0.8:
            pyautogui.moveTo(loc[0], loc[1], duration=0.3)
            pyautogui.click()
            return ToolResult(call_id=call_id, result=f"Click en '{image_name}' (Confianza: {int(confidence*100)}%, Escala: {scale:.2f}x)")
        
        time.sleep(0.5)

@registry.register(name="vision.find_text")
def vision_find_text(call_id: str, text: str) -> ToolResult:
    """Find coordinates of text on screen using OCR (pytesseract)."""
    err = _require_pyautogui(call_id)
    if err: return err
    
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ToolResult(call_id=call_id, success=False, error="pytesseract/Pillow missing.")

    try:
        # Take screenshot
        screenshot = pyautogui.screenshot()
        
        # OCR
        # We use 'eng+osd' by default, or maybe just 'eng'
        data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
        
        found_coords = []
        n_boxes = len(data['text'])
        target = text.lower()
        
        for i in range(n_boxes):
            detected_text = data['text'][i].strip().lower()
            if target in detected_text:
                x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                center_x = x + w // 2
                center_y = y + h // 2
                found_coords.append((center_x, center_y))
                
        if found_coords:
            # Return first match
            cx, cy = found_coords[0]
            return ToolResult(call_id=call_id, result=f"Text '{text}' found at ({cx}, {cy})")
        
        return ToolResult(call_id=call_id, success=False, error=f"Text '{text}' not found on screen.")
        
    except Exception as e:
        if "tesseract is not installed" in str(e).lower() or "file not found" in str(e).lower():
             return ToolResult(call_id=call_id, success=False, error="Tesseract-OCR binary not found in PATH. Install from: https://github.com/UB-Mannheim/tesseract/wiki")
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="vision.click_text")
def vision_click_text(call_id: str, text: str) -> ToolResult:
    """Find text using OCR and click on it."""
    # Reuse find logic
    res = vision_find_text(call_id, text)
    if not res.success:
        return res
    
    # Parse coordinates from result string "Text '...' found at (x, y)"
    try:
        import re
        match = re.search(r"\((\d+), (\d+)\)", res.result)
        if match:
             x, y = int(match.group(1)), int(match.group(2))
             pyautogui.moveTo(x, y, duration=0.5)
             pyautogui.click()
             return ToolResult(call_id=call_id, result=f"Clicked text '{text}' at ({x}, {y})")
        return ToolResult(call_id=call_id, success=False, error="Could not parse coordinates from find_text result.")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="vision.screenshot")
def take_screenshot(call_id: str, filename: str = "screen_evidence.png") -> ToolResult:
    err = _require_pyautogui(call_id)
    if err: return err
    try:
        path = Path("assets") / filename 
        # Si assets no existe, usar root
        if not path.parent.exists(): path = Path(filename)
            
        pyautogui.screenshot(str(path))
        return ToolResult(call_id=call_id, result=f"Captura guardada en: {path}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))