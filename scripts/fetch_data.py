"""
Lion's Den — zero-cost data pipeline.

Sources:
- Football Soccer API (free key): current/upcoming fixtures, venue coordinates/capacity,
  referee and match detail. We deliberately stay within the free plan's recent-data window.
- FBref: current-season Sporting player/team statistics, squad and Primeira Liga table.
- Sporting.pt + Google News RSS: news.
- OpenStreetMap/Nominatim: fallback venue geocoding.

Important:
The football API key is only used inside GitHub Actions. It is never shipped to the browser.
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

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

FSAPI_KEY = os.environ.get("FSAPI_KEY")
FSAPI_BASE = "https://api.footballsoccerapi.com/v1"
FBREF_TEAM = "https://fbref.com/en/squads/13dc44fd/2026-2027/all_comps/Sporting-CP-Stats-All-Competitions"
FBREF_TEAM_ROSTER = "https://fbref.com/en/squads/13dc44fd/2026-2027/roster/Sporting-CP-Roster-Details"
FBREF_LEAGUE = "https://fbref.com/en/comps/32/stats/Primeira-Liga-Stats"
FBREF_SCHEDULE = "https://fbref.com/en/squads/13dc44fd/2026-2027/matchlogs/all_comps/schedule/Sporting-CP-Scores-and-Fixtures-All-Competitions"
FBREF_LEAGUE_SCHEDULE = "https://fbref.com/en/comps/32/schedule/Primeira-Liga-Scores-and-Fixtures"
SPORTING_STADIUM = "Estádio José Alvalade"
SPORTING_NEWS = "https://www.sporting.pt/pt/noticias/futebol"
USER_AGENT = "Mozilla/5.0 (compatible; LionsDen/3.0; +https://github.com/cpscp/lionsden)"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_json(name, payload):
    out = {"_updated_at": now_iso(), **payload}
    (DATA / name).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_existing(name):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def fsapi(path, params=None):
    if not FSAPI_KEY:
        raise RuntimeError("Missing FSAPI_KEY GitHub secret.")
    r = session.get(
        FSAPI_BASE + path,
        headers={"X-API-Key": FSAPI_KEY, "Accept": "application/json"},
        params=params or {},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload


def fnum(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().replace(",", ".")
    if s in {"", "—", "-", "nan", "None"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    if s in {"nan", "None"}:
        return ""
    return re.sub(r"\s+", " ", s)


def fbref_html(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def read_fbref_tables(url):
    html = fbref_html(url)
    chunks = [html]
    soup = BeautifulSoup(html, "html.parser")
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        if "<table" in c:
            chunks.append(str(c))
    tables = []
    for chunk in chunks:
        try:
            tables.extend(pd.read_html(chunk))
        except Exception:
            pass
    # de-duplicate by shape + columns + first row
    out, seen = [], set()
    for df in tables:
        key = (tuple(map(str, df.columns)), len(df), tuple(map(str, df.iloc[0].tolist())) if len(df) else ())
        if key not in seen:
            seen.add(key)
            out.append(df)
    return out, html


def pick_table(tables, required_columns):
    req = {str(x) for x in required_columns}
    for df in tables:
        cols = {str(c) for c in df.columns}
        if req.issubset(cols):
            return df.copy()
    return None


def multi_index_flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            " ".join(str(x) for x in col if str(x) != "nan").strip()
            for col in df.columns
        ]
    else:
        df.columns = [str(c) for c in df.columns]
    return df


def normalize_player_name(v):
    s = clean(v)
    s = re.sub(r"^[^A-Za-zÀ-ÿ0-9]+", "", s)
    return s


def fetch_fbref_stats():
    tables, team_html = read_fbref_tables(FBREF_TEAM)
    for df in tables:
        multi_index_flatten(df)

    standard = pick_table(tables, ["Player", "MP", "Starts", "Min", "Gls", "Ast"])
    shooting = pick_table(tables, ["Player", "Sh", "SoT"])
    playing = pick_table(tables, ["Player", "Min", "Starts", "Subs"])
    keepers = pick_table(tables, ["Player", "GA", "Saves", "CS"])
    misc = pick_table(tables, ["Player", "CrdY", "CrdR"])
    fixtures = pick_table(tables, ["Date", "Comp", "Venue", "Result", "GF", "GA", "Opponent", "Referee"])

    if standard is None:
        raise RuntimeError("FBref standard player table not found.")

    def rows_for(df):
        if df is None:
            return {}
        out = {}
        for _, r in df.iterrows():
            name = normalize_player_name(r.get("Player"))
            if not name or name == "Squad Total":
                continue
            out[name] = {clean(k): (None if pd.isna(v) else v) for k, v in r.to_dict().items()}
        return out

    sh = rows_for(shooting)
    pt = rows_for(playing)
    kg = rows_for(keepers)
    mi = rows_for(misc)

    # Parse roster photos/profile links. This page is small and usually more stable than individual requests.
    photo_map = {}
    profile_map = {}
    try:
        roster_tables, roster_html = read_fbref_tables(FBREF_TEAM_ROSTER)
        soup = BeautifulSoup(roster_html, "html.parser")
        for a in soup.select('a[href*="/en/players/"]'):
            name = normalize_player_name(a.get_text(" ", strip=True))
            if not name:
                continue
            href = a.get("href")
            if href and href.startswith("/"):
                href = "https://fbref.com" + href
            img = a.find_previous("img")
            if img and img.get("src"):
                photo_map[name] = img.get("src")
            profile_map[name] = href
    except Exception as e:
        print("Roster enrichment warning:", e)

    players = []
    for _, r in standard.iterrows():
        name = normalize_player_name(r.get("Player"))
        if not name or name == "Squad Total":
            continue
        s = {clean(k): (None if pd.isna(v) else v) for k, v in r.to_dict().items()}
        s2 = sh.get(name, {})
        s3 = pt.get(name, {})
        s4 = kg.get(name, {})
        s5 = mi.get(name, {})
        players.append({
            "name": name,
            "nation": clean(s.get("Nation")),
            "position": clean(s.get("Pos")),
            "age": clean(s.get("Age")),
            "matches": int(fnum(s.get("MP")) or 0),
            "starts": int(fnum(s.get("Starts")) or 0),
            "minutes": int(fnum(s.get("Min")) or 0),
            "goals": int(fnum(s.get("Gls")) or 0),
            "assists": int(fnum(s.get("Ast")) or 0),
            "yellow": int(fnum(s.get("CrdY")) or fnum(s5.get("CrdY")) or 0),
            "red": int(fnum(s.get("CrdR")) or fnum(s5.get("CrdR")) or 0),
            "shots": int(fnum(s2.get("Sh")) or 0),
            "shots_on_target": int(fnum(s2.get("SoT")) or 0),
            "saves": int(fnum(s4.get("Saves")) or 0),
            "goals_against": int(fnum(s4.get("GA")) or 0),
            "clean_sheets": int(fnum(s4.get("CS")) or 0),
            "photo": photo_map.get(name),
            "profile": profile_map.get(name),
        })

    # Team-level values from the player tables + schedule.
    team = {
        "matches": sum(1 for _, r in fixtures.iterrows() if clean(r.get("Result"))) if fixtures is not None else 0,
        "goals": sum(int(fnum(r.get("GF")) or 0) for _, r in fixtures.iterrows()) if fixtures is not None else 0,
        "goals_against": sum(int(fnum(r.get("GA")) or 0) for _, r in fixtures.iterrows()) if fixtures is not None else 0,
        "assists": sum(p["assists"] for p in players),
        "shots": sum(p["shots"] for p in players),
        "shots_on_target": sum(p["shots_on_target"] for p in players),
        "minutes": sum(p["minutes"] for p in players),
        "clean_sheets": sum(p["clean_sheets"] for p in players if p["position"] == "GK"),
    }

    # Possession is available on the match-log table; average only numeric values.
    poss = []
    if fixtures is not None and "Poss" in fixtures.columns:
        for v in fixtures["Poss"].tolist():
            n = fnum(v)
            if n is not None:
                poss.append(n)
    team["possession_avg"] = round(sum(poss) / len(poss), 1) if poss else None

    # Form from the most recent completed matches.
    form = []
    recent_results = []
    if fixtures is not None:
        for _, r in fixtures.iterrows():
            result = clean(r.get("Result"))
            if result in {"W", "D", "L"}:
                form.append(result)
                recent_results.append({
                    "date": clean(r.get("Date")),
                    "competition": clean(r.get("Comp")),
                    "venue": SPORTING_STADIUM if clean(r.get("Venue")) == "Home" else clean(r.get("Venue")),
                    "result": result,
                    "gf": int(fnum(r.get("GF")) or 0),
                    "ga": int(fnum(r.get("GA")) or 0),
                    "opponent": clean(r.get("Opponent")),
                    "referee": clean(r.get("Referee")),
                })
    team["form"] = form[-6:]
    team["recent_matches"] = recent_results[-6:]

    # Full schedule from FBref is useful as a fallback for dates beyond the free API's window.
    schedule_rows = []
    if fixtures is not None:
        for _, r in fixtures.iterrows():
            d = clean(r.get("Date"))
            if not d:
                continue
            schedule_rows.append({
                "date": d,
                "time": clean(r.get("Time")),
                "competition": clean(r.get("Comp")),
                "round": clean(r.get("Round")),
                "venue_side": clean(r.get("Venue")),
                "venue_name": SPORTING_STADIUM if clean(r.get("Venue")) == "Home" else "",
                "result": clean(r.get("Result")),
                "gf": int(fnum(r.get("GF")) or 0) if clean(r.get("Result")) else None,
                "ga": int(fnum(r.get("GA")) or 0) if clean(r.get("Result")) else None,
                "opponent": clean(r.get("Opponent")).replace("tr ", "").replace("fr ", "").replace("eng ", "").replace("it ", "").replace("ua ", "").replace("es ", ""),
                "possession": fnum(r.get("Poss")),
                "attendance": int(fnum(r.get("Attendance")) or 0) if fnum(r.get("Attendance")) is not None else None,
                "referee": clean(r.get("Referee")),
                "match_report": clean(r.get("Match Report")),
            })

    league_enrichment = fetch_league_schedule_enrichment()
    schedule_rows = apply_schedule_enrichment(schedule_rows, league_enrichment)

    write_json("squad.json", {
        "season": "2026/27",
        "competition_scope": "All competitions",
        "players": players,
        "source": "FBref",
    })
    write_json("team-stats.json", {
        "season": "2026/27",
        "team": team,
        "source": "FBref",
    })
    write_json("fbref-schedule.json", {
        "season": "2026/27",
        "fixtures": schedule_rows,
        "source": "FBref",
    })


def fetch_league_schedule_enrichment():
    """Fetch Primeira Liga match rows so fixture cards can show real venue/referee data."""
    try:
        tables, _ = read_fbref_tables(FBREF_LEAGUE_SCHEDULE)
        for df in tables:
            multi_index_flatten(df)
        table = pick_table(tables, ["Date", "Home", "Away", "Venue", "Referee"])
        if table is None:
            print("League schedule table not found; continuing without venue enrichment.")
            return {}
        out = {}
        for _, r in table.iterrows():
            d = clean(r.get("Date"))
            home = clean(r.get("Home"))
            away = clean(r.get("Away"))
            if not d or not home or not away:
                continue
            key = f"{d}|{home}|{away}"
            out[key] = {
                "venue_name": clean(r.get("Venue")),
                "referee": clean(r.get("Referee")),
                "attendance": int(fnum(r.get("Attendance")) or 0) if fnum(r.get("Attendance")) is not None else None,
            }
        return out
    except Exception as e:
        print("League schedule enrichment warning:", e)
        return {}


def apply_schedule_enrichment(schedule_rows, enrichment):
    for row in schedule_rows:
        if row.get("venue_side") == "Home":
            row["venue_name"] = SPORTING_STADIUM
        d = row.get("date") or ""
        # FBref's team schedule gives opponent but not venue name. For Primeira Liga,
        # the competition schedule supplies the exact stadium and referee.
        opponent = clean(row.get("opponent"))
        home = "Sporting CP" if row.get("venue_side") == "Home" else opponent
        away = opponent if row.get("venue_side") == "Home" else "Sporting CP"
        e = enrichment.get(f"{d}|{home}|{away}")
        if e:
            row["venue_name"] = e.get("venue_name") or row.get("venue_name")
            row["referee"] = e.get("referee") or row.get("referee")
            row["attendance"] = e.get("attendance")
    return schedule_rows


def fetch_standings():
    tables, html = read_fbref_tables(FBREF_LEAGUE)
    for df in tables:
        multi_index_flatten(df)
    table = pick_table(tables, ["Rk", "Squad", "MP", "W", "D", "L", "GF", "GA", "Pts"])
    if table is None:
        raise RuntimeError("FBref Primeira Liga standings table not found.")

    rows = []
    for _, r in table.iterrows():
        squad = clean(r.get("Squad"))
        if not squad or squad in {"Squad", "Opponent"}:
            continue
        rows.append({
            "rank": int(fnum(r.get("Rk")) or len(rows) + 1),
            "team": squad,
            "played": int(fnum(r.get("MP")) or 0),
            "wins": int(fnum(r.get("W")) or 0),
            "draws": int(fnum(r.get("D")) or 0),
            "losses": int(fnum(r.get("L")) or 0),
            "gf": int(fnum(r.get("GF")) or 0),
            "ga": int(fnum(r.get("GA")) or 0),
            "gd": int(fnum(r.get("GD")) or 0),
            "points": int(fnum(r.get("Pts")) or 0),
            "last5": clean(r.get("Last 5")),
        })
    write_json("standings.json", {"competition": "Primeira Liga", "season": "2026/27", "table": rows, "source": "FBref"})


def fetch_fsa_fixtures():
    if not FSAPI_KEY:
        raise RuntimeError("Missing FSAPI_KEY.")
    teams = fsapi("/teams", {"country": "Portugal", "limit": 100}).get("data", [])
    sporting = next((x for x in teams if clean(x.get("team_name")).lower() in {"sporting cp", "sporting lisboa", "sporting"}), None)
    if not sporting:
        # relaxed fallback
        sporting = next((x for x in teams if "sporting" in clean(x.get("team_name")).lower()), None)
    if not sporting:
        raise RuntimeError("Sporting CP was not found in Football Soccer API.")
    team_id = sporting["team_id"]

    today = date.today()
    end = today + timedelta(days=30)
    payload = fsapi("/matches", {
        "team_id": team_id,
        "date_from": today.isoformat(),
        "date_to": end.isoformat(),
        "limit": 100,
        "sort": "kickoff_utc",
    })
    rows = payload.get("data", [])

    fixtures = {}
    for x in rows:
        mid = str(x.get("match_id"))
        if not mid:
            continue
        fixtures[mid] = x

    normalized = []
    for x in fixtures.values():
        status = x.get("match_status")
        normalized.append({
            "id": x.get("match_id"),
            "date": x.get("kickoff_utc"),
            "kickoff_date": x.get("kickoff_date"),
            "kickoff_local_time": x.get("kickoff_local_time"),
            "status": {"short": status, "long": status.replace("_", " ").title() if status else ""},
            "referee": x.get("referee_name"),
            "venue": {
                "name": x.get("venue_name"),
                "city": x.get("city_name"),
                "lat": x.get("latitude"),
                "lon": x.get("longitude"),
                "capacity": x.get("venue_capacity"),
            },
            "competition": {"name": x.get("league_name"), "season": x.get("season_start_year")},
            "home": {"id": x.get("home_team_id"), "name": x.get("home_team_name")},
            "away": {"id": x.get("away_team_id"), "name": x.get("away_team_name")},
            "goals": {"home": x.get("home_goals"), "away": x.get("away_goals")},
            "half_time": {"home": x.get("half_time_home_goals"), "away": x.get("half_time_away_goals")},
            "travel_km": x.get("away_travel_km"),
            "source": "Football Soccer API",
        })
    normalized.sort(key=lambda x: x.get("date") or 0)
    write_json("fixtures.json", {"team_id": team_id, "fixtures": normalized, "source": "Football Soccer API"})


def enrich_match_details():
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    upcoming = [f for f in fixtures if f.get("status", {}).get("short") == "scheduled"]
    recent = [f for f in fixtures if f.get("status", {}).get("short") == "finished"]
    chosen = recent[-1:] + upcoming[:3]
    details = []
    for f in chosen:
        try:
            p = fsapi(f"/matches/{f['id']}").get("data")
            details.append(p)
        except Exception as e:
            print("Match detail warning:", f.get("id"), e)
    write_json("match-details.json", {"fixtures": details, "source": "Football Soccer API"})


def fetch_news():
    items = []
    try:
        r = session.get(SPORTING_NEWS, timeout=25)
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
            if len(items) >= 12:
                break
    except Exception as e:
        print("Sporting news warning:", e)

    try:
        import feedparser
        feed = feedparser.parse("https://news.google.com/rss/search?q=Sporting%20CP&hl=pt-PT&gl=PT&ceid=PT:pt-150")
        existing = {x["url"] for x in items}
        for entry in feed.entries[:24]:
            url = entry.get("link")
            title = entry.get("title")
            if not url or not title or url in existing:
                continue
            source = (entry.get("source") or {}).get("title") or "Google News"
            items.append({
                "title": title[:180],
                "url": url,
                "source": source,
                "published": entry.get("published"),
            })
            if len(items) >= 30:
                break
    except Exception as e:
        print("Google News warning:", e)

    write_json("news.json", {"items": items})


def geocode_missing_venues():
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    cache = safe_existing("venues.json") or {"venues": {}}
    venues = cache.get("venues", {})
    changed = False
    for f in fixtures:
        v = f.get("venue") or {}
        name, city = v.get("name"), v.get("city")
        if not name or v.get("lat") is not None:
            continue
        key = f"{name}|{city or ''}"
        if key in venues:
            continue
        try:
            time.sleep(1.1)
            q = quote_plus(f"{name}, {city or ''}, Portugal")
            r = session.get("https://nominatim.openstreetmap.org/search",
                            params={"q": q, "format": "jsonv2", "limit": 1},
                            timeout=20)
            r.raise_for_status()
            rows = r.json()
            if rows:
                venues[key] = {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"]),
                               "display": rows[0].get("display_name")}
                changed = True
        except Exception as e:
            print("Nominatim warning:", e)
    if changed or not (DATA / "venues.json").exists():
        write_json("venues.json", {"venues": venues, "source": "OpenStreetMap Nominatim"})


def main():
    mode = os.environ.get("LIONS_DEN_MODE", "full")
    errors = []

    # News is independent and should not stop football data.
    try:
        fetch_news()
    except Exception as e:
        errors.append(f"news: {e}")

    if mode in {"full", "football"}:
        try:
            fetch_fsa_fixtures()
            geocode_missing_venues()
            enrich_match_details()
        except Exception as e:
            errors.append(f"football-api: {e}")

        for fn in (fetch_fbref_stats, fetch_standings):
            try:
                fn()
            except Exception as e:
                errors.append(f"{fn.__name__}: {e}")

    # Always leave a status file so the app can explain which source failed.
    write_json("status.json", {
        "mode": mode,
        "errors": errors,
        "sources": {
            "football_api": bool(FSAPI_KEY),
            "fbref": True,
            "news": True,
            "maps": True,
        }
    })
    if errors:
        print("Completed with warnings:", *errors, sep="\n- ")
    else:
        print("Lion's Den data update completed successfully.")


if __name__ == "__main__":
    main()
