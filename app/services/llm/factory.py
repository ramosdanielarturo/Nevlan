"""
ArthurOS Services - LLM Factory (DEBUG MODE)
--------------------------------------------
"""
from __future__ import annotations
import os
from typing import Optional
from app.core.config import settings
from app.services.llm.base import LLMProvider

_provider: Optional[LLMProvider] = None

def get_llm_provider(force_reload: bool = False) -> LLMProvider:
    global _provider
    if _provider is not None and not force_reload:
        return _provider

    # --- INICIO DEL ESPÍA ---
    print("\n" + "="*40)
    print("DIAGNOSTICO DE CLAVE EN RUNTIME:")
    
    # Buscamos de dónde viene la clave
    env_key = os.getenv("OPENAI_API_KEY")
    settings_key = getattr(settings, "OPENAI_API_KEY", None)
    
    final_key = settings_key or env_key
    
    if final_key:
        print(f"Clave que intentare usar: {final_key[:15]}...******")
        if final_key.startswith("sk-proj-H5"):
             print("ALERTA ROJA: ESTAS USANDO LA CLAVE VIEJA/SABOTEADA.")
             print("Buscaremos quien la puso ahi...")
        elif final_key.startswith("sk-proj-"):
             print("La clave parece correcta (empieza por sk-proj-)")
    else:
        print("ERROR: No encuentro ninguna clave.")
    print("="*40 + "\n")
    # --- FIN DEL ESPÍA ---

    preferred = (getattr(settings, "LLM_PROVIDER", None) or os.getenv("ARTHUROS_LLM_PROVIDER") or "").strip().lower()

    groq_key = getattr(settings, "GROQ_API_KEY", None) or os.getenv("GROQ_API_KEY")
    openai_key = final_key

    if preferred == "openai" or openai_key:
        from app.services.llm.openai import OpenAIProvider
        # Forzamos pasar la clave explícita para asegurar que usa la que vimos
        _provider = OpenAIProvider(api_key=openai_key)
        return _provider

    if preferred == "groq" or groq_key:
        from app.services.llm.groq import GroqProvider
        _provider = GroqProvider(api_key=groq_key)
        return _provider

    raise RuntimeError("No hay proveedores LLM configurados.")