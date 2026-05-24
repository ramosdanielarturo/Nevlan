import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

print("-" * 50)
print(f"🔑 Clave detectada: {api_key[:10]}...******" if api_key else "❌ NO SE DETECTÓ CLAVE")
print("-" * 50)

if not api_key or api_key.startswith("sk-proj-PEGAR"):
    print("⚠️  ERROR: Configura OPENAI_API_KEY en el archivo .env")
    exit()

try:
    print("📡 Conectando con OpenAI...")
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hola, ¿me escuchas?"}],
    )
    print("✅ ¡ÉXITO! OpenAI respondió:")
    print(f"🤖 {response.choices[0].message.content}")
except Exception as e:
    print(f"❌ FALLO DE CONEXIÓN: {e}")
