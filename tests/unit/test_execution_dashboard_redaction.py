"""
Auditoría de privacidad del ``ExecutionDashboard``.

Verifica que el dashboard, en modo default (``redact=True``), no
expone:
  - URLs completas (sólo el host).
  - Títulos de ventana del usuario.
  - OCR / visible_text.
  - Coordenadas de clicks de correcciones humanas.
  - Rutas a screenshots / crops.
  - Tokens / api keys que aparezcan en errores.

Y que con ``redact=False`` sí deja pasar la información cruda (modo
auditoría técnica autorizada).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _build(tmp_path):
    from app.services.missions.execution_memory_store import (
        ExecutionMemoryStore, StepAttemptRecord,
    )
    from app.services.missions.execution_dashboard import ExecutionDashboard
    store = ExecutionMemoryStore(
        db_path=tmp_path / "d.db", audit_jsonl=tmp_path / "d.jsonl",
    )
    dash = ExecutionDashboard(store=store)
    return store, dash


def _seed_run_with_sensitive_data(store):
    """Inyecta data muy sensible para verificar la redacción."""
    from app.services.missions.execution_memory_store import StepAttemptRecord
    run = store.start_run(mission_id="m_priv_test")
    sensitive_state_after = {
        "active_app": "chrome.exe",
        "active_pid": 12345,
        "active_window_title": "Mi cuenta bancaria — Pagos pendientes",
        "browser_url": "https://miapp.com/account?token=ABC123XYZ&user=daniel",
        "browser_title": "Detalle de mi tarjeta de crédito",
        "browser_domain": "miapp.com",
        "visible_text": "Saldo: $1,234.56 USD | Última transacción 2026-05-06",
        "uia_visible_names": ["Saldo", "Cancelar suscripción", "Daniel Ramos"],
        "screen_size": [1920, 1080],
        "dpi_scale": 1.25,
    }
    store.record_step_attempt(StepAttemptRecord(
        run_id=run.run_id, mission_id="m_priv_test",
        step_id="s1", step_kind="open_url",
        attempt_index=1, strategy_used="cdp_open",
        success=False, retries=2, duration_ms=2000,
        semantic_target="login_button",
        app="chrome.exe", domain="miapp.com",
        window_title="Mi cuenta — sesión #abc-def-ghi-jkl",
        state_after=sensitive_state_after,
        error="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRETTOKEN.signature  failed",
        failure_type="permission_error",
    ))
    store.upsert_target_resolution(
        semantic_target="login_button",
        domain="miapp.com", app="chrome.exe",
        successful_selectors=["#login"],
        ocr_anchor="Iniciar sesión con tu cuenta corporativa de la empresa privada",
        success=True, last_successful_strategy="dom_input",
    )
    store.record_user_correction({
        "run_id": run.run_id, "step_kind": "open_url",
        "semantic_target": "login_button",
        "domain": "miapp.com", "app": "chrome.exe",
        "click_x": 547, "click_y": 612,
        "target_label": "Iniciar sesión",
        "nearest_text": "Bienvenido daniel.ramos@empresa.com",
        "uia_blob": {"name": "Login", "value": "secret123"},
        "dom_blob": {"selector": "#login"},
        "visual_crop_path": "C:\\Users\\Daniel\\screenshots\\private_001.png",
        "screenshot_context_path": "C:\\Users\\Daniel\\screenshots\\page_001.png",
        "question": "¿Dónde queda el botón de login?",
    })
    return run.run_id


class TestDashboardRedaction:
    def test_run_detail_redacts_sensitive_state_in_expert_mode(self, tmp_path):
        store, dash = _build(tmp_path)
        run_id = _seed_run_with_sensitive_data(store)
        out = dash.run_detail(run_id, expert=True, redact=True)
        # Debe estar marcado como redactado.
        assert out["redacted"] is True

        # Verificamos los campos sensibles del state_after:
        attempt = out["attempts"][0]
        sa = attempt["state_after"]
        # browser_url se reduce a "<host:domain>"
        assert "token" not in str(sa.get("browser_url", "")).lower()
        assert sa.get("browser_url") == "<host:miapp.com>"
        # active_window_title redactado
        assert sa.get("active_window_title") == "<redacted>"
        # browser_title redactado
        assert sa.get("browser_title") == "<redacted>"
        # visible_text redactado
        assert sa.get("visible_text") == "<redacted>"
        # uia_visible_names enmascarado a contador
        assert "items redacted" in str(sa.get("uia_visible_names"))
        # Pero campos técnicos siguen presentes
        assert sa.get("active_app") == "chrome.exe"
        assert sa.get("dpi_scale") == 1.25
        assert sa.get("browser_domain") == "miapp.com"

        # window_title del attempt también redactado.
        assert attempt["window_title"] != "Mi cuenta — sesión #abc-def-ghi-jkl"

        # Error: token bearer enmascarado.
        err = (attempt.get("error") or "").lower()
        assert "secrettoken" not in err
        assert "***redacted***" in err

    def test_run_detail_summary_view_does_not_include_sensitive_fields(self, tmp_path):
        """En vista no-experta, ni state_before/after ni errores se exponen."""
        store, dash = _build(tmp_path)
        run_id = _seed_run_with_sensitive_data(store)
        out = dash.run_detail(run_id, expert=False, redact=True)
        attempt = out["attempts"][0]
        # Clave técnica básica está
        assert attempt["step_kind"] == "open_url"
        assert attempt["failure_type"] == "permission_error"
        # No se filtra el error completo (no aparece la palabra "Bearer")
        full_dump = json.dumps(out, default=str)
        assert "Bearer" not in full_dump
        assert "secrettoken" not in full_dump.lower()
        assert "Saldo: $1,234.56" not in full_dump
        assert "miapp.com/account?token=" not in full_dump

    def test_target_table_redacts_long_ocr_anchor(self, tmp_path):
        store, dash = _build(tmp_path)
        run_id = _seed_run_with_sensitive_data(store)
        out = dash.target_table(redact=True)
        # El target tiene un ocr_anchor de >40 chars → debe estar redactado
        assert any(
            (item.get("ocr_anchor") or "").startswith("<redacted")
            for item in out["items"]
        )
        assert out["redacted"] is True

    def test_corrections_table_strips_clicks_paths_and_blobs(self, tmp_path):
        store, dash = _build(tmp_path)
        _seed_run_with_sensitive_data(store)
        out = dash.corrections_table(redact=True)
        assert out["items"], "debería haber al menos 1 corrección"
        c = out["items"][0]
        # Sensibles eliminados:
        for k in (
            "click_x", "click_y",
            "uia_blob", "dom_blob",
            "visual_crop_path", "screenshot_context_path",
            "nearest_text", "question",
        ):
            assert k not in c, f"campo sensible filtrado: {k} = {c.get(k)!r}"
        # Conserva metadatos técnicos:
        assert c.get("step_kind") == "open_url"
        assert c.get("semantic_target") == "login_button"
        # No expone el email del usuario:
        full_dump = json.dumps(out, default=str)
        assert "daniel.ramos@empresa.com" not in full_dump
        assert "secret123" not in full_dump
        assert "private_001.png" not in full_dump

    def test_no_redact_flag_exposes_raw_data(self, tmp_path):
        """Cuando se desactiva la redacción explícitamente, los datos
        crudos sí pasan (modo auditoría técnica)."""
        store, dash = _build(tmp_path)
        run_id = _seed_run_with_sensitive_data(store)
        out = dash.run_detail(run_id, expert=True, redact=False)
        attempt = out["attempts"][0]
        sa = attempt["state_after"]
        # En modo crudo, la URL completa sí aparece
        assert "miapp.com/account" in (sa.get("browser_url") or "")

        targets = dash.target_table(redact=False)
        assert any(
            (it.get("ocr_anchor") or "").startswith("Iniciar")
            for it in targets["items"]
        )
        corr = dash.corrections_table(redact=False)
        assert any(it.get("click_x") == 547 for it in corr["items"])

    def test_history_summary_does_not_include_sensitive_strings(self, tmp_path):
        store, dash = _build(tmp_path)
        _seed_run_with_sensitive_data(store)
        out = dash.history()
        full_dump = json.dumps(out, default=str)
        assert "Bearer" not in full_dump
        assert "Saldo:" not in full_dump
        assert "secret123" not in full_dump
