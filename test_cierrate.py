import re
import unicodedata

names = r"(?:arthur|arturo|art|artur|artu)"

close_words = [
    "cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar",
    "finaliza", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá",
    "adios", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you",
    "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye",
    "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close",
    "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"
]

def make_pattern(word_list):
    cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
    return re.compile(rf"(?:{names}\s+{cmds}\b)|(?:\b{cmds}\s+{names})", re.IGNORECASE)

re_close = make_pattern(close_words)

def normalize(text):
    norm = text.lower().strip()
    norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
    return norm

# Test exacto del usuario
test = "CIERRATE ARTHUR"
norm = normalize(test)
match = re_close.search(norm)

print(f"Input: '{test}'")
print(f"Normalized: '{norm}'")
print(f"Match: {match}")
if match:
    print(f"Matched text: '{match.group()}'")
else:
    print("NO MATCH - El regex NO detecta este comando")
    
# Probar variaciones
print("\n" + "="*50)
print("Probando variaciones:")
tests = [
    "cierrate arthur",
    "arthur cierrate",
    "cierra arthur",
    "arthur cierra",
]

for t in tests:
    norm = normalize(t)
    match = re_close.search(norm)
    print(f"'{t}' -> {'MATCH' if match else 'NO MATCH'}")
