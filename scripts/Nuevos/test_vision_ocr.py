import time
from app.skills.tools.desktop_automation import vision_find_text

def test_ocr():
    print("👁️ Probando Visión OCR (Tesseract)...")
    
    # Buscamos algo que seguro está en pantalla, ej: 'Desktop' o del propio editor si está visible
    # O simplemente un texto común de Windows como "Recycle" o "Papelera"
    text_to_find = "Recycle" 
    
    start = time.time()
    res = vision_find_text("test_ocr", text_to_find)
    end = time.time()
    
    if res.success:
        print(f"✅ ÉXITO: {res.result}")
        print(f"⏱️ Tiempo: {end - start:.2f}s")
    else:
        print(f"❌ FALLO: {res.error}")
        if "Tesseract-OCR binary not found" in res.error:
            print("👉 El usuario debe instalar Tesseract. Esto es esperado si no lo hizo aún.")

if __name__ == "__main__":
    test_ocr()
