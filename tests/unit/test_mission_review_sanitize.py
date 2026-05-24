"""Saneamiento de paths de imágenes en CompiledStep.

Bug observado el 2026-05-03:
    Tras una grabación, el log mostraba:
        Failed to move snapshot: [WinError 2] El sistema no puede
        encontrar el archivo especificado:
        '...\\assets\\temp\\step_b09f476f.png' ->
        '...\\assets\\<mid>\\step_b09f476f.png'
    El archivo destino sí existía, pero ``image_ref`` quedó apuntando
    al path origen ``assets\\temp\\...`` que ya no existe. Cuando el
    review intentaba renderizar el thumbnail, Qt hacía un paint event
    lazy con ``QPixmap`` sobre un archivo inexistente y disparaba
    ``access violation`` que cerraba TODA la app (al guardar o reparar).

Estos tests fijan el contrato del módulo backend (sin PyQt6):
    - ``is_safe_png_path`` rechaza paths nulos, archivos inexistentes,
      archivos vacíos (<8 bytes) y archivos sin firma PNG válida.
    - ``sanitize_mission_image_refs`` blanquea referencias inválidas
      en TODOS los CompiledStep antes de que el dialog se renderice.
    - Las imágenes válidas se conservan intactas.
    - Funciones puras: nunca lanzan, no tienen side-effects en disco.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest


def _make_valid_png(path: Path) -> None:
    """Crea un PNG mínimo (1x1) con firma + chunks IHDR/IDAT/IEND."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data)
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    idat_raw = b"\x00\x00"
    idat_data = zlib.compress(idat_raw)
    idat_crc = zlib.crc32(b"IDAT" + idat_data)
    idat = (
        struct.pack(">I", len(idat_data))
        + b"IDAT" + idat_data
        + struct.pack(">I", idat_crc)
    )
    iend_crc = zlib.crc32(b"IEND")
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    path.write_bytes(sig + ihdr + idat + iend)


# ── is_safe_png_path ──────────────────────────────────────────────────


class TestIsSafePngPath:
    def test_returns_false_for_none(self):
        from app.services.missions.asset_validation import is_safe_png_path
        assert is_safe_png_path(None) is False

    def test_returns_false_for_empty_string(self):
        from app.services.missions.asset_validation import is_safe_png_path
        assert is_safe_png_path("") is False

    def test_returns_false_for_nonexistent(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        assert is_safe_png_path(str(tmp_path / "nope.png")) is False

    def test_returns_false_for_zero_byte_file(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")
        assert is_safe_png_path(str(empty)) is False

    def test_returns_false_for_garbage_bytes(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        garbage = tmp_path / "garbage.png"
        garbage.write_bytes(b"\x00" * 64)  # no PNG signature
        assert is_safe_png_path(str(garbage)) is False

    def test_returns_false_for_jpeg_signature(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        # Firma JPEG + algunos bytes — NO debe pasar como PNG
        wrong = tmp_path / "x.png"
        wrong.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        assert is_safe_png_path(str(wrong)) is False

    def test_returns_true_for_valid_png(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        good = tmp_path / "good.png"
        _make_valid_png(good)
        assert is_safe_png_path(str(good)) is True

    def test_does_not_raise_on_directory(self, tmp_path: Path):
        from app.services.missions.asset_validation import is_safe_png_path
        # Pasar un directorio NO debe lanzar — solo retornar False.
        assert is_safe_png_path(str(tmp_path)) is False


# ── safe_png_or_none ─────────────────────────────────────────────────


class TestSafePngOrNone:
    def test_returns_string_for_valid(self, tmp_path: Path):
        from app.services.missions.asset_validation import safe_png_or_none
        good = tmp_path / "g.png"
        _make_valid_png(good)
        assert safe_png_or_none(str(good)) == str(good)

    def test_returns_none_for_invalid(self, tmp_path: Path):
        from app.services.missions.asset_validation import safe_png_or_none
        assert safe_png_or_none(str(tmp_path / "nope.png")) is None
        assert safe_png_or_none(None) is None
        assert safe_png_or_none("") is None


# ── sanitize_mission_image_refs ──────────────────────────────────────


class TestSanitizeMissionImageRefs:
    def test_blanks_orphan_temp_path(self, tmp_path: Path):
        """``image_ref`` apuntando a ``temp/...`` (no existe) debe
        quedar en None — exactamente el bug del 2026-05-03 que cerraba
        Nevlan al hacer click en Reparar/Guardar.
        """
        from app.contracts.mission import (
            Mission, CompiledStep, TargetContext,
        )
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )

        orphan = str(tmp_path / "temp" / "step_b09f476f.png")  # NO existe
        good = tmp_path / "real.png"
        _make_valid_png(good)

        m = Mission(name="t")
        s_bad = CompiledStep(
            target_context=TargetContext(
                image_ref=orphan,
                image_ref_mid=None,
            ),
        )
        s_good = CompiledStep(
            target_context=TargetContext(image_ref=str(good)),
        )
        m.compiled_execution_graph = [s_bad, s_good]

        n = sanitize_mission_image_refs(m)

        assert s_bad.target_context.image_ref is None, (
            "El image_ref huérfano debe blanquearse para evitar el "
            "paint event crash."
        )
        assert s_good.target_context.image_ref == str(good)
        assert n == 1

    def test_blanks_zero_byte_png(self, tmp_path: Path):
        from app.contracts.mission import (
            Mission, CompiledStep, TargetContext,
        )
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )

        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")

        m = Mission(name="t")
        s = CompiledStep(target_context=TargetContext(image_ref=str(empty)))
        m.compiled_execution_graph = [s]

        sanitize_mission_image_refs(m)
        assert s.target_context.image_ref is None

    def test_blanks_image_ref_mid_independently(self, tmp_path: Path):
        """``image_ref`` puede ser válido y ``image_ref_mid`` huérfano —
        cada uno se valida por separado.
        """
        from app.contracts.mission import (
            Mission, CompiledStep, TargetContext,
        )
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )

        good = tmp_path / "g.png"
        _make_valid_png(good)
        bad = str(tmp_path / "missing.png")

        m = Mission(name="t")
        s = CompiledStep(target_context=TargetContext(
            image_ref=str(good),
            image_ref_mid=bad,
        ))
        m.compiled_execution_graph = [s]

        sanitize_mission_image_refs(m)
        assert s.target_context.image_ref == str(good)
        assert s.target_context.image_ref_mid is None

    def test_handles_empty_graph(self):
        from app.contracts.mission import Mission
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )
        # No debe lanzar.
        n = sanitize_mission_image_refs(Mission(name="t"))
        assert n == 0

    def test_handles_missing_target_context(self):
        from app.contracts.mission import Mission, CompiledStep
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )
        m = Mission(name="t")
        s = CompiledStep()
        m.compiled_execution_graph = [s]
        sanitize_mission_image_refs(m)  # no raise

    def test_does_not_raise_with_strange_objects(self):
        """Si ``mission`` es algo raro (None, dict, lista) la función
        no debe lanzar. Robustez defensiva.
        """
        from app.services.missions.asset_validation import (
            sanitize_mission_image_refs,
        )
        sanitize_mission_image_refs(None)  # no raise
        # mission con compiled_execution_graph None — debe no lanzar.

        class _Stub:
            compiled_execution_graph = None

        sanitize_mission_image_refs(_Stub())


# ── Test del logger con pythonw (stderr=None) ───────────────────────


class TestLoggerWithoutStderr:
    """Cuando se lanza con ``pythonw.exe`` (sin consola), ``sys.stderr``
    es None y el ``logger.add(sys.stderr, ...)`` original lanzaba
    ``TypeError: Cannot log to objects of type 'NoneType'`` antes de
    que la app arranque siquiera. Bug observado el 2026-05-03 con un
    crash temprano en ``main_exception``.
    """

    def test_setup_logging_does_not_raise_when_stderr_is_none(
        self, monkeypatch
    ):
        # Limpiamos el módulo del cache para forzar re-init.
        import sys as _sys
        # Patcheamos sys.stderr a None ANTES de que setup_logging lo lea.
        monkeypatch.setattr(_sys, "stderr", None)
        # Re-importamos ``setup_logging`` y lo llamamos: NO debe lanzar.
        from app.core.logger import setup_logging

        # ``setup_logging`` no devuelve nada — solo no debe lanzar.
        setup_logging()
