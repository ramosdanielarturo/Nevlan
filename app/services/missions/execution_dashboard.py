"""
ArthurOS Services - ExecutionDashboard (Auto-Learning Executor)
---------------------------------------------------------------
Vista simple del aprendizaje y la historia de ejecuciones.

Funciones:
  - history(mission_id?, redact=True) -> list de ejecuciones resumido
  - run_detail(run_id, expert=False, redact=True) -> historial detallado
  - strategy_table(step_kind?) -> tabla de scores por estrategia
  - target_table(redact=True) -> tabla de targets aprendidos
  - main() -> CLI: ``python -m app.services.missions.execution_dashboard``

Privacidad y datos sensibles (PRD §12):
  Por defecto el dashboard **redacta** información que puede revelar
  contenido del usuario:
    - ``state_before`` / ``state_after``: se conservan claves estables
      (active_app, active_window_class, dpi, screen_size) y se redactan
      o truncan valores potencialmente sensibles (visible_text, OCR,
      browser_url, browser_title, active_window_title).
    - Errores: truncados a 80 caracteres + posibles tokens enmascarados.
    - User corrections: se omiten click_x/click_y, screenshot_path,
      visual_crop_path, nearest_text — se conservan sólo etiquetas y
      contadores.
    - Targets: se mantienen selectores DOM/UIA (necesarios para
      auditoría técnica) pero se enmascara cualquier ``ocr_anchor``
      que parezca una query del usuario (>40 chars).

Para acceder a los datos crudos, llamar con ``redact=False``. La capa
es idéntica entre la API Python y el CLI:

    dash = ExecutionDashboard()
    dash.run_detail("run_id", expert=True, redact=False)  # explícito
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional

from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)
from app.services.missions.target_memory import (
    TargetMemory,
    get_target_memory,
)
from app.services.missions.strategy_scoring import (
    StrategyScoringEngine,
    get_strategy_scoring_engine,
)
from app.services.missions.execution_quality import (
    compute_execution_quality,
)


# ──────────────────────────────────────────────────────────────────────
# Redacción de datos sensibles
# ──────────────────────────────────────────────────────────────────────

# Claves que NO se redactan en state snapshots — son técnicas y
# necesarias para auditar fallos.
_STATE_SAFE_KEYS = {
    "active_app", "active_pid", "active_window_class",
    "active_window_bbox", "running_processes",
    "screen_size", "dpi_scale",
    "uia_targets_available", "dom_targets_available",
    "has_playwright_page",
    "detect_ms", "errors", "timestamp",
    "browser_domain",  # solo el host base, no la URL completa
}

# Claves que SÍ se redactan en state snapshots.
_STATE_SENSITIVE_KEYS = {
    "browser_url", "browser_title",
    "active_window_title",
    "visible_text", "uia_visible_names",
}

# Patrones para enmascarar tokens / queries muy largos en errores.
_TOKEN_PATTERNS = [
    # 1) Authorization: Bearer X.Y.Z (cubre JWT con puntos y otros)
    re.compile(
        r"(?i)(authorization)\s*[:=]\s*(bearer\s+)?[\w\-\.]+",
    ),
    # 2) Bearer X.Y.Z suelto
    re.compile(r"(?i)\bbearer\s+[\w\-\.]+"),
    # 3) token=, api_key=, password=
    re.compile(
        r"(?i)\b(token|api[_-]?key|password)\s*[:=]\s*\S+",
    ),
    # 4) JWT-style genérico: 3 grupos separados por punto, base64-ish
    re.compile(
        r"\b[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}\b",
    ),
    # 5) Cualquier token/hash de 32+ chars
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
]


def _redact_string(value: Any, max_len: int = 80) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    s = value
    for pat in _TOKEN_PATTERNS:
        s = pat.sub("***REDACTED***", s)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _redact_state_dict(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(state, dict):
        return state
    out: Dict[str, Any] = {}
    for k, v in state.items():
        if k in _STATE_SAFE_KEYS:
            out[k] = v
        elif k in _STATE_SENSITIVE_KEYS:
            if k == "browser_url":
                # Conservamos sólo el dominio sin path/query.
                if isinstance(v, str) and "://" in v:
                    try:
                        host = v.split("://", 1)[1].split("/", 1)[0]
                        out[k] = f"<host:{host}>"
                    except Exception:
                        out[k] = "<redacted>"
                else:
                    out[k] = "<redacted>"
            elif isinstance(v, list):
                out[k] = f"<{len(v)} items redacted>"
            else:
                out[k] = "<redacted>"
        else:
            # Conservadores con campos desconocidos.
            if isinstance(v, str) and len(v) > 60:
                out[k] = "<redacted>"
            else:
                out[k] = v
    return out


def _redact_attempt(a: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(a)
    if "state_before" in d:
        sb = d["state_before"]
        if isinstance(sb, str):
            try:
                sb = json.loads(sb)
            except Exception:
                pass
        d["state_before"] = _redact_state_dict(sb if isinstance(sb, dict) else None)
    if "state_after" in d:
        sa = d["state_after"]
        if isinstance(sa, str):
            try:
                sa = json.loads(sa)
            except Exception:
                pass
        d["state_after"] = _redact_state_dict(sa if isinstance(sa, dict) else None)
    if "error" in d:
        d["error"] = _redact_string(d.get("error"))
    # window_title puede contener nombres de archivo confidenciales,
    # nombres de cuenta, etc. Lo redactamos siempre en expert+redact;
    # los tests que necesiten el título usan ``redact=False``.
    if "window_title" in d and d.get("window_title"):
        d["window_title"] = "<redacted>"
    return d


def _redact_target(row: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(row)
    # Selectores DOM/UIA se conservan (ayudan al debug; no son del usuario).
    # Pero ocr_anchor largo puede contener texto del usuario → redactar.
    anchor = d.get("ocr_anchor")
    if isinstance(anchor, str) and len(anchor) > 40:
        d["ocr_anchor"] = "<redacted (>40 chars)>"
    return d


# ──────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────

class ExecutionDashboard:
    """Genera resúmenes de aprendizaje y ejecución para humanos."""

    def __init__(
        self,
        store: Optional[ExecutionMemoryStore] = None,
        *,
        scoring: Optional[StrategyScoringEngine] = None,
        targets: Optional[TargetMemory] = None,
    ) -> None:
        self.store = store or get_execution_memory_store()
        self.scoring = scoring or get_strategy_scoring_engine()
        self.targets = targets or get_target_memory()

    # ── History ────────────────────────────────────────────────
    def history(self, mission_id: Optional[str] = None, *, limit: int = 20) -> Dict[str, Any]:
        runs = self.store.list_runs(mission_id=mission_id, limit=limit)
        items = []
        for run in runs:
            attempts = self.store.list_step_attempts(run["run_id"])
            successes = sum(1 for a in attempts if a.get("success"))
            failures = len(attempts) - successes
            duration_ms = sum(int(a.get("duration_ms") or 0) for a in attempts)
            strategies_used = sorted({
                a.get("strategy_used") for a in attempts if a.get("strategy_used")
            })
            weak_steps = self._weak_step_summary(attempts)
            items.append({
                "run_id": run["run_id"],
                "mission_id": run.get("mission_id"),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "status": run.get("status"),
                "quality_score": run.get("quality_score"),
                "successes": successes,
                "failures": failures,
                "duration_ms": duration_ms,
                "strategies_used": list(strategies_used),
                "weak_steps": weak_steps,
            })
        return {"items": items}

    def run_detail(
        self, run_id: str, *,
        expert: bool = False, redact: bool = True,
    ) -> Dict[str, Any]:
        run = self.store.get_run(run_id) or {}
        attempts = self.store.list_step_attempts(run_id)
        normalized: List[Dict[str, Any]] = []
        for a in attempts:
            if expert:
                expanded = self._expand_attempt(a)
                normalized.append(_redact_attempt(expanded) if redact else expanded)
            else:
                row = {
                    "step_id": a.get("step_id"),
                    "step_kind": a.get("step_kind"),
                    "strategy_used": a.get("strategy_used"),
                    "success": bool(a.get("success")),
                    "retries": a.get("retries"),
                    "duration_ms": a.get("duration_ms"),
                    "failure_type": a.get("failure_type"),
                    "user_intervention": bool(a.get("user_intervention")),
                }
                if redact:
                    pass  # vista resumida ya no expone strings sensibles
                normalized.append(row)
        try:
            quality = compute_execution_quality(run_id, attempts=attempts)
            quality_dict = quality.to_dict()
        except Exception:
            quality_dict = None
        return {
            "run": run,
            "attempts": normalized,
            "quality": quality_dict,
            "redacted": bool(redact),
        }

    def strategy_table(
        self, step_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = self.store.list_strategy_scores(step_kind=step_kind)
        # Ordenamos por (step_kind asc, score desc).
        out = []
        for r in rows:
            ok = int(r.get("ok") or 0)
            fail = int(r.get("fail") or 0)
            score = self.scoring.score_strategy(
                step_kind=r["step_kind"], strategy=r["strategy"],
                scope_app=r.get("scope_app"), scope_domain=r.get("scope_domain"),
                scope_target=r.get("scope_target"),
            )
            out.append({
                "step_kind": r.get("step_kind"),
                "strategy": r.get("strategy"),
                "scope_app": r.get("scope_app"),
                "scope_domain": r.get("scope_domain"),
                "scope_target": r.get("scope_target"),
                "ok": ok, "fail": fail,
                "avg_duration_ms": r.get("avg_duration_ms"),
                "score": score.score,
                "components": score.components,
                "samples": score.samples,
            })
        out.sort(key=lambda r: (str(r.get("step_kind") or ""), -float(r.get("score") or 0)))
        return {"items": out}

    def corrections_table(self, *, redact: bool = True) -> Dict[str, Any]:
        """Lista de correcciones humanas guardadas.

        En modo redactado (default) **no** expone clicks (x, y), rutas
        de screenshots, ni texto cercano. Sólo conserva metadatos
        técnicos (step_kind, target, app, domain, contadores).
        """
        out: List[Dict[str, Any]] = []
        if self.store.is_persistent and self.store._conn is not None:  # type: ignore[union-attr]
            try:
                with self.store._lock:  # type: ignore[union-attr]
                    rows = self.store._conn.execute(  # type: ignore[union-attr]
                        "SELECT * FROM user_corrections ORDER BY created_at DESC"
                    ).fetchall()
                    for row in rows:
                        d = dict(row)
                        if redact:
                            for k in (
                                "click_x", "click_y",
                                "uia_blob", "dom_blob",
                                "visual_crop_path", "screenshot_context_path",
                                "nearest_text", "question",
                            ):
                                d.pop(k, None)
                            if "target_label" in d:
                                d["target_label"] = _redact_string(d["target_label"], max_len=40)
                        out.append(d)
            except Exception:
                pass
        else:
            for r in self.store._mem.get("user_corrections", []):
                d = dict(r)
                if redact:
                    for k in (
                        "click_x", "click_y", "uia_blob", "dom_blob",
                        "visual_crop_path", "screenshot_context_path",
                        "nearest_text", "question",
                    ):
                        d.pop(k, None)
                    if "target_label" in d:
                        d["target_label"] = _redact_string(d["target_label"], max_len=40)
                out.append(d)
        return {"items": out, "redacted": bool(redact)}

    def target_table(self, *, redact: bool = True) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        # Listamos todos los targets via SQL directo si tenemos conn,
        # si no, vamos al fallback de memoria.
        if self.store.is_persistent and self.store._conn is not None:  # type: ignore[union-attr]
            try:
                with self.store._lock:  # type: ignore[union-attr]
                    rows = self.store._conn.execute(  # type: ignore[union-attr]
                        "SELECT * FROM target_resolutions ORDER BY success_count DESC"
                    ).fetchall()
                    for row in rows:
                        d = dict(row)
                        for k in ("successful_selectors", "successful_uia"):
                            v = d.get(k)
                            if isinstance(v, str):
                                try:
                                    d[k] = json.loads(v)
                                except Exception:
                                    pass
                        if redact:
                            d = _redact_target(d)
                        out.append(d)
            except Exception:
                pass
        else:
            for r in self.store._mem.get("target_resolutions", []):
                d = dict(r)
                if redact:
                    d = _redact_target(d)
                out.append(d)
        return {"items": out, "redacted": bool(redact)}

    # ── Internals ─────────────────────────────────────────────
    def _expand_attempt(self, a: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(a)
        for k in ("state_before", "state_after"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
        return d

    def _weak_step_summary(self, attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Steps con más reintentos / más fallos."""
        by_step: Dict[str, Dict[str, Any]] = {}
        for a in attempts:
            sid = str(a.get("step_id") or "")
            entry = by_step.setdefault(sid, {
                "step_id": sid, "step_kind": a.get("step_kind"),
                "retries": 0, "failures": 0, "interventions": 0,
            })
            entry["retries"] = max(int(entry.get("retries") or 0),
                                    int(a.get("retries") or 0))
            if not a.get("success"):
                entry["failures"] = int(entry.get("failures") or 0) + 1
            if a.get("user_intervention"):
                entry["interventions"] = int(entry.get("interventions") or 0) + 1
        weak = [
            e for e in by_step.values()
            if int(e["retries"]) > 0 or int(e["failures"]) > 0 or int(e["interventions"]) > 0
        ]
        weak.sort(key=lambda e: (-int(e.get("retries") or 0),
                                  -int(e.get("failures") or 0)))
        return weak[:5]


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="execution_dashboard")
    parser.add_argument(
        "--no-redact", action="store_true",
        help=("Desactiva la redacción de datos potencialmente "
              "sensibles. Sólo para auditoría técnica autorizada."),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hist = sub.add_parser("history")
    p_hist.add_argument("--mission", default=None)
    p_hist.add_argument("--limit", type=int, default=20)

    p_run = sub.add_parser("run")
    p_run.add_argument("run_id")
    p_run.add_argument("--expert", action="store_true")

    p_strat = sub.add_parser("strategies")
    p_strat.add_argument("--kind", default=None)

    sub.add_parser("targets")
    sub.add_parser("corrections")

    args = parser.parse_args(argv)
    redact = not bool(args.no_redact)
    dash = ExecutionDashboard()

    if args.cmd == "history":
        _print(dash.history(args.mission, limit=int(args.limit)))
    elif args.cmd == "run":
        _print(dash.run_detail(
            args.run_id, expert=bool(args.expert), redact=redact,
        ))
    elif args.cmd == "strategies":
        _print(dash.strategy_table(args.kind))
    elif args.cmd == "targets":
        _print(dash.target_table(redact=redact))
    elif args.cmd == "corrections":
        _print(dash.corrections_table(redact=redact))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["ExecutionDashboard", "main"]
