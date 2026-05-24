"""Qt font engine: jamás caer a MS Serif (fuente bitmap legacy).

Bug observado el 2026-05-02:
    El crash handler capturó este reporte:

        DirectWrite: CreateFontFaceFromHDC() failed [...] for
        QFontDef(Family="MS Serif", ...)
        Windows fatal exception: access violation

    Qt elegía "MS Serif" como fuente fallback al renderizar algún
    glyph del ``MissionReviewDialog``. DirectWrite no soporta fuentes
    bitmap y genera un access violation en lugar de devolver error,
    matando todo el proceso (centro de automatizaciones incluido).

Fix:
    1. ``main.py`` setea ``QT_QPA_PLATFORM=windows:fontengine=freetype``
       antes de importar PyQt6. Freetype maneja fuentes legacy sin
       crashear.
    2. ``main.py`` setea ``app.setFont(QFont("Segoe UI", 9))`` para que
       cualquier widget sin font-family explícito use Segoe UI (fuente
       moderna TrueType siempre presente en Windows desde Vista).
    3. Stylesheets de QToolTip incluyen ``font-family: 'Segoe UI'``
       para que los tooltips no degraden a MS Serif.

Estos tests verifican el setup en ``main.py`` sin necesidad de levantar
un QApplication real (los tests son headless, no hay display).
"""
from __future__ import annotations

import sys
from pathlib import Path


def test_main_sets_freetype_fontengine_on_win32():
    """``main.py`` debe pedir el motor Freetype en Windows."""
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    text = main_path.read_text(encoding="utf-8")
    assert 'QT_QPA_PLATFORM' in text and 'fontengine=freetype' in text, (
        "main.py debe configurar QT_QPA_PLATFORM con fontengine=freetype "
        "para evitar el access violation de DirectWrite con MS Serif."
    )
    # Y el setdefault debe condicionarse a win32 para no afectar Linux/Mac.
    assert 'sys.platform == "win32"' in text


def test_main_sets_default_segoe_ui():
    """``main.py`` debe llamar ``app.setFont(QFont("Segoe UI", ...))``."""
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    text = main_path.read_text(encoding="utf-8")
    assert 'QFont("Segoe UI"' in text, (
        "main.py debe setear Segoe UI como fuente default explícita "
        "para evitar que Qt elija MS Serif como fallback."
    )
    assert 'app.setFont(' in text


def test_tooltip_stylesheet_in_review_specifies_font_family():
    """El QToolTip del MissionReview no puede dejar font-family al
    sistema (caía a MS Serif): debe especificar Segoe UI explícito.
    """
    review_path = (
        Path(__file__).resolve().parents[2]
        / "app" / "interfaces" / "desktop" / "mission_review.py"
    )
    text = review_path.read_text(encoding="utf-8")
    # Buscamos el bloque del QToolTip y verificamos que tiene font-family.
    # Heurística simple: tras "QToolTip {" debe aparecer "font-family"
    # antes del cierre "}".
    idx = text.find("QToolTip {")
    if idx == -1:
        # Aceptamos también doble-llave de f-string.
        idx = text.find("QToolTip {{")
    assert idx >= 0, "No encontré bloque QToolTip en mission_review.py"
    block = text[idx: idx + 600]
    assert "font-family" in block.lower(), (
        "QToolTip del review no especifica font-family. Sin esto Qt "
        "puede degradar a MS Serif y disparar access violation."
    )
    assert "segoe ui" in block.lower()


def test_install_crash_handlers_called_before_qt_imports():
    """En ``main.py`` ``install_crash_handlers()`` debe llamarse antes
    de cualquier ``from PyQt6...``. De lo contrario un crash temprano
    durante import de PyQt6 no sería capturado.
    """
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    text = main_path.read_text(encoding="utf-8")
    crash_idx = text.find("install_crash_handlers()")
    qt_idx = text.find("from PyQt6")
    assert crash_idx >= 0, "install_crash_handlers no se invoca en main.py"
    assert qt_idx >= 0
    assert crash_idx < qt_idx, (
        "install_crash_handlers() debe correr antes de importar PyQt6 "
        "para capturar crashes durante el setup de Qt."
    )
