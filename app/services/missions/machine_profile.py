"""
ArthurOS Services - MachineProfile (Auto-Learning Executor)
-----------------------------------------------------------
Perfil de la máquina donde corre Nevlan, con métricas de fiabilidad
por técnica (UIA / DOM / OCR / Vision).

Por qué:
  - En PCs distintos, distintas técnicas funcionan: el mismo Chrome con
    debug puerto 9222 hace el DOM trivial; sin debug, hay que ir a UIA.
  - Permite que el AutoLearningExecutor ajuste el orden por máquina.

Atributos:
  - machine_id (estable: hash(MAC + hostname))
  - screen_size (W x H)
  - dpi_scale (1.0, 1.25, 1.5, ...)
  - os_version (string corta)
  - browser ("chrome", "edge", "firefox" si detectable)
  - chrome_debug_available (bool)
  - {uia,dom,ocr,vision}_reliability_score (0..1)

Las "reliability" son **persistentes** vía
``ExecutionMemoryStore.machine_metrics`` y se actualizan con
``observe(strategy, success)``. El score usa suavizado bayesiano
(Beta(2,2)) para que una primera observación no fije el extremo.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.logger import log
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)


# ──────────────────────────────────────────────────────────────────────
# Detect helpers
# ──────────────────────────────────────────────────────────────────────

def _machine_id() -> str:
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    try:
        # uuid.getnode devuelve 48-bit MAC del primer adapter (estable
        # en general). Si falla, cae a un uuid random consistente con
        # la sesión.
        node = uuid.getnode()
    except Exception:
        node = 0
    raw = f"{host}|{node}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _detect_screen_size() -> Optional[tuple]:
    # Intentamos varios providers en orden, defensivos.
    try:
        import ctypes  # type: ignore
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if w and h:
            return (int(w), int(h))
    except Exception:
        pass
    try:
        import pyautogui  # type: ignore
        s = pyautogui.size()
        return (int(s.width), int(s.height))
    except Exception:
        pass
    return None


def _detect_dpi_scale() -> Optional[float]:
    try:
        import ctypes  # type: ignore
        shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
        # 0 = MDT_EFFECTIVE_DPI
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        # GetDpiForSystem (1607+) si existe
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            d = user32.GetDpiForSystem()
            if d:
                return round(float(d) / 96.0, 2)
        except Exception:
            pass
        # Fallback: monitor 0
        h_mon = ctypes.windll.user32.MonitorFromPoint(  # type: ignore[attr-defined]
            ctypes.wintypes.POINT(0, 0) if hasattr(ctypes, "wintypes") else 0,
            2,
        )
        shcore.GetDpiForMonitor(h_mon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return round(float(dpi_x.value) / 96.0, 2)
    except Exception:
        return None


def _detect_chrome_debug_available(port: int = 9222, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _detect_browser_default() -> str:
    # Heurística simple basada en presencia de chrome.exe / msedge.exe.
    try:
        import shutil
        if shutil.which("chrome") or shutil.which("chrome.exe"):
            return "chrome"
        if shutil.which("msedge") or shutil.which("msedge.exe"):
            return "edge"
        if shutil.which("firefox"):
            return "firefox"
    except Exception:
        pass
    return "unknown"


# ──────────────────────────────────────────────────────────────────────
# DTO
# ──────────────────────────────────────────────────────────────────────

@dataclass
class MachineProfileSnapshot:
    machine_id: str
    screen_size: Optional[tuple] = None
    dpi_scale: Optional[float] = None
    os_name: str = ""
    os_version: str = ""
    browser: str = "unknown"
    chrome_debug_available: bool = False
    uia_reliability_score: float = 0.5
    dom_reliability_score: float = 0.5
    ocr_reliability_score: float = 0.5
    vision_reliability_score: float = 0.5
    refreshed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "screen_size": list(self.screen_size) if self.screen_size else None,
            "dpi_scale": self.dpi_scale,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "browser": self.browser,
            "chrome_debug_available": self.chrome_debug_available,
            "uia_reliability_score": self.uia_reliability_score,
            "dom_reliability_score": self.dom_reliability_score,
            "ocr_reliability_score": self.ocr_reliability_score,
            "vision_reliability_score": self.vision_reliability_score,
            "refreshed_at": self.refreshed_at,
        }


# ──────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────

_TECHNIQUE_KEYS = ("uia", "dom", "ocr", "vision")
_METRIC_OK = "_ok"
_METRIC_FAIL = "_fail"


class MachineProfile:
    """Perfil de la máquina + reliability scores por técnica."""

    def __init__(self, store: Optional[ExecutionMemoryStore] = None) -> None:
        self.store = store or get_execution_memory_store()
        self.machine_id = _machine_id()
        self._cached_snapshot: Optional[MachineProfileSnapshot] = None

    # ── API pública ────────────────────────────────────────────
    def snapshot(self, force_refresh: bool = False) -> MachineProfileSnapshot:
        if self._cached_snapshot and not force_refresh:
            return self._cached_snapshot
        screen = _detect_screen_size()
        dpi = _detect_dpi_scale()
        os_name = platform.system() or ""
        os_version = platform.release() or ""
        browser = _detect_browser_default()
        chrome_debug = _detect_chrome_debug_available()
        uia_r = self._compute_reliability("uia")
        dom_r = self._compute_reliability("dom")
        ocr_r = self._compute_reliability("ocr")
        vis_r = self._compute_reliability("vision")
        snap = MachineProfileSnapshot(
            machine_id=self.machine_id,
            screen_size=screen,
            dpi_scale=dpi,
            os_name=os_name,
            os_version=os_version,
            browser=browser,
            chrome_debug_available=chrome_debug,
            uia_reliability_score=uia_r,
            dom_reliability_score=dom_r,
            ocr_reliability_score=ocr_r,
            vision_reliability_score=vis_r,
        )
        self._cached_snapshot = snap
        return snap

    def observe(self, technique: str, *, success: bool) -> None:
        """Acumula un éxito/fallo para una técnica.

        ``technique`` puede ser exactamente "uia"/"dom"/"ocr"/"vision" o
        un nombre de estrategia compuesto (lo mapeamos por substring).
        """
        key = _technique_key(technique)
        if not key:
            return
        try:
            metrics = self.store.get_machine_metrics(self.machine_id)
            ok = float(metrics.get(f"{key}{_METRIC_OK}", 0.0))
            fail = float(metrics.get(f"{key}{_METRIC_FAIL}", 0.0))
            if success:
                ok += 1.0
            else:
                fail += 1.0
            self.store.update_machine_metric(
                machine_id=self.machine_id, metric=f"{key}{_METRIC_OK}", value=ok,
            )
            self.store.update_machine_metric(
                machine_id=self.machine_id, metric=f"{key}{_METRIC_FAIL}", value=fail,
            )
        except Exception as e:
            log.debug(f"MachineProfile.observe falló: {e}")

    def reliability(self, technique: str) -> float:
        return self._compute_reliability(_technique_key(technique) or "")

    # ── Strategy ranking helper ───────────────────────────────
    def adjust_strategy_order(self, strategies: list) -> list:
        """Reordena estrategias usando reliability + flags de máquina.

        Reglas:
          - Si ``chrome_debug_available=False``, **bajamos** prioridad de
            estrategias que contengan "dom"/"playwright" (el DOM no es
            confiable sin debug).
          - Subimos las que contengan ``technique`` con mejor reliability.
          - Estable en empates: respetamos orden original.
        """
        snap = self.snapshot()
        scored = []
        for idx, s in enumerate(strategies):
            score = 0.0
            ks = (s or "").lower()
            if "dom" in ks or "playwright" in ks:
                if not snap.chrome_debug_available:
                    score -= 0.3
                score += snap.dom_reliability_score
            if "uia" in ks:
                score += snap.uia_reliability_score
            if "ocr" in ks:
                score += snap.ocr_reliability_score
            if "vision" in ks:
                score += snap.vision_reliability_score
            scored.append((-score, idx, s))
        scored.sort()
        return [s for _, _, s in scored]

    # ── Internals ─────────────────────────────────────────────
    def _compute_reliability(self, key: str) -> float:
        if not key:
            return 0.5
        try:
            metrics = self.store.get_machine_metrics(self.machine_id)
            ok = float(metrics.get(f"{key}{_METRIC_OK}", 0.0))
            fail = float(metrics.get(f"{key}{_METRIC_FAIL}", 0.0))
        except Exception:
            ok = fail = 0.0
        # Beta(2,2) → punto neutro = 0.5, pero menos sensible al primer
        # dato.
        return (ok + 2.0) / (ok + fail + 4.0)


def _technique_key(technique: str) -> str:
    if not technique:
        return ""
    s = str(technique).lower()
    for k in _TECHNIQUE_KEYS:
        if k in s:
            return k
    # Aliases.
    if "playwright" in s or "selector" in s:
        return "dom"
    if "image" in s or "screenshot_match" in s:
        return "vision"
    return ""


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_profile: Optional[MachineProfile] = None


def get_machine_profile() -> MachineProfile:
    global _profile
    if _profile is None:
        _profile = MachineProfile()
    return _profile


def reset_machine_profile_for_tests() -> None:
    global _profile
    _profile = None


__all__ = [
    "MachineProfile",
    "MachineProfileSnapshot",
    "get_machine_profile",
    "reset_machine_profile_for_tests",
]
