"""Tests para el bloqueo de URLs (interna y externa) como texto escrito.

Bug 2026-05-03 (caso real): cuando el usuario hacía click en el icono
de YouTube, Nevlan grababa:

    "Escribir 'https://www.youtube.com/'"

como si el usuario hubiera tipeado la URL. La URL venía de
``page.url`` en el DOM, no del teclado.

Comportamiento esperado:
  - ``is_browser_internal_url(chrome://...)`` → True
  - ``looks_like_external_url(https://...)`` → True
  - ``is_url_contamination`` cubre ambos casos cuando el usuario NO
    escribió la URL.
  - El recorder y el compiler descartan URLs como ``final_text``
    cuando el ``user_typed_text`` no las contiene.
"""
from __future__ import annotations

from app.services.missions.field_commit import (
    is_browser_internal_url, is_url_contamination, looks_like_external_url,
)


class TestInternalUrlDetection:
    def test_chrome_internal(self):
        assert is_browser_internal_url("chrome://profile-picker/")
        assert is_browser_internal_url("chrome://settings/")
        assert is_browser_internal_url("CHROME://NEW-TAB/")  # case insens

    def test_edge_internal(self):
        assert is_browser_internal_url("edge://settings/")

    def test_about_blank(self):
        assert is_browser_internal_url("about:blank")

    def test_normal_url_is_not_internal(self):
        assert not is_browser_internal_url("https://www.youtube.com/")
        assert not is_browser_internal_url("http://example.com")

    def test_empty_or_none(self):
        assert not is_browser_internal_url("")
        assert not is_browser_internal_url(None)


class TestExternalUrlDetection:
    def test_https(self):
        assert looks_like_external_url("https://www.youtube.com/")

    def test_http(self):
        assert looks_like_external_url("http://example.com")

    def test_ftp(self):
        assert looks_like_external_url("ftp://files.example.com/")

    def test_text_is_not_url(self):
        assert not looks_like_external_url("hola")
        assert not looks_like_external_url("devuelveme el amor")
        assert not looks_like_external_url("user@example.com")


class TestUrlContamination:
    """``is_url_contamination`` decide si el valor leído es ruido."""

    def test_internal_url_with_different_buffer_is_contamination(self):
        """chrome://profile-picker/ + buffer 'Chrome' → contamina.

        Caso clásico: el usuario escribió "Chrome" en Windows Search,
        pero el campo lee la barra de URL del profile picker.
        """
        assert is_url_contamination(
            read_value="chrome://profile-picker/",
            user_typed="Chrome",
        )

    def test_external_url_with_empty_buffer_is_contamination(self):
        """https://www.youtube.com/ + buffer vacío → contamina.

        El usuario hizo click en el icono de YouTube sin escribir nada.
        """
        assert is_url_contamination(
            read_value="https://www.youtube.com/",
            user_typed="",
        )

    def test_external_url_with_different_buffer_is_contamination(self):
        """https://www.youtube.com/ + buffer 'Devuelveme...' → contamina."""
        assert is_url_contamination(
            read_value="https://www.youtube.com/",
            user_typed="devuelveme el amor",
        )

    def test_user_actually_typed_url_is_not_contamination(self):
        """Si el usuario realmente escribió la URL en la barra de
        direcciones, NO es contaminación.
        """
        assert not is_url_contamination(
            read_value="https://www.youtube.com/",
            user_typed="https://www.youtube.com/",
        )
        assert not is_url_contamination(
            read_value="https://www.youtube.com",
            user_typed="https://www.youtube.com",
        )

    def test_normal_text_is_not_contamination(self):
        """Buffer dice "hola" y read dice "hola" → no contamina."""
        assert not is_url_contamination(
            read_value="hola mundo",
            user_typed="hola mundo",
        )

    def test_empty_read_value(self):
        assert not is_url_contamination(
            read_value="", user_typed="anything"
        )
        assert not is_url_contamination(
            read_value=None, user_typed="anything"
        )

    def test_case_insensitive_url_match(self):
        """HTTPS://YOUTUBE.COM con buffer 'https://youtube.com' → no contamina."""
        assert not is_url_contamination(
            read_value="HTTPS://www.youtube.com/",
            user_typed="https://www.youtube.com/",
        )
