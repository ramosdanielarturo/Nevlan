"""
ArthurOS Filesystem Guard (Unlocked)
------------------------------------
Permite acceso a carpetas del usuario (Desktop, Documents, etc.)
pero protege archivos del sistema Windows.
"""
import os
from pathlib import Path

def safe_resolve(path_str: str) -> Path:
    # 1. Obtenemos el HOME del usuario (ej: C:\Users\Daniel)
    user_home = Path.home()
    
    # 2. Resolvemos la ruta solicitada
    # Si es absoluta, la usamos. Si es relativa, la unimos al path actual.
    target = Path(path_str).resolve()
    
    # Si la ruta es relativa (ej: "Documentos"), asumimos que es desde el Home
    if not target.is_absolute():
        target = (user_home / path_str).resolve()

    # 3. LISTA BLANCA (Permitir)
    # Permitimos cualquier cosa dentro del usuario
    if str(target).startswith(str(user_home)):
        return target
        
    # 4. LISTA NEGRA (Prohibir Windows y Archivos de Programa)
    sys_root = Path(os.environ.get("SystemRoot", "C:\\Windows")).resolve()
    if str(target).startswith(str(sys_root)):
         raise PermissionError("ACCESO DENEGADO: No puedes tocar la carpeta Windows.")

    # Si pasa los filtros, adelante
    return target

def anywhere_resolve(path_str: str) -> Path:
    return Path(path_str).resolve()