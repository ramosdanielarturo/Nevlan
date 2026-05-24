import time
import pygetwindow as gw
from app.skills.tools.web import web_scan_context
from app.skills.tools.desktop import desktop_scan_context

def test_amphibious():
    print("🐸 Iniciando prueba de Modo Anfibio...")
    
    # 1. Detectar ventana
    window = gw.getActiveWindow()
    if not window:
        print("❌ No active window detected.")
        return

    title = window.title
    print(f"🖥️ Ventana Activa: '{title}'")
    
    # 2. Decisión
    is_browser = any(b in title.lower() for b in ["chrome", "edge", "firefox", "brave", "opera"])
    
    if is_browser:
        print("🌐 Detectado como NAVEGADOR. Ejecutando scan web...")
        res = web_scan_context("test_web")
    else:
        print("💻 Detectado como APP NATIVA. Ejecutando scan desktop...")
        res = desktop_scan_context("test_desktop")
        
    # 3. Resultado
    if res.success:
        print("\n✅ ÉXITO - Contexto Capturado:\n")
        print("------------------------------------------------")
        print(res.result)
        print("------------------------------------------------")
    else:
        print(f"\n❌ ERROR: {res.error}")

if __name__ == "__main__":
    test_amphibious()
