"""
Test rápido para verificar que el comando de cierre funciona correctamente
"""
import re
import unicodedata

# Copiado del código actualizado
names = r"(?:arthur|arturo|art|artur|artu)"

close_words = [
    "cierra", "cierrate", "cerrar", "salir", "sal", "salte", "exit", "quit", "finalizar",
    "finaliza", "termina", "terminá", "terminar", "fin", "acabar", "acaba", "acabá",
    "adios", "a", "bye", "chao", "chau", "hasta luego", "nos vemos", "goodbye", "see you",
    "apagate", "shutdown", "kill", "matar", "muerete", "morite", "destruye",
    "autodestruccion", "vete", "andate", "largo", "fuera", "safa", "terminate", "close",
    "sistema fuera", "abortar", "cerrar sistema", "bye bye", "hasta mañana", "apagar sistema"
]

def make_pattern(word_list):
    # Regex con límites de palabra estrictos para detectar palabras cortas como "a"
    cmds = r"(?:" + "|".join(map(re.escape, word_list)) + r")"
    # Usamos \s+ para espacios y \b para límites de palabra
    return re.compile(rf"(?:{names}\s+{cmds}\b)|(?:\b{cmds}\s+{names})", re.IGNORECASE)

re_close = make_pattern(close_words)

def normalize(text):
    norm = text.lower().strip()
    norm = ''.join(c for c in unicodedata.normalize("NFD", norm) if unicodedata.category(c) != "Mn")
    return norm

# Casos de prueba
test_cases = [
    ("arthur a", True, "Debe cerrar con 'arthur a'"),
    ("a arthur", True, "Debe cerrar con 'a arthur'"),
    ("arthur adios", True, "Debe cerrar con 'arthur adios'"),
    ("adios arthur", True, "Debe cerrar con 'adios arthur'"),
    ("arthur cierra", True, "Debe cerrar con 'arthur cierra'"),
    ("solo a", False, "NO debe cerrar sin 'arthur'"),
    ("solo adios", False, "NO debe cerrar sin 'arthur'"),
    ("arthur", False, "NO debe cerrar solo con 'arthur'"),
]

print("=" * 60)
print("VERIFICACIÓN DE COMANDOS DE CIERRE")
print("=" * 60)
print()

all_passed = True
for test, should_match, description in test_cases:
    norm = normalize(test)
    match = re_close.search(norm)
    matched = match is not None
    
    status = "OK" if matched == should_match else "FAIL"
    if matched != should_match:
        all_passed = False
    
    print(f"[{status}] {description}")
    print(f"     Input: '{test}' -> Normalized: '{norm}'")
    print(f"     Expected: {'MATCH' if should_match else 'NO MATCH'}, Got: {'MATCH' if matched else 'NO MATCH'}")
    if match:
        print(f"     Matched text: '{match.group()}'")
    print()

print("=" * 60)
if all_passed:
    print("TODOS LOS TESTS PASARON!")
else:
    print("ALGUNOS TESTS FALLARON - Revisar configuracion")
print("=" * 60)
