
import re
import unicodedata

def clean_text(text):
    norm = text.lower().strip()
    norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
    return norm

names = r"(?:arthur|arturo|art|artur|artu)"
close_words = ["cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá", "adios", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you", "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye", "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close", "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"]

cmds = r"(?:" + "|".join(map(re.escape, close_words)) + r")"
# The regex used in main_window.py
re_close = re.compile(rf"(?:{names}\s+{cmds}\b)|(?:\b{cmds}\s+{names})", re.IGNORECASE)

inputs = [
    "arthur cerrar",
    "cerrar arthur",
    "arthur, cerrar",
    "arthur. cerrar",
    "¡arthur! cerrar",
    "arthur cerrar ahora",
    "arthur, salte",
]

print("--- Testing Regex ---")
print(f"Regex Pattern: {re_close.pattern}")
for i in inputs:
    norm = clean_text(i)
    matched = re_close.search(norm)
    print(f"Input: '{i}' -> Norm: '{norm}' -> Match: {bool(matched)}")
