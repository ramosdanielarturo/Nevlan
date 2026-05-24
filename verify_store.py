
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.missions.store import mission_store

print("--- Verificando MissionStore ---")
print(f"Base path: {mission_store.base_path}")
missions = mission_store.list_all()
print(f"Misiones encontradas: {len(missions)}")
for m in missions:
    print(f"- {m.name} (File ID: {m.id})")

if any("Cinemex" in m.name for m in missions):
    print("SUCCESS: Cinemex Pedregal found.")
else:
    print("FAILURE: Cinemex Pedregal NOT found.")
