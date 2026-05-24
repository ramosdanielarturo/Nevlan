#!/usr/bin/env python3
"""Valida el baseline certificado FASE 7 congelado en fixtures.

Exit codes:
    0 — baseline pasa gate batch
    1 — archivo ausente o JSON inválido
    2 — violaciones gate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.runtime.fase7_release_gate import (  # noqa: E402
    FASE7_ACCEPTANCE_STATUS,
    fase7_batch_gate_violations,
)
from tests.fixtures.fase7_certified_release import (  # noqa: E402
    CERTIFIED_LIVE_BASELINE_PATH,
    FASE7_CERTIFIED_RELEASE_ID,
    FASE7_CERTIFIED_REQUIRED_RUNS,
    FASE7_CERTIFIED_TOTAL_RUNS,
)


def main() -> int:
    path = CERTIFIED_LIVE_BASELINE_PATH
    if not path.is_file():
        print(f"missing certified baseline: {path}", file=sys.stderr)
        return 1

    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1

    violations = fase7_batch_gate_violations(
        blob,
        required_runs=FASE7_CERTIFIED_REQUIRED_RUNS,
    )
    if violations:
        print(f"FASE7 CERTIFIED BASELINE VIOLATIONS ({FASE7_CERTIFIED_RELEASE_ID}):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    aggregate = dict(blob.get("aggregate") or {})
    accepted = int(aggregate.get("accepted_runs") or 0)
    if accepted != FASE7_CERTIFIED_TOTAL_RUNS:
        print(
            f"accepted_runs mismatch: {accepted} != {FASE7_CERTIFIED_TOTAL_RUNS}",
            file=sys.stderr,
        )
        return 2

    if str(blob.get("release_status") or "") != FASE7_ACCEPTANCE_STATUS:
        print(f"release_status not ACCEPTED: {blob.get('release_status')!r}", file=sys.stderr)
        return 2

    print(
        f"OK {FASE7_CERTIFIED_RELEASE_ID}: "
        f"{accepted}/{FASE7_CERTIFIED_TOTAL_RUNS} ACCEPTED, gate clean",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
