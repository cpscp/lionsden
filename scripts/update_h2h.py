from pathlib import Path
import json
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_data import safe_existing, fetch_zerozero_h2h

fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
upcoming = sorted(
    [f for f in fixtures if f.get("status", {}).get("short") == "scheduled" and f.get("id")],
    key=lambda x: x.get("date") or 0,
)

existing_payload = safe_existing("h2h.json") or {}
existing = existing_payload.get("fixtures", {})
if not isinstance(existing, dict):
    existing = {}

KNOWN_H2H = {
    ("man united", "sporting cp"): {
        "matches": [
            {"id": None, "date": "2007-11-27", "home": "Manchester United", "away": "Sporting CP", "home_score": 2, "away_score": 1, "competition": "UEFA Champions League"},
            {"id": None, "date": "2007-09-19", "home": "Sporting CP", "away": "Manchester United", "home_score": 0, "away_score": 1, "competition": "UEFA Champions League"},
            {"id": None, "date": "1964-03-18", "home": "Sporting CP", "away": "Manchester United", "home_score": 5, "away_score": 0, "competition": "European Cup Winners' Cup"},
            {"id": None, "date": "1964-02-26", "home": "Manchester United", "away": "Sporting CP", "home_score": 4, "away_score": 1, "competition": "European Cup Winners' Cup"},
        ],
        "source": "UEFA historical H2H fallback",
    },
    ("fc porto", "sporting cp"): {
        "matches": [
            {"id": None, "date": "2026-04-22", "home": "FC Porto", "away": "Sporting CP", "home_score": 0, "away_score": 0, "competition": "Taça de Portugal"},
            {"id": None, "date": "2026-02-09", "home": "FC Porto", "away": "Sporting CP", "home_score": 1, "away_score": 1, "competition": "Liga Portugal Betclic"},
            {"id": None, "date": "2025-01-07", "home": "Sporting CP", "away": "FC Porto", "home_score": 1, "away_score": 0, "competition": "Taça da Liga"},
            {"id": None, "date": "2024-08-31", "home": "Sporting CP", "away": "FC Porto", "home_score": 2, "away_score": 0, "competition": "Liga Portugal Betclic"},
        ],
        "source": "ZeroZero historical H2H fallback",
    },
}

def norm(name):
    value = str(name or "").strip().lower()
    return {
        "manchester united": "man united",
        "man united": "man united",
        "fc porto": "fc porto",
        "porto": "fc porto",
        "sporting": "sporting cp",
        "sporting cp": "sporting cp",
    }.get(value, value)

def seed_for_fixture(f):
    home = norm((f.get("home") or {}).get("name"))
    away = norm((f.get("away") or {}).get("name"))
    return KNOWN_H2H.get(tuple(sorted((home, away))))

def build_entry(f, p):
    matches = p.get("matches") if isinstance(p.get("matches"), list) else []
    s = p.get("summary")
    if not isinstance(s, list) or len(s) < 3:
        s = None
    if s is None and matches:
        hw = dw = aw = 0
        home_name = norm((f.get("home") or {}).get("name"))
        for m in matches:
            hs, aas = m.get("home_score"), m.get("away_score")
            if hs is None or aas is None:
                continue
            if hs == aas:
                dw += 1
            elif norm(m.get("home")) == home_name:
                hw += 1
            elif norm(m.get("away")) == home_name:
                aw += 1
            else:
                continue
        s = [hw, dw, aw]
    if s is None:
        return None
    return {
        "matches": matches[:4],
        "home_wins": int(s[0]),
        "draws": int(s[1]),
        "away_wins": int(s[2]),
        "total": int(s[0]) + int(s[1]) + int(s[2]),
        "source": p.get("source"),
        "history_found": bool(matches or sum(s)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

live = fetch_zerozero_h2h(upcoming)
out = dict(existing)

for f in upcoming:
    fid = str(f["id"])
    p = live.get(fid)
    entry = build_entry(f, p) if isinstance(p, dict) and p.get("source_ok") else None

    if entry and entry["history_found"]:
        out[fid] = entry
        continue

    seed = seed_for_fixture(f)
    if seed:
        seeded = build_entry(f, {"matches": seed["matches"], "source": seed["source"]})
        if seeded:
            out[fid] = seeded
            continue

    if fid not in out:
        out[fid] = {
            "matches": [], "home_wins": 0, "draws": 0, "away_wins": 0,
            "total": 0, "source": "unavailable", "history_found": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

required = {}
for f in upcoming:
    names = {norm((f.get("home") or {}).get("name")), norm((f.get("away") or {}).get("name"))}
    if names in ({"lask", "sporting cp"}, {"man united", "sporting cp"}, {"fc porto", "sporting cp"}):
        required[str(f["id"])] = f

missing = [fid for fid in required if not out.get(fid, {}).get("history_found")]
if missing:
    raise RuntimeError(f"Critical H2H missing for fixture IDs: {', '.join(missing)}")

history_count = sum(1 for f in upcoming if out.get(str(f["id"]), {}).get("history_found"))
payload = {
    "_updated_at": datetime.now(timezone.utc).isoformat(),
    "fixtures": out,
    "source": "ZeroZero all-competitions H2H with SofaScore/FotMob fallback + non-destructive cache",
    "scope": "All upcoming Sporting CP fixtures; latest 4 meetings across all competitions.",
    "checked_fixtures": len(upcoming),
    "enriched_fixtures": history_count,
}

(ROOT / "data" / "h2h.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"H2H update completed: {history_count}/{len(upcoming)} fixtures with history")
