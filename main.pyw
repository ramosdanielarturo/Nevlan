"""
Nevlan — Entrypoint sin consola (Windows).
Este archivo .pyw evita que Windows abra una ventana de terminal al arrancar.
Para debugging con consola visible, usar `python main.py`.
"""
from main import main

if __name__ == "__main__":
    main()
