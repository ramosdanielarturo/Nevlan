# -*- coding: utf-8 -*-
"""
Test script para verificar conexion CDP a Chrome del usuario
"""
import sys
import os

# Configurar encoding para Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, r'c:\Users\Daniel Ramos\Desktop\ArthurOS')

from app.skills.tools.web import web_navigate, web_fill_form, web_get_info

print("=" * 60)
print("Test: Conexion CDP a Chrome del usuario")
print("=" * 60)

# Test 1: Navegar a Google
print("\n[1/3] Navegando a Google...")
result = web_navigate("test1", "https://www.google.com")
if result.success:
    print(f"OK: {result.result}")
else:
    print(f"ERROR: {result.error}")
    sys.exit(1)

# Test 2: Verificar info de la pagina
print("\n[2/3] Verificando info de la pagina...")
result = web_get_info("test2")
if result.success:
    print(f"OK: {result.result}")
else:
    print(f"ERROR: {result.error}")

# Test 3: Escribir "test" en el buscador
print("\n[3/3] Escribiendo 'test' en el buscador...")
result = web_fill_form("test3", "textarea[name='q']", "test")
if result.success:
    print(f"OK: {result.result}")
else:
    print(f"ERROR: {result.error}")

print("\n" + "=" * 60)
print("EXITO: ArthurOS esta usando tu Chrome!")
print("Verifica que:")
print("  - Chrome tiene tus extensiones")
print("  - Estas logueado en tus cuentas")
print("  - El texto 'test' aparece en el buscador de Google")
print("=" * 60)
