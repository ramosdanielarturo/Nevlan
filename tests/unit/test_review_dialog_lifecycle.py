"""Contratos de ciclo de vida del MissionReviewDialog.

Bug observado el 2026-05-03 (segundo intento):
    Pese a forzar Freetype y blanquear ``image_ref`` huérfanos, Nevlan
    seguía cerrándose con ``access violation`` durante ``dialog.exec()``
    al guardar / reparar pasos. El stack mostraba SOLO 3 frames Python:

        Current thread:
          File "automation_center.py", line 5287 in _safe_open_review
          File "main.py", line 110 in main
          File "main.py", line 114 in <module>

    Eso es sello inequívoco de un crash C++-side dentro del event loop.

Causa raíz identificada:
    1. ``WA_DeleteOnClose`` + ``dialog.exec()`` modal + ``parent=None``:
       cuando el modal cierra, Qt ejecuta ``deleteLater`` sobre el
       QObject C++. El wrapper Python sigue vivo apuntando a un C++
       liberado. Cualquier acceso siguiente (incluso GC silencioso)
       crashea con access violation. Patrón conocido de PyQt6.
    2. ``os.environ.setdefault("QT_QPA_PLATFORM", ...)`` solo aplica si
       la variable NO existía. En sistemas con QT_QPA_PLATFORM ya
       seteado a otro valor, el fontengine seguía siendo DirectWrite
       y crasheaba con fuentes legacy.

Estos tests fijan los fixes para que NO regresen.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Bug 1: WA_DeleteOnClose con exec() ─────────────────────────────


class TestNoDeleteOnCloseWithExec:
    """``MissionReviewDialog`` se abre con ``exec()``, pero NUNCA debe
    setearse ``WA_DeleteOnClose`` antes de ``exec()``: PyQt6 lo trata
    como "Qt borra el C++ apenas el modal cierre", lo que invalida el
    wrapper Python y crashea al GC.
    """

    def test_safe_open_review_does_not_set_wa_deleteonclose(self):
        """Buscamos en el código fuente que no haya ``WA_DeleteOnClose``
        ANTES de ``dialog.exec()``. El comentario explicativo NO cuenta:
        validamos que la línea de código real no exista.
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "automation_center.py").read_text(encoding="utf-8")

        # Localizamos la región alrededor de ``_safe_open_review``.
        idx = src.index("def _safe_open_review")
        end = src.index("def ", idx + 1)
        region = src[idx:end]

        # NO debe haber código activo seteando WA_DeleteOnClose.
        # Permitimos la palabra dentro de comentarios (líneas que
        # empiezan con #).
        offending: list[str] = []
        for ln_no, ln in enumerate(region.splitlines(), start=1):
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "WA_DeleteOnClose" in ln and "setAttribute" in ln:
                offending.append(f"line {ln_no}: {ln.strip()}")

        assert not offending, (
            "_safe_open_review no debe usar WA_DeleteOnClose con exec(): "
            "es un patrón conocido de PyQt6 que crashea al cerrar.\n"
            + "\n".join(offending)
        )


# ── Bug 2: Forzar fontengine de forma confiable ────────────────────


class TestQtPlatformForcedReliably:
    """``main.py`` debe forzar ``windows:fontengine=freetype`` por TODOS
    los caminos posibles: ``os.environ`` (override, no setdefault) y
    ``sys.argv -platform``. Sin esto, en un sistema con
    QT_QPA_PLATFORM ya configurado a otro valor, el fontengine queda
    en DirectWrite y crashea con fuentes legacy bitmap.
    """

    def test_main_py_overrides_qt_qpa_platform(self):
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "main.py").read_text(encoding="utf-8")

        # Debe SOBREESCRIBIR (no setdefault) la var de entorno.
        assert 'os.environ["QT_QPA_PLATFORM"]' in src or (
            'os.environ[\'QT_QPA_PLATFORM\']' in src
        ), (
            "main.py debe SOBREESCRIBIR QT_QPA_PLATFORM (no usar "
            "setdefault). setdefault solo aplica si la var no existe — "
            "lo cual es justo el caso donde el usuario tiene un "
            "QT_QPA_PLATFORM con DirectWrite que crashea."
        )

    def test_main_py_passes_platform_via_sys_argv(self):
        """sys.argv ``-platform windows:fontengine=freetype`` es la
        forma de pasarle a Qt el platform plugin de manera totalmente
        independiente del environment. Cinturón+tirantes con env var.
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "main.py").read_text(encoding="utf-8")
        assert '"-platform"' in src, (
            "main.py debe extender sys.argv con [\"-platform\", "
            "\"windows:fontengine=freetype\"]"
        )
        assert "fontengine=freetype" in src

    def test_main_py_sets_fusion_style(self):
        """Estilo ``fusion`` evita que Qt delegue paint a la nativa de
        Windows (DirectWrite/D3D), que es la que dispara el access
        violation en LTSC 1809.
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "main.py").read_text(encoding="utf-8")
        assert 'app.setStyle("fusion")' in src or "app.setStyle('fusion')" in src


# ── Bug 3: Dialog hereda fuente segura ──────────────────────────────


class TestDialogForcesSafeFont:
    """El dialog DEBE setear su QFont a Segoe UI explícitamente. Aunque
    QApplication tenga la fuente configurada, un widget puede heredar
    de un parent con fuente legacy si Qt resuelve por proxyStyle u
    otra cadena. setFont en el dialog blinda contra eso.
    """

    def test_review_dialog_init_sets_segoe_ui(self):
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "mission_review.py").read_text(encoding="utf-8")

        idx = src.index("class MissionReviewDialog")
        end_marker = "def _build_title_bar"
        end = src.index(end_marker, idx)
        init_region = src[idx:end]

        assert 'QFont("Segoe UI"' in init_region, (
            "MissionReviewDialog.__init__ debe setear self.setFont"
            "(QFont(\"Segoe UI\", ...)) explícitamente."
        )
        assert "self.setFont(" in init_region

    def test_review_dialog_has_breadcrumb_logs(self):
        """Sin breadcrumbs, un access violation en C++ deja solo el
        frame ``dialog.exec()`` y no sabemos en qué fase crashó. Los
        logs ``Review[N/8]`` son nuestra única forma de diagnosticar.
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "mission_review.py").read_text(encoding="utf-8")

        for marker in ("Review[1/8]", "Review[3/8]",
                       "Review[6/8]", "Review[8/8]"):
            assert marker in src, (
                f"Falta breadcrumb '{marker}' en mission_review.py — "
                "sin estos logs un futuro access violation NO se podrá "
                "diagnosticar."
            )


# ── Bug 4: Sin FramelessWindowHint + barra nativa ──────────────────


class TestNativeWindowChrome:
    """El dialog DEBE usar barra nativa de Windows. El chrome custom
    con ``FramelessWindowHint`` + ``nativeEvent`` con override de
    ``WM_NCHITTEST`` causaba access violation en el primer paint event
    en Win10 LTSC 1809 (bug 2026-05-03).
    """

    def test_review_dialog_does_not_use_frameless_hint(self):
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "mission_review.py").read_text(encoding="utf-8")

        idx = src.index("class MissionReviewDialog")
        end = src.index("def _build_title_bar", idx)
        init_region = src[idx:end]

        # Buscamos LÍNEAS DE CÓDIGO ACTIVAS que contengan FramelessWindowHint.
        offending: list[str] = []
        for ln in init_region.splitlines():
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if (
                "FramelessWindowHint" in ln
                and "setWindowFlags" in ln  # solo el setWindowFlags activo
            ):
                offending.append(ln.strip())

        assert not offending, (
            "MissionReviewDialog NO debe usar FramelessWindowHint con "
            "exec(): dispara access violation en el primer paint event "
            "en Win10 LTSC 1809.\n"
            + "\n".join(offending)
        )

    def test_review_dialog_does_not_override_nativeevent_with_ht(self):
        """El override de ``nativeEvent`` con códigos ``HT*`` (HTTOP,
        HTBOTTOM, etc.) es solo necesario con FramelessWindowHint.
        Con barra nativa interfiere con el HitTest de Windows y se
        sospechaba como causante del crash. Debe estar removido o ser
        un no-op (sin returns con códigos HT*).
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "mission_review.py").read_text(encoding="utf-8")

        idx = src.index("class MissionReviewDialog")
        end_marker = "class "
        try:
            end = src.index(end_marker, idx + 1)
        except ValueError:
            end = len(src)
        cls_region = src[idx:end]

        # NO debe haber un ``def nativeEvent`` activo que retorne
        # códigos HTTOPLEFT/HTBOTTOMLEFT/etc. Es OK que existan los
        # comentarios mencionando los códigos, pero NO en código real.
        offending: list[str] = []
        for ln in cls_region.splitlines():
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Líneas activas que retornan True con un código HT*.
            if "return True," in ln and "code" in ln:
                offending.append(ln.strip())

        assert not offending, (
            "MissionReviewDialog ya no usa FramelessWindowHint, así "
            "que ``nativeEvent`` no debe retornar códigos HT*. Esto "
            "interfiere con el HitTest nativo de Windows.\n"
            + "\n".join(offending)
        )

    def test_review_dialog_logs_paint_event(self):
        """Para que el próximo crash deje rastro, el dialog DEBE
        loguear el primer ``showEvent`` y ``paintEvent``.
        """
        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "interfaces" / "desktop" /
               "mission_review.py").read_text(encoding="utf-8")
        assert "Review[paint] showEvent dispatched" in src
        assert "Review[paint] first paintEvent OK" in src
