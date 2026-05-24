"""Carga segura de entorno para Nevlan — sin secretos incrustados en código."""
from __future__ import annotations

import os


def bootstrap_llm_environment() -> None:
    """Carga ``.env`` si existe python-dotenv. No escribe valores reales aquí."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def configure_llm_fallback_from_environment() -> None:
    """Solo fija modelo/proveedor por defecto cuando no están definidos."""
    if not os.environ.get("ARTHUROS_LLM_PROVIDER"):
        os.environ.setdefault("ARTHUROS_LLM_PROVIDER", "openai")
    os.environ.setdefault("OPENAI_MODEL", "gpt-4o-mini")


def ensure_cloud_llm_configured(logger=None) -> bool:
    """
    Retorna True si hay clave configurada sin imprimir secreto.

    Preferencia: OPENAI_API_KEY desde entorno (.env cargado antes).
    """
    key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPEN_AI_API_KEY")  # alias tolerado
        or ""
    ).strip()
    if key:
        return True
    if logger:
        logger.warning(
            "Nevlan: OPENAI_API_KEY no está definida — funciones Cloud LLM desactivadas hasta "
            "configurar la variable en el entorno (ver `.env.example`)."
        )
    return False
