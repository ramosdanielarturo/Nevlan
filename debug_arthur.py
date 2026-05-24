
import re
import string
import unicodedata

# Function from main_window.py
def process_text_sim(text):
    norm = text.lower()
    for char in string.punctuation:
        norm = norm.replace(char, " ")
    norm = " ".join(norm.split())
    norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
    return norm

# Regex building from main_window.py
names = r"(?:arthur|arturo|art|artur|artu)"
close_words = ["cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá", "adios", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you", "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye", "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close", "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"]

def make_pattern(word_list):
    cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
    return re.compile(rf"(?:{names}\s+{cmds}\b)|(?:\b{cmds}\s+{names})", re.IGNORECASE)

re_close = make_pattern(close_words)

tests = [
    "Arthur cierra",
    "arthur cierra",
    "Arthur, cierra",
    "arthur. cierra",
    "Cierra Arthur",
    "cierra arthur"
]

print("--- DIAGNOSTIC RESULT ---")
for t in tests:
    norm = process_text_sim(t)
    match = re_close.search(norm)
    print(f"Input: '{t}'")
    print(f"  Norm: '{norm}'")
    print(f"  Match: {'YES' if match else 'NO'}")
    if match:
        print(f"  Group: '{match.group(0)}'")
    print("-" * 20)
