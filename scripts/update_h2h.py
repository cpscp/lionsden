from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_data import safe_existing, fetch_zerozero_h2h, write_json

fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
upcoming = [f for f in fixtures if f.get("status", {}).get("short") == "scheduled" and f.get("id")]
upcoming = sorted(upcoming, key=lambda x: x.get("date") or 0)[:30]

existing = safe_existing("match-context.json") or {}
contexts = existing.get("fixtures") if isinstance(existing.get("fixtures"), dict) else {}
h2h = fetch_zerozero_h2h(upcoming)

for f in upcoming:
    fid = str(f["id"])
    p = h2h.get(fid)
    if p is None or not p.get("source_ok"):
        # Never turn a transient source failure into a fake 0-0-0 history.
        continue
    s = p.get("summary")
    if s is None:
        s = [0, 0, 0]
    contexts.setdefault(fid, {})["h2h"] = {
        "matches": p.get("matches", [])[:4],
        "home_wins": int(s[0]),
        "draws": int(s[1]),
        "away_wins": int(s[2]),
        "total": int(s[0]) + int(s[1]) + int(s[2]),
        "source": p.get("source"),
        "history_found": bool(p.get("history_found")),
    }

write_json("match-context.json", {
    "fixtures": contexts,
    "source": "FotMob · current standings · team form · ZeroZero H2H",
    "h2h_source": "ZeroZero — all competitions",
})
print("ZeroZero H2H update completed for", len(upcoming), "scheduled fixtures")
