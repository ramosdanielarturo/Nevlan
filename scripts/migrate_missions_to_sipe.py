"""Migración 1-shot del store: aplica el Semantic Intent Promotion
Engine sobre todas las misiones persistidas y reescribe el JSON.

Uso
---

    python scripts/migrate_missions_to_sipe.py            # dry-run
    python scripts/migrate_missions_to_sipe.py --apply    # escribe a disco

Comportamiento
--------------

* **Dry-run (default).** Carga cada misión, la pasa por SIPE en una
  copia profunda, y reporta qué cambiaría. NO toca el disco.
* **--apply.** Crea un backup ``.bak-sipe_<ts>.json`` antes de
  reescribir cada misión vía ``MissionStore.save``.
* **Idempotente.** Las misiones cuyo
  ``semantic_execution_plan.source`` ya sea
  ``"semantic_intent_promotion_engine"`` se reportan como
  ``already_promoted`` y se saltan (a menos que pases ``--force``).
* **Tolerante a fallos.** Un error en una misión no aborta el batch:
  cada fila del reporte queda con ``ok=False`` y el motivo en
  ``error``. La misión queda intacta en disco.

Filtros
-------

* ``--mission-id <id>``  procesa solo esa misión.
* ``--limit <n>``        procesa como mucho ``n`` misiones.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List

from app.services.missions.bulk_recompile import (
    SipeMigrationRow,
    sipe_migrate_missions,
)
from app.services.missions.store import MissionStore


def _print_row(row: SipeMigrationRow) -> None:
    tag = "ok" if row.ok else "FAIL"
    if row.already_promoted:
        tag = "skip(already)"
    before = ",".join(row.steps_before) or "-"
    after = ",".join(row.steps_after) or "-"
    extra = ""
    if row.error:
        extra = f"  error={row.error}"
    elif row.blockers:
        extra = f"  blockers={','.join(row.blockers)}"
    print(
        f"  [{tag:<14}] {row.mission_id}  "
        f"status={row.intent_status or '-':<14}  "
        f"before=[{before}]  after=[{after}]{extra}"
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Reescribe los JSON en disco (default: dry-run).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-promueve misiones que ya estaban migradas.",
    )
    parser.add_argument(
        "--mission-id", type=str, default="",
        help="Procesa solo esta misión.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Procesa como mucho N misiones (0 = todas).",
    )
    args = parser.parse_args(argv)

    store = MissionStore()
    print(f"Mission store: {store.base_path}")
    if args.mission_id:
        m = store.load(args.mission_id)
        if m is None:
            print(f"  Mission '{args.mission_id}' not found.")
            return 2
        missions = [m]
    else:
        missions = store.list_all()

    if args.limit and len(missions) > args.limit:
        missions = missions[: args.limit]

    print(f"Loaded {len(missions)} mission(s).")
    print(
        f"Mode: {'APPLY (will write to disk)' if args.apply else 'DRY-RUN'}"
    )
    if args.apply:
        print("Backups: .bak-sipe_<ts>.json per mission.")
    print()

    rows = sipe_migrate_missions(
        missions,
        in_place=bool(args.apply),
        skip_already_promoted=not args.force,
    )

    n_ok = sum(1 for r in rows if r.ok and not r.already_promoted)
    n_skip = sum(1 for r in rows if r.already_promoted)
    n_fail = sum(1 for r in rows if not r.ok)

    print("Per-mission results:")
    for row in rows:
        _print_row(row)
    print()
    print(
        f"Summary: migrated={n_ok}  already_promoted={n_skip}  "
        f"failed={n_fail}  total={len(rows)}"
    )

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to write changes.")
        return 0

    # Persistir resultados.
    print()
    print("Persisting...")
    written = 0
    for row in rows:
        if not row.ok or row.migrated_mission is None:
            continue
        if row.already_promoted and not args.force:
            continue
        try:
            store.backup_mission_json(row.mission_id, tag="sipe")
            store.save(row.migrated_mission)
            written += 1
        except Exception as e:
            print(f"  WRITE FAILED {row.mission_id}: {e}")

    print(f"Done. {written} mission(s) written to disk.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
