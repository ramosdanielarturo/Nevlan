"""Estadísticas de ejecución en runtime persistidas en ``target_context.confidence``."""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.contracts.mission import CompiledStep


def merge_runtime_feedback(
    step: CompiledStep,
    success: bool,
    *,
    resolver: str = "",
    premium_fallback: str = "",
    premium_meta: Optional[Dict[str, Any]] = None,
    error_snip: str = "",
    elapsed_ms: float = 0.0,
) -> None:
    """Actualiza ``runtime_stats`` en el bundle de confianza (se guarda al persistir misión)."""
    tc = step.target_context
    cf: Dict[str, Any] = dict(tc.confidence or {})
    rt: Dict[str, Any] = dict(cf.get("runtime_stats") or {})
    rt["runs"] = int(rt.get("runs") or 0) + 1
    rt["successes"] = int(rt.get("successes") or 0) + (1 if success else 0)
    rt["failures"] = int(rt.get("failures") or 0) + (0 if success else 1)
    rt["fail_streak"] = 0 if success else int(rt.get("fail_streak") or 0) + 1
    rt["last_success"] = bool(success)
    if resolver:
        rt["last_resolver"] = resolver[:120]
    if premium_fallback:
        rt["last_premium_fallback"] = premium_fallback[:80]
    if isinstance(premium_meta, dict) and premium_meta:
        snap = {
            k: premium_meta.get(k)
            for k in (
                "region_used",
                "visual_match_score",
                "image_used",
                "fallback_used",
                "offset_applied",
            )
            if k in premium_meta
        }
        if snap:
            rt["last_premium_meta"] = snap
    if error_snip and not success:
        rt["last_error_snip"] = error_snip[:220]
    if elapsed_ms > 0:
        rt["last_elapsed_ms"] = float(elapsed_ms)
        prev = float(rt.get("avg_step_ms") or 0.0)
        n = float(rt["runs"])
        rt["avg_step_ms"] = ((prev * (n - 1)) + elapsed_ms) / max(1.0, n)
    cf["runtime_stats"] = rt
    # Aprendizaje por estrategia (clave = resolver/strategy string del runtime).
    try:
        if resolver:
            key = resolver[:160]
            st = cf.get("strategy_success_stats") or {}
            if not isinstance(st, dict):
                st = {}
            cur = dict(st.get(key) or {})
            cur["runs"] = int(cur.get("runs") or 0) + 1
            cur["ok"] = int(cur.get("ok") or 0) + (1 if success else 0)
            cur["fail"] = int(cur.get("fail") or 0) + (0 if success else 1)
            cur["fail_streak"] = 0 if success else int(cur.get("fail_streak") or 0) + 1
            if success:
                cur["preferred"] = True  # marcador; priorización en resolver se lee después
                cf["preferred_runtime_strategy"] = key[:120]
            st[key] = cur
            cf["strategy_success_stats"] = st
    except Exception:
        pass

    tc.confidence = cf
