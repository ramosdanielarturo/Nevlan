class HelpRequestGenerator:
    @staticmethod
    def generate_help_request(context: dict, error: Exception) -> str:
        """
        Generates a concise, human-like request for help.
        Avoids technical jargon where possible.
        """
        # Can be enhanced with LLM later, for now deterministic templates
        action = context.get("action", "operación")
        target = context.get("target", "elemento")
        
        return f"No pude completar la acción '{action}' en '{target}'. ¿Podrías hacerlo tú y decirme cuando estés listo?"

    @staticmethod
    def ask_choice(options: list) -> str:
        if not options:
            return "Necesito ayuda para decidir."
        
        opts_str = " o ".join([f"'{o}'" for o in options])
        return f"Encontré varias opciones: {opts_str}. ¿Cuál prefieres?"
