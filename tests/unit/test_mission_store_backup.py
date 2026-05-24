from pathlib import Path

from app.contracts.mission import Mission
from app.services.missions.store import MissionStore


def test_backup_mission_json_creates_sibling_backup(tmp_path: Path):
    store = MissionStore(base_path=tmp_path)
    mid = "mission-backup-unit"
    store.save(Mission(id=mid, name="Unit backup"))
    json_path = store._get_mission_file(mid)
    assert json_path.exists()

    bak = store.backup_mission_json(mid, tag="unit")
    assert bak is not None
    assert bak.parent == json_path.parent
    assert bak.exists()
    assert ".bak-unit_" in bak.name
    assert bak.read_text(encoding="utf-8") == json_path.read_text(encoding="utf-8")
