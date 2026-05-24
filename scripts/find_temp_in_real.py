"""Find /temp/ paths in the real mission JSON."""
import json
import re
from pathlib import Path

PATH = Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json")
data = json.loads(PATH.read_text(encoding="utf-8"))
text = json.dumps(data, ensure_ascii=False)
hits = set(re.findall(r"[^\"]*?temp[/\\][^\"\s]+", text, flags=re.IGNORECASE))
print(f"Total /temp-ish/ matches: {len(hits)}")
for h in sorted(hits):
    print(f"  {h}")
