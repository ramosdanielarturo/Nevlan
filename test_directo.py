import os

from dotenv import load_dotenv
from openai import OpenAI
import httpx

load_dotenv()
MI_CLAVE = os.getenv("OPENAI_API_KEY", "sk-proj-PEGAR-AQUI")

print(f"🔑 Probando con clave de entorno: {MI_CLAVE[:8]}...")

if not MI_CLAVE or MI_CLAVE.startswith("sk-proj-PEGAR"):
    print("❌ ERROR: Configura OPENAI_API_KEY en .env antes de ejecutar este script.")
    exit()

try:
    print("📡 Intentando conectar a OpenAI...")

    client = OpenAI(
        api_key=MI_CLAVE,
        timeout=httpx.Timeout(20.0, connect=10.0),
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Si lees esto, responde: 'Conexión Exitosa'"}],
    )

    print("\n✅ ¡FUNCIONA! El problema NO es tu clave ni tu internet.")
    print(f"🤖 OpenAI dice: {response.choices[0].message.content}")
    print("\n👉 CONCLUSIÓN: Tu .env o tu terminal tienen 'basura' en memoria. Reinicia tu PC.")

except Exception as e:
    print("\n❌ FALLO REAL:")
    print(e)
    print("\n👉 DIAGNÓSTICO:")
    if "401" in str(e):
        print("Tu clave está mal copiada o revocada.")
    elif "ConnectError" in str(e) or "SSL" in str(e):
        print("Tu antivirus, Firewall o VPN está bloqueando a Python.")
