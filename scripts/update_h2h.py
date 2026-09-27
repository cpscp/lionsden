from pathlib import Path
import json
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_data import safe_existing, fetch_zerozero_h2h

fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
upcoming = [
    f for f in fixtures
    if f.get("status", {}).get("short") == "scheduled" and f.get("id")
]
upcoming = sorted(upcoming, key=lambda x: x.get("date") or 0)[:30]

h2h = fetch_zerozero_h2h(upcoming)
out = {}

for f in upcoming:
    fid = str(f["id"])
    p = h2h.get(fid)
    if not p or not p.get("source_ok"):
        continue

    s = p.get("summary")
    if not isinstance(s, list) or len(s) < 3:
        s = [0, 0, 0]

    out[fid] = {
        "matches": p.get("matches", [])[:4],
        "home_wins": int(s[0]),
        "draws": int(s[1]),
        "away_wins": int(s[2]),
        "total": int(s[0]) + int(s[1]) + int(s[2]),
        "source": p.get("source"),
        "history_found": bool(p.get("history_found")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

payload = {
    "_updated_at": datetime.now(timezone.utc).isoformat(),
    "fixtures": out,
    "source": "ZeroZero all-competitions H2H with SofaScore/FotMob fallback",
    "scope": "All upcoming Sporting CP fixtures; latest 4 meetings across all competitions.",
    "checked_fixtures": len(upcoming),
    "enriched_fixtures": len(out),
}

(ROOT / "data" / "h2h.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"H2H update completed: {len(out)}/{len(upcoming)} fixtures enriched")
