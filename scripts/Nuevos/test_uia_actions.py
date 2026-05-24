import time
import subprocess
from app.skills.tools.desktop import desktop_uia_set_text, desktop_uia_click, desktop_scan_context

def test_uia_actions():
    print("🧪 Iniciando prueba de Acciones Nativas (UIA)...")
    
    # 1. Abrir Notepad
    print("🚀 Lanzando Notepad...")
    subprocess.Popen("notepad.exe")
    time.sleep(2)
    
    # 2. Escanear
    print("👁️ Escaneando contexto...")
    scan = desktop_scan_context("test_scan")
    if scan.success:
        print(f"Contexto: {scan.result[:200]}...")
    else:
        print(f"❌ Error scan: {scan.error}")
        return

    # 3. Escribir Texto (Document Control)
    print("⌨️ Escribiendo texto...")
    # Notepad nuevo usa 'Text Editor' o 'Document' como auto_id o nombre
    # Probamos genérico
    res = desktop_uia_set_text("test_type", "Hola ArthurOS! Esto es una prueba de UIA.", automation_id="Editor") 
    # Nota: El ID 'Editor' funciona en W11 Notepad nuevo. En viejo es '15'.
    
    if not res.success:
        print(f"⚠️ Falló escritura por ID 'Editor', intentando por ClassName 'Edit' (Notepad viejo)...")
        # Fallback manual no implementado en tool, pero el usuario podría reintentar.
        # Por ahora solo reportamos.
        print(f"Error: {res.error}")
    else:
        print(f"✅ Escritura exitosa: {res.result}")
        
    # 4. Clic en Menú Archivo
    print("🖱️ Clic en 'Archivo'...")
    res = desktop_uia_click("test_click", name="File") # English Windows?
    if not res.success:
         res = desktop_uia_click("test_click", name="Archivo") # Spanish Windows
    
    if res.success:
        print(f"✅ Clic exitoso: {res.result}")
    else:
        print(f"❌ Error Clic: {res.error}")

if __name__ == "__main__":
    test_uia_actions()
