from app.skills.tools.web import web_navigate, web_scan_context, web_close_browser
import time

def test_visual_cortex():
    print("🚀 Iniciando prueba de Visual Cortex...")
    
    # 1. Navegar a Google (Página con Input y Botones)
    print("🌐 Navegando a Google...")
    web_navigate("test_nav", "https://www.google.com")
    
    # 2. Escanear Contexto
    print("👁️ Escaneando DOM...")
    result = web_scan_context("test_scan")
    
    if result.success:
        print("\n✅ ÉXITO: Contexto Visual Detectado:\n")
        print("------------------------------------------------")
        print(result.result)
        print("------------------------------------------------")
        
        # Validaciones básicas
        if "INPUTS:" in result.result and "Buttons:" in result.result: # Case insensitive check below
            print("✅ Inputs detectados")
        else:
            if "INPUTS:" in result.result: print("✅ Inputs detectados")
            if "BUTTONS:" in result.result: print("✅ Botones detectados")
            
    else:
        print(f"\n❌ ERROR: {result.error}")

    # 3. Prueba de Video (Simulada navegando a youtube si es posible, o w3schools)
    # Por ahora google es suficiente para probar la inyección de JS.
    
    # web_close_browser("test_end")

if __name__ == "__main__":
    test_visual_cortex()
