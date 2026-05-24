"""
Auditoría estricta de modo privado: NADA debe materializarse en disco.

La promesa del PRD §12 es:
  > "modo privado sin persistencia"

Este test la verifica de forma rigurosa:

  1. Toma un snapshot recursivo del estado del filesystem antes de
     ejecutar (var/missions/, tmp_path, var/missions/learning/).
  2. Corre el `AutoLearningExecutor` con `private_mode=True` end-to-end:
       - start_run + finish_run
       - 5 step attempts (mezcla success/fail)
       - target memory updates
       - strategy score upserts
       - failure pattern record
       - timeout observation record
       - machine metric updates
       - user correction record
  3. Toma un snapshot recursivo después.
  4. Asserta `before == after` para todas las rutas relevantes.

Si el modo privado deja un solo byte adicional, el test falla.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# Helpers de snapshot del filesystem
# ──────────────────────────────────────────────────────────────────────

def _snapshot_dir(path: Path) -> Dict[str, str]:
    """Recorre `path` y devuelve {ruta_relativa: hash_md5}.

    Si una ruta no existe, devuelve dict vacío. No sigue symlinks.
    """
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for root, dirs, files in os.walk(path, followlinks=False):
        # Excluimos pycache porque puede aparecer por compilación.
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            full = Path(root) / fn
            try:
                rel = str(full.relative_to(path))
            except Exception:
                rel = str(full)
            try:
                h = hashlib.md5(full.read_bytes()).hexdigest()
            except Exception:
                h = "?"
            result[rel] = h
    return result


def _diff(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    diffs: List[str] = []
    keys = set(before) | set(after)
    for k in sorted(keys):
        b = before.get(k)
        a = after.get(k)
        if b != a:
            diffs.append(f"{k}: {b} → {a}")
    return diffs


# ──────────────────────────────────────────────────────────────────────
# Test
# ──────────────────────────────────────────────────────────────────────

class TestPrivateModeFilesystemAudit:
    def test_private_mode_leaves_no_trace_on_disk(self, tmp_path):
        from app.services.missions.auto_learning_executor import (
            AutoLearningExecutor,
            _make_ephemeral_learning_logger,
        )
        from app.services.missions.execution_memory_store import (
            ExecutionMemoryStore, StepAttemptRecord,
        )
        from app.services.missions.execution_contracts import MissionStep
        from app.services.missions.smart_executor import StepOutcome
        from app.services.missions import auto_learning_executor as ale_mod
        from app.core.paths import VAR_DIR

        # 1. Snapshot del estado relevante ANTES.
        var_missions = VAR_DIR / "missions"
        before_var = _snapshot_dir(var_missions)
        before_tmp = _snapshot_dir(tmp_path)
        before_cwd = _snapshot_dir(Path.cwd())

        # 2. SmartMissionExecutor fake (no toca el disco real, no abre Chrome).
        class _FakeSmart:
            def __init__(self, run_id, learning, on_status,
                         on_human_help_needed, on_step_done, expert_mode,
                         **_kw):
                self.run_id = run_id
                self.learning = learning
                self.on_step_done = on_step_done
                self.on_human_help_needed = on_human_help_needed

            def run_mission(self, steps, **_kwargs):
                from app.services.missions.smart_executor import MissionResult
                outcomes = [
                    StepOutcome(
                        step_id=s.id, kind=s.kind, status="success",
                        strategy_used="dom_input", retries=0, duration_ms=120,
                        state_after={"active_app": "chrome.exe",
                                     "browser_url": "https://www.youtube.com/"},
                    )
                    for s in steps
                ]
                # 1 fallido para ejercitar el clasificador.
                outcomes[-1] = StepOutcome(
                    step_id=steps[-1].id, kind=steps[-1].kind,
                    status="failed", strategy_used="bookmark_click",
                    retries=2, duration_ms=2000,
                    error="postcondition no cumplida",
                    state_after={"active_app": "chrome.exe",
                                 "browser_url": "https://www.google.com/"},
                )
                for o in outcomes:
                    if self.on_step_done:
                        self.on_step_done(o)
                r = MissionResult(run_id=self.run_id, status="failed")
                r.outcomes = outcomes
                return r

        # 3. Ejecución end-to-end en modo privado.
        with patch.object(ale_mod, "SmartMissionExecutor", _FakeSmart):
            executor = AutoLearningExecutor(
                mission_id="m_priv",
                private_mode=True,
            )
            assert not executor.store.is_persistent
            steps = [
                MissionStep(id=f"s{i}", kind="search_youtube",
                            params={"query": "x", "domain": "youtube.com"})
                for i in range(3)
            ]
            steps.append(
                MissionStep(id="s_open", kind="open_url",
                            params={"alias": "youtube"})
            )

            # Inyectamos también escrituras directas al store para
            # ejercitar todos los caminos:
            executor.store.upsert_strategy_score(
                step_kind="search_youtube", strategy="dom_input",
                success=True, duration_ms=200,
                scope_app="chrome.exe", scope_domain="youtube.com",
                scope_target="youtube_search_box",
            )
            executor.store.upsert_target_resolution(
                semantic_target="youtube_search_box",
                domain="youtube.com", app="chrome.exe",
                successful_selectors=["input[name='search_query']"],
                last_successful_strategy="dom_input", success=True,
            )
            executor.store.record_user_correction({
                "step_id": "s_priv", "step_kind": "search_youtube",
                "semantic_target": "youtube_search_box",
                "domain": "youtube.com", "app": "chrome.exe",
                "click_x": 100, "click_y": 200, "target_label": "Buscar",
            })
            executor.store.record_failure_pattern(
                run_id="r1", mission_id="m_priv", step_id="s",
                step_kind="open_url", strategy="bookmark_click",
                failure_type="wrong_page",
                error_signature="url no contiene youtube",
            )
            executor.store.record_timeout_observation(
                condition="youtube_loaded", duration_ms=1500, success=True,
                app="chrome.exe", domain="youtube.com",
            )
            executor.store.update_machine_metric(
                machine_id=executor.machine.machine_id,
                metric="dom_ok", value=5.0,
            )

            result = executor.run_mission(steps)
            assert result.mission_result.status in ("failed", "success")

        # 4. Snapshot DESPUÉS.
        after_var = _snapshot_dir(var_missions)
        after_tmp = _snapshot_dir(tmp_path)
        after_cwd = _snapshot_dir(Path.cwd())

        # 5. ASSERTS estrictos:
        diff_var = _diff(before_var, after_var)
        diff_tmp = _diff(before_tmp, after_tmp)
        diff_cwd = _diff(before_cwd, after_cwd)

        assert diff_var == [], (
            "Modo privado escribió en var/missions/. Diff:\n  "
            + "\n  ".join(diff_var)
        )
        assert diff_tmp == [], (
            "Modo privado escribió en tmp_path. Diff:\n  "
            + "\n  ".join(diff_tmp)
        )
        assert diff_cwd == [], (
            "Modo privado escribió en CWD. Diff:\n  "
            + "\n  ".join(diff_cwd)
        )

        # 6. Y aún así, los datos estuvieron disponibles en RAM:
        rows = executor.store.list_step_attempts()
        assert len(rows) >= 4
        sc = executor.store.get_strategy_score(
            step_kind="search_youtube", strategy="dom_input",
            scope_app="chrome.exe", scope_domain="youtube.com",
            scope_target="youtube_search_box",
        )
        assert sc is not None
        # Cada step success añade un ok; el explícito antes del run
        # también. Aceptamos cualquier valor positivo, sólo verificamos
        # que la memoria privada esté funcionando.
        assert int(sc["ok"]) >= 1
        prior = executor.store.find_user_correction(
            step_kind="search_youtube",
            semantic_target="youtube_search_box",
            domain="youtube.com", app="chrome.exe",
        )
        assert prior is not None
        assert prior["target_label"] == "Buscar"

    def test_ephemeral_learning_logger_does_not_write(self, tmp_path):
        """El LearningLogger efímero acumula stats en RAM pero no escribe."""
        from app.services.missions.auto_learning_executor import (
            _make_ephemeral_learning_logger,
        )
        from app.core.paths import VAR_DIR

        before = _snapshot_dir(VAR_DIR / "missions" / "learning")
        logger = _make_ephemeral_learning_logger()
        for i in range(20):
            logger.record_success(
                run_id=f"r{i}", kind="open_url", step_id="s1",
                strategy="cdp_open", duration_ms=100,
            )
            logger.record_failure(
                run_id=f"r{i}", kind="open_url", step_id="s1",
                strategy="bookmark", error="x",
            )
        after = _snapshot_dir(VAR_DIR / "missions" / "learning")
        assert _diff(before, after) == [], (
            "El logger efímero materializó archivos en var/missions/learning/"
        )
        # Sin embargo, la API en RAM debe seguir funcionando:
        ranked = logger.rank_strategies("open_url", ["bookmark", "cdp_open"])
        assert ranked[0] == "cdp_open"
