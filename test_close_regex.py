import re
import unicodedata

# Nombres aceptados
names = r"(?:arthur|arturo|art|artur|artu)"

# Close words
close_words = [
    "cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar",
    "finaliza", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá",
    "adios", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you",
    "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye",
    "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close",
    "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"
]

# Función make_pattern (copiada del código)
def make_pattern(word_list):
    cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
    return re.compile(rf"(?:{names}.*?{cmds})|(?:{cmds}.*?{names})", re.IGNORECASE)

# Crear el patrón
re_close = make_pattern(close_words)

# Función de normalización (copiada del código)
def normalize(text):
    norm = text.lower().strip()
    norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
    return norm

# Casos de prueba
test_cases = [
    "arthur adios",
    "adios arthur",
    "arthur a",
    "a arthur",
    "arthur cierra",
    "cierra arthur",
    "arthur bye",
    "bye bye arthur",
    "solo adios",  # Este NO debería funcionar
    "arthur",  # Este NO debería funcionar
]

print("Probando el regex de close:")
print("=" * 50)
for test in test_cases:
    norm = normalize(test)
    match = re_close.search(norm)
    result = "MATCH" if match else "NO MATCH"
    print(f"'{test}' -> '{norm}' -> {result}")
    if match:
        print(f"  Matched: {match.group()}")
