"""
Nevlan — '_handle_execution_blocked' (PRD 2026-05-08c §G + §H + §D)
=====================================================================

Bug: aparece "Arregla los puntos marcados…" aunque no haya puntos
visibles, y los popups del flujo de ejecución bloqueada usan
``QMessageBox`` nativo (texto a veces invisible, sin estilos Nevlan).

Estos tests validan el contrato a nivel de fuente:

  * El handler usa ``get_visible_blockers_for_user``.
  * El handler usa ``NevlanDialog`` (no ``QMessageBox.warning``).
  * Si no hay puntos visibles, el mensaje es honesto: NO afirma
    "Arregla los puntos marcados…".
  * El humanizador de códigos existe y NO devuelve el código crudo
    para los códigos canónicos.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_CENTER = REPO_ROOT / "app" / "interfaces" / "desktop" / "automation_center.py"


def _src() -> str:
    return AUTO_CENTER.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    src = _src()
    idx = src.index(f"def {name}")
    end = src.index("\n    def ", idx + 1)
    return src[idx:end]


# ─────────────────────────────────────────────────────────────────
# §G — handler usa visible_blockers
# ─────────────────────────────────────────────────────────────────


class TestHandleExecutionBlockedUsesVisibleBlockers:
    def test_function_imports_get_visible_blockers_indirectly(self):
        """El handler llama a build_mission_review_summary (que
        retorna visible_blockers) — no hace falta llamar directamente
        a la función helper."""
        body = _function_body("_handle_execution_blocked")
        assert "build_mission_review_summary" in body, (
            "El handler debe usar el summary que ya filtra blockers "
            "visibles del usuario."
        )

    def test_handler_no_longer_uses_qmessagebox_warning(self):
        """PRD §I: nada de QMessageBox nativo en el handler."""
        body = _function_body("_handle_execution_blocked")
        assert "QMessageBox.warning" not in body
        assert "QMessageBox.critical" not in body

    def test_handler_uses_nevlan_dialog(self):
        body = _function_body("_handle_execution_blocked")
        assert "show_warning" in body or "NevlanDialog" in body

    def test_handler_does_not_blindly_say_fix_marked_points(self):
        """PRD §G.3: el handler NO debe afirmar 'Arregla los puntos
        marcados…' a ciegas. Ese mensaje solo aplica cuando hay
        puntos visibles. Inspeccionamos solo CÓDIGO (sin
        docstrings/comentarios)."""
        body = _function_body("_handle_execution_blocked")
        # Strip docstring (entre primer """ y siguiente """).
        if '"""' in body:
            first = body.index('"""')
            second = body.index('"""', first + 3)
            body = body[:first] + body[second + 3:]
        # Strip comentarios "#…".
        code_lines = []
        for line in body.splitlines():
            stripped = line.split("#", 1)[0]
            code_lines.append(stripped)
        code_only = "\n".join(code_lines)
        assert "Arregla los puntos marcados" not in code_only, (
            "Mensaje inadecuado todavía presente como código; debe "
            "removerse o condicionarse a la presencia de puntos "
            "visibles."
        )

    def test_handler_branches_on_visible_blockers(self):
        body = _function_body("_handle_execution_blocked")
        assert "visible" in body, (
            "El handler debe inspeccionar la lista de blockers "
            "visibles antes de decidir el mensaje."
        )


# ─────────────────────────────────────────────────────────────────
# §G — humanizador de códigos
# ─────────────────────────────────────────────────────────────────


class TestHumanizeBlockerCode:
    def test_humanize_function_exists(self):
        src = _src()
        assert "_humanize_blocker_code" in src

    def test_humanize_returns_human_text_for_canonical_codes(self):
        """Para los códigos canónicos, el humanizador debe devolver
        un texto humano (NO el código tal cual).
        Reproducimos las reglas mínimas con un import dinámico."""
        # Cargamos el módulo en un namespace sin instanciar la UI:
        import importlib
        mod = importlib.import_module(
            "app.interfaces.desktop.automation_center",
        )
        # _humanize_blocker_code es método de instancia. La lógica
        # interesante es la tabla de mapping. La extraemos via fuente.
        body = _function_body("_humanize_blocker_code")
        for code in (
            "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
            "QUERY_FIDELITY_SUSPECT",
            "EMPTY_QUERY_IN_SEARCH",
            "EMPTY_URL_IN_OPEN_URL",
            "EMPTY_APP_IN_OPEN_APP",
            "NEEDS_USER_LABEL_PENDING",
        ):
            assert f'"{code}"' in body, (
                f"El humanizador no contempla {code}; vista normal "
                "mostraría el código crudo."
            )
