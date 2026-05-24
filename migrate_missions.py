
import json
import shutil
from pathlib import Path

BASE_PATH = Path(r"C:\Users\Daniel Ramos\Desktop\ArthurOS2\ArthurOS\var\missions")

def migrate_step(step: dict) -> dict:
    # Si ya tiene event_type, asumimos que es nuevo o hibrido
    if "event_type" in step:
        return step

    # Migrar action_type -> event_type
    action_type = step.get("action_type")
    new_step = {
        "id": step.get("id"),
        "timestamp": step.get("timestamp"),
        "offset_ms": int(step.get("delay_before", 0) * 1000), # Approx mapping
        "delay_until_next_ms": 0,
        "metadata": step.get("metadata", {})
    }

    if action_type == "mouse_click":
        new_step["event_type"] = "mouse_click"
        new_step["mouse_action"] = {
            "x": int(step.get("x", 0)),
            "y": int(step.get("y", 0)),
            "button": "left" if "left" in str(step.get("text", "")).lower() else "right"
        }
    elif action_type == "key_press":
        new_step["event_type"] = "keyboard_key_press"
        # Old 'key' was a single char or 'Key.enter'
        k = step.get("key")
        new_step["keyboard_action"] = {
            "keys": [k] if k else [],
            "modifiers": step.get("modifiers", [])
        }
    else:
        # Fallback genérico
        new_step["event_type"] = action_type or "unknown"

    return new_step

def migrate_mission(file_path: Path):
    print(f"Migrating {file_path.name}...")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Basic Checks
        if "steps" not in data:
            print("  No steps found. Skipping.")
            return

        new_steps = []
        for s in data["steps"]:
            new_steps.append(migrate_step(s))
        
        data["steps"] = new_steps
        
        # Migrar root fields
        if "requires_confirmation" in data:
            data["safety"] = {
                "requires_confirmation": data.pop("requires_confirmation", False),
                "has_high_risk_actions": data.pop("high_risk", False)
            }
            
        if "run_count" in data:
            data["metadata"] = {
                "total_executions": data.pop("run_count", 0),
                "successful_executions": 0,
                "failed_executions": 0
            }
        
        # description null fix
        if data.get("description") is None:
            data["description"] = ""
            
        # Remove legacy fields
        for field in ["last_run_status", "run_count", "requires_confirmation", "high_risk"]:
            if field in data:
                del data[field]

        # Backup
        bak_path = file_path.with_suffix(".bak")
        shutil.copy(file_path, bak_path)
        print(f"  Backup created at {bak_path.name}")

        # Save
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print("  Migration successful.")

    except Exception as e:
        print(f"  Error migrating: {e}")

if __name__ == "__main__":
    if not BASE_PATH.exists():
        print(f"Path not found: {BASE_PATH}")
    else:
        for f in BASE_PATH.glob("*.json"):
            migrate_mission(f)
