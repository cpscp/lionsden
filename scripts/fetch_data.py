"""Build the static data layer for Lion's Den using only free services.

Primary football source: API-Football free tier (100 requests/day).
News: Sporting CP official site + Google News RSS aggregation.
Maps: OpenStreetMap/Nominatim, cached locally to avoid repeated geocoding.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

API_KEY = os.environ.get("API_FOOTBALL_KEY")
if not API_KEY:
    sys.exit("Missing API_FOOTBALL_KEY secret.")

BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY, "Accept": "application/json"}
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

TEAM_ID = 228  # Sporting CP
PRIMEIRA_LIGA = 94
SEASON = 2026
USER_AGENT = "LionsDen/2.0 (+https://github.com/cpscp/lionsden)"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def api(path: str, params: dict | None = None):
    r = session.get(BASE + path, headers=HEADERS, params=params or {}, timeout=25)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"API-Football: {payload['errors']}")
    return payload


def write_json(name: str, payload):
    payload = {"_updated_at": datetime.now(timezone.utc).isoformat(), **payload}
    (DATA / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_fixture(x):
    f = x["fixture"]
    l = x.get("league", {})
    h = x["teams"]["home"]
    a = x["teams"]["away"]
    return {
        "id": f.get("id"),
        "date": f.get("date"),
        "timestamp": f.get("timestamp"),
        "timezone": f.get("timezone"),
        "status": f.get("status", {}),
        "referee": f.get("referee"),
        "venue": f.get("venue") or {},
        "competition": {
            "id": l.get("id"), "name": l.get("name"), "logo": l.get("logo"),
            "round": l.get("round"), "season": l.get("season")
        },
        "home": {"id": h.get("id"), "name": h.get("name"), "logo": h.get("logo"), "winner": h.get("winner")},
        "away": {"id": a.get("id"), "name": a.get("name"), "logo": a.get("logo"), "winner": a.get("winner")},
        "goals": x.get("goals", {}),
        "score": x.get("score", {}),
    }


def fetch_schedule():
    # One call covers a rolling window around today, across all Sporting competitions.
    # This is deliberately used instead of separate next/last calls to protect the 100/day free quota.
    today = date.today()
    payload = api("/fixtures", {
        "team": TEAM_ID, "season": SEASON,
        "from": today.isoformat(),
        "to": (today + timedelta(days=60)).isoformat(),
    })
    normalized = [normalize_fixture(x) for x in payload.get("response", [])]
    # Add the previous five results with one additional call only during full refreshes via caller.
    normalized.sort(key=lambda x: x.get("timestamp") or 0)
    return normalized


def fetch_standings_and_team_stats():
    standings = api("/standings", {"league": PRIMEIRA_LIGA, "season": SEASON, "team": TEAM_ID})
    table = []
    for group in standings.get("response", []):
        for league in group.get("league", {}).get("standings", []):
            table.extend(league)
    team_stats = api("/teams/statistics", {"league": PRIMEIRA_LIGA, "season": SEASON, "team": TEAM_ID})
    write_json("standings.json", {"competition": "Liga Portugal", "table": table})
    write_json("team-stats.json", {"team": team_stats.get("response", {})})


def fetch_squad_and_player_stats():
    squad = api("/players/squads", {"team": TEAM_ID}).get("response", [])
    pages = []
    page = 1
    while True:
        payload = api("/players", {"team": TEAM_ID, "season": SEASON, "page": page})
        pages.extend(payload.get("response", []))
        total = int((payload.get("paging") or {}).get("total", 1))
        if page >= total:
            break
        page += 1
        time.sleep(0.25)
    stats_by_id = {}
    for item in pages:
        stats_by_id[item["player"]["id"]] = item

    players = []
    for item in squad:
        p = item.get("player", {})
        full = stats_by_id.get(p.get("id"), {})
        stats = full.get("statistics", [])
        players.append({
            "id": p.get("id"), "name": p.get("name"), "firstname": p.get("firstname"),
            "lastname": p.get("lastname"), "age": p.get("age"), "number": p.get("number"),
            "position": p.get("position"), "photo": p.get("photo"), "nationality": p.get("nationality"),
            "height": p.get("height"), "weight": p.get("weight"), "injured": p.get("injured"),
            "stats": stats,
        })
    # Preserve season players that API-Football reports even if squads endpoint lags behind.
    known = {p["id"] for p in players}
    for item in pages:
        p = item.get("player", {})
        if p.get("id") not in known:
            players.append({
                "id": p.get("id"), "name": p.get("name"), "firstname": p.get("firstname"),
                "lastname": p.get("lastname"), "age": p.get("age"), "number": None,
                "position": None, "photo": p.get("photo"), "nationality": p.get("nationality"),
                "height": p.get("height"), "weight": p.get("weight"), "injured": p.get("injured"),
                "stats": item.get("statistics", []),
            })
    players.sort(key=lambda p: (p.get("position") or "", p.get("name") or ""))
    write_json("squad.json", {"team_id": TEAM_ID, "players": players})


def geocode_venues(fixtures):
    cache_path = DATA / "venues.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    changed = False
    for fx in fixtures:
        v = fx.get("venue") or {}
        name, city = v.get("name"), v.get("city")
        if not name:
            continue
        key = f"{name}|{city or ''}"
        if key in cache:
            continue
        try:
            time.sleep(1.1)  # Nominatim courtesy/rate-limit spacing
            q = quote_plus(f"{name}, {city or ''}, Portugal")
            r = session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "jsonv2", "limit": 1},
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json()
            if rows:
                cache[key] = {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"]), "display": rows[0].get("display_name")}
            else:
                cache[key] = {"lat": None, "lon": None}
            changed = True
        except Exception as e:
            print(f"Geocode warning for {key}: {e}")
    if changed:
        write_json("venues.json", {"venues": cache})


def fetch_news():
    items = []
    # Official Sporting news page: robust link extraction, no dependence on a private API.
    try:
        r = session.get("https://www.sporting.pt/pt/noticias/futebol", timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            title = " ".join(a.stripped_strings)
            if href.startswith("/"):
                href = "https://www.sporting.pt" + href
            if "sporting.pt" not in href or "/noticias/" not in href or len(title) < 18:
                continue
            if href in seen:
                continue
            seen.add(href)
            items.append({"title": title[:180], "url": href, "source": "Sporting.pt"})
            if len(items) >= 10:
                break
    except Exception as e:
        print(f"Official news warning: {e}")

    # Google News RSS gives a free multi-source stream; only titles + links are stored.
    try:
        feed = feedparser.parse("https://news.google.com/rss/search?q=Sporting%20CP&hl=pt-PT&gl=PT&ceid=PT:pt-150")
        existing = {x["url"] for x in items}
        for entry in feed.entries[:20]:
            url = entry.get("link")
            title = entry.get("title")
            if not url or not title or url in existing:
                continue
            source = (entry.get("source") or {}).get("title") or "Google News"
            items.append({"title": title[:180], "url": url, "source": source, "published": entry.get("published")})
            if len(items) >= 24:
                break
    except Exception as e:
        print(f"News RSS warning: {e}")
    write_json("news.json", {"items": items})



def fetch_match_details(fixtures):
    # One batched call can return events, lineups, team stats and player stats for several fixtures.
    candidates = sorted(fixtures, key=lambda x: x.get("timestamp") or 0)
    completed = [x for x in candidates if x.get("status", {}).get("short") in {"FT", "AET", "PEN"}]
    upcoming = [x for x in candidates if x.get("status", {}).get("short") in {"NS", "TBD"}]
    ids = [x["id"] for x in completed[-3:] + upcoming[:2] if x.get("id")]
    if not ids:
        write_json("match-details.json", {"fixtures": []})
        return
    payload = api("/fixtures", {"ids": "-".join(map(str, ids))}).get("response", [])
    write_json("match-details.json", {"fixtures": payload})


def fetch_live_detail(fixtures):
    live = [f for f in fixtures if f.get("status", {}).get("short") in {"1H", "HT", "2H", "ET", "BT", "P"}]
    if not live:
        write_json("live.json", {"fixture": None})
        return
    ids = "-".join(str(f["id"]) for f in live[:5])
    payload = api("/fixtures", {"ids": ids}).get("response", [])
    write_json("live.json", {"fixtures": payload})


def main():
    fixtures = fetch_schedule()
    mode = os.environ.get("LIONS_DEN_MODE", "full")
    if mode == "full":
        past_payload = api("/fixtures", {"team": TEAM_ID, "season": SEASON, "last": 5})
        past = [normalize_fixture(x) for x in past_payload.get("response", [])]
        fixtures = {f["id"]: f for f in fixtures + past}
        fixtures = list(fixtures.values())
        fixtures.sort(key=lambda x: x.get("timestamp") or 0)
    write_json("fixtures.json", {"team_id": TEAM_ID, "fixtures": fixtures})
    geocode_venues(fixtures[:10])
    fetch_news()

    # Full data can be requested less frequently by the workflow to respect 100 calls/day.
    if mode == "full":
        fetch_standings_and_team_stats()
        fetch_squad_and_player_stats()
        fetch_match_details(fixtures)
    fetch_live_detail(fixtures)
    print(f"Updated Lion's Den ({mode})")


if __name__ == "__main__":
    main()
