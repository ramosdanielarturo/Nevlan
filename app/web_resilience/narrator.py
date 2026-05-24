from .taxonomy import WebFailureType

class WebNarrator:
    def narrate_fallback(self, failure: WebFailureType, strategy_name: str) -> str:
        if failure == WebFailureType.SELECTOR_NOT_FOUND:
            return f"No encontré el elemento habitual, intentando con {strategy_name}."
        if failure == WebFailureType.ELEMENT_NOT_VISIBLE:
            return f"El elemento está oculto, probando {strategy_name} para revelarlo."
        if failure == WebFailureType.INTERCEPTED:
            return f"Algo bloquea el click, intentando {strategy_name}."
        return f"Problema detectado ({failure.value}), probando alternativa {strategy_name}."

    def narrate_success(self, strategy_name: str) -> str:
        return f"Recuperado exitosamente usando {strategy_name}."
