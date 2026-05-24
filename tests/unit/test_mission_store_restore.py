from pathlib import Path

from app.contracts.mission import Mission
from app.services.missions.store import MissionStore


def test_restore_roundtrip(tmp_path: Path):
    store = MissionStore(base_path=tmp_path)
    mid = "rst-1"
    m = Mission(id=mid, name="R")
    store.save(m)

    bak = store.backup_mission_json(mid, tag="for_restore")
    assert bak is not None

    live = store._get_mission_file(mid)
    live.write_text("{}", encoding="utf-8")

    ok = store.restore_mission_json_from_backup(mid, Path(bak))
    assert ok is True
    fresh = store.load(mid)
    assert fresh is not None
    assert fresh.name == "R"
