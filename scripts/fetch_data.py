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
FBREF_LEAGUE = "https://fbref.com/en/comps/32/2026-2027/2026-2027-Primeira-Liga-Stats"
FBREF_SCHEDULE = "https://fbref.com/en/squads/13dc44fd/2026-2027/matchlogs/all_comps/schedule/Sporting-CP-Scores-and-Fixtures-All-Competitions"
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
    normalized_tables = []
    for df in tables:
        normalized_tables.append(multi_index_flatten(df.copy()))
    tables = normalized_tables

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
        "matches": sum(1 for _, r in fixtures.iterrows() if clean(r.get("Result"))),
        "goals": sum(int(fnum(r.get("GF")) or 0) for _, r in fixtures.iterrows()),
        "goals_against": sum(int(fnum(r.get("GA")) or 0) for _, r in fixtures.iterrows()),
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
                    "venue": clean(r.get("Venue")),
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
                "result": clean(r.get("Result")),
                "gf": int(fnum(r.get("GF")) or 0) if clean(r.get("Result")) else None,
                "ga": int(fnum(r.get("GA")) or 0) if clean(r.get("Result")) else None,
                "opponent": clean(r.get("Opponent")).replace("tr ", "").replace("fr ", "").replace("eng ", "").replace("it ", "").replace("ua ", "").replace("es ", ""),
                "possession": fnum(r.get("Poss")),
                "attendance": int(fnum(r.get("Attendance")) or 0) if fnum(r.get("Attendance")) is not None else None,
                "referee": clean(r.get("Referee")),
                "match_report": clean(r.get("Match Report")),
            })

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


def sofa_get(path, params=None):
    url = "https://www.sofascore.com/api/v1" + path
    r = session.get(url, params=params or {}, timeout=25,
                    headers={"User-Agent": USER_AGENT, "Referer": "https://www.sofascore.com/"})
    r.raise_for_status()
    return r.json()




def fotmob_get(path, params=None):
    url = "https://www.fotmob.com" + path
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.fotmob.com/"
    }
    r = session.get(url, params=params or {}, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

def _first_dict(obj, *keys):
    if not isinstance(obj, dict):
        return {}
    for k in keys:
        v = obj.get(k)
        if isinstance(v, dict):
            return v
    return {}

def _first_list(obj, *keys):
    if not isinstance(obj, dict):
        return []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):
            return v
    return []

def _obj(v):
    return v if isinstance(v, dict) else {}

def _fixture_competition(x, stamp):
    raw = x.get("competition") or x.get("league") or x.get("tournament")
    if isinstance(raw, dict):
        name = clean(raw.get("name") or raw.get("leagueName") or raw.get("tournamentName"))
        if name: return name
    elif isinstance(raw, str) and raw.strip():
        return raw.strip()
    d = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    if d in {"2026-07-14","2026-07-20","2026-07-25","2026-07-31"}: return "Amigável"
    if d in {"2026-09-09","2026-10-13","2026-10-21","2026-11-03","2026-11-25"}: return "Liga dos Campeões"
    if d == "2026-10-27": return "Taça da Liga"
    return "Liga Portugal"

def fetch_fotmob_core():
    """Primary zero-cost source: FotMob public web data."""
    team_id = 9768
    payload = fotmob_get("/api/data/teams", {"id": team_id, "ccode3": "PRT"})

    # ---- Fixtures ----
    fxroot = _first_dict(payload, "fixtures")
    allfx = _first_dict(fxroot, "allFixtures")
    raw_fixtures = _first_list(allfx, "fixtures")
    if not raw_fixtures:
        raw_fixtures = _first_list(fxroot, "fixtures")

    fixtures = []
    for x in raw_fixtures:
        home = x.get("home") or x.get("homeTeam") or {}
        away = x.get("away") or x.get("awayTeam") or {}
        st = x.get("status") or {}
        utc = st.get("utcTime") or x.get("utcTime") or x.get("matchTimeUTCDate")
        if not utc:
            continue
        try:
            stamp = int(datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        finished = bool(st.get("finished")) or str(st.get("reason", "")).lower() in {"ft", "full time"}
        score = x.get("score") or {}
        hs = score.get("home") if isinstance(score, dict) else None
        aas = score.get("away") if isinstance(score, dict) else None
        if hs is None:
            hs = home.get("score")
        if aas is None:
            aas = away.get("score")
        comp = x.get("competition") or x.get("league") or {}
        venue = _obj(x.get("venue"))
        fixtures.append({
            "id": x.get("id") or x.get("matchId"),
            "date": stamp,
            "kickoff_date": datetime.fromtimestamp(stamp, timezone.utc).date().isoformat(),
            "kickoff_local_time": datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M"),
            "status": {
                "short": "finished" if finished else ("live" if st.get("started") and not finished else "scheduled"),
                "long": "Terminado" if finished else ("Em direto" if st.get("started") else "Agendado")
            },
            "referee": None,
            "venue": {
                "name": clean(venue.get("name")),
                "city": clean(venue.get("city")),
                "lat": venue.get("latitude"),
                "lon": venue.get("longitude"),
                "capacity": venue.get("capacity")
            },
            "competition": {
                "name": _fixture_competition(x, stamp),
                "round": clean(x.get("round") or x.get("matchday")),
                "season": "2026/27"
            },
            "home": {"id": home.get("id"), "name": clean(home.get("name"))},
            "away": {"id": away.get("id"), "name": clean(away.get("name"))},
            "goals": {"home": hs if finished else None, "away": aas if finished else None},
            "half_time": {"home": None, "away": None},
            "source": "FotMob"
        })

    fixtures = {str(x["id"]): x for x in fixtures if x.get("id")}.values()
    fixtures = sorted(fixtures, key=lambda x: x.get("date") or 0)
    if not fixtures:
        raise RuntimeError("FotMob returned no Sporting fixtures.")

    # Match details for next 8 and last 4 provide referee/venue/map data.
    for f in list(fixtures)[-4:] + [x for x in fixtures if x["date"] > int(time.time())][:8]:
        try:
            md = fotmob_get("/api/data/matchDetails", {"matchId": f["id"]})
            content = _first_dict(md, "content")
            facts = _first_dict(content, "matchFacts")
            info = _first_dict(facts, "infoBox")
            referee = info.get("Referee") or info.get("referee")
            if referee:
                f["referee"] = clean(referee)
            venue = f.get("venue") or {}
            for key, value in {
                "name": venue.get("name") or info.get("Stadium"),
                "city": venue.get("city") or info.get("Location")
            }.items():
                if value:
                    venue[key] = clean(value)
            f["venue"] = venue
        except Exception as e:
            print("FotMob match detail warning:", f.get("id"), e)

    write_json("fixtures.json", {
        "team_id": team_id,
        "fixtures": list(fixtures),
        "source": "FotMob",
        "season": "2026/27"
    })

    # ---- Squad + player season stats ----
    squad_root = _first_dict(payload, "squad")
    groups = []
    def collect_squad_nodes(obj):
        if isinstance(obj, dict):
            pid = obj.get("id") or obj.get("playerId")
            name = obj.get("name")
            if pid and name and not isinstance(name, dict) and not obj.get("isCoach") and name != "Rui Borges": groups.append(obj)
            for value in obj.values(): collect_squad_nodes(value)
        elif isinstance(obj, list):
            for value in obj: collect_squad_nodes(value)
    collect_squad_nodes(squad_root)
    dedup = {}
    for m in groups: dedup[str(m.get("id") or m.get("playerId"))] = m
    groups = list(dedup.values())

    players = []
    for m in groups:
        pid = m.get("id") or m.get("playerId")
        if not pid:
            continue
        player = {
            "fotmob_id": pid,
            "name": clean(m.get("name")),
            "position": clean(m.get("rolePosition") or m.get("position")),
            "nationality": clean(m.get("cname") or m.get("country")),
            "photo": f"https://images.fotmob.com/image_resources/playerimages/{pid}.png",
            "stats": {}
        }
        try:
            pd = fotmob_get("/api/data/playerData", {"id": pid, "includeMarketValues": "true"})
            seasons = pd.get("statSeasons") or []
            season = next((z for z in seasons if str(z.get("seasonName", "")).replace("-", "/") in {"2026/2027", "2026/27"}), None)
            if not season and seasons:
                season = seasons[0]
            stats = {}
            recent = pd.get("recentMatches") or []
            current_matches = []
            for rm in recent:
                md = ((rm.get("matchDate") or {}).get("utcTime") if isinstance(rm.get("matchDate"), dict) else None)
                try:
                    rts = datetime.fromisoformat(str(md).replace("Z", "+00:00")).timestamp() if md else 0
                except Exception:
                    rts = 0
                if rts >= datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp():
                    current_matches.append(rm)
            played = [rm for rm in current_matches if (fnum(rm.get("minutesPlayed")) or 0) > 0]
            stats["matches"] = len(played)
            stats["starts"] = sum(1 for rm in played if not rm.get("onBench"))
            stats["minutes"] = int(sum(fnum(rm.get("minutesPlayed")) or 0 for rm in played))
            stats["goals"] = int(sum(fnum(rm.get("goals")) or 0 for rm in played))
            stats["assists"] = int(sum(fnum(rm.get("assists")) or 0 for rm in played))
            stats["yellow"] = int(sum(fnum(rm.get("yellowCards")) or 0 for rm in played))
            stats["red"] = int(sum(fnum(rm.get("redCards")) or 0 for rm in played))
            ratings = []
            for rm in played:
                rp = rm.get("ratingProps") or {}
                fb = (rp.get("num") or {}).get("fallback") if isinstance(rp.get("num"), dict) else None
                value = (fb or {}).get("number") if isinstance(fb, dict) else None
                if value is not None:
                    ratings.append(float(value))
            if ratings:
                stats["rating"] = round(sum(ratings) / len(ratings), 2)
            player["stats"] = stats
            dob = _first_dict(pd, "birthDate")
            player["dateOfBirth"] = dob.get("iso") or dob.get("date") if dob else None
        except Exception as e:
            print("FotMob player warning:", pid, e)
        players.append(player)

    if players:
        old_squad = safe_existing("squad.json") or {}
        old_players = old_squad.get("squad") or old_squad.get("players") or []
        old_by_name = {normalize_player_name(p.get("name")).lower(): p for p in old_players if p.get("name")}
        for p in players:
            old = old_by_name.get(normalize_player_name(p.get("name")).lower(), {})
            p["position"] = p.get("position") or old.get("position")
            p["nationality"] = p.get("nationality") or old.get("nationality")
            p["dateOfBirth"] = p.get("dateOfBirth") or old.get("dateOfBirth")
        write_json("squad.json", {
            "team": "Sporting Clube de Portugal",
            "crest": "https://images.fotmob.com/image_resources/logo/teamlogo/9768.png",
            "coach": "Rui Borges",
            "season": "2026/27",
            "squad": players,
            "source": "FotMob"
        })

    # ---- Primeira Liga standings ----
    league = fotmob_get("/api/data/leagues", {"id": 61, "season": "2026/2027", "ccode3": "PRT"})
    rows = []
    for block in _first_list(league, "table"):
        data = _first_dict(block, "data")
        table = _first_dict(data, "table")
        for r in _first_list(table, "all"):
            scores = str(r.get("scoresStr") or "0-0").split("-")
            rows.append({
                "position": r.get("idx"),
                "team": {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "shortName": r.get("shortName") or r.get("name"),
                    "tla": None,
                    "crest": f"https://images.fotmob.com/image_resources/logo/teamlogo/{r.get('id')}.png" if r.get("id") else None
                },
                "playedGames": r.get("played", 0),
                "won": r.get("wins", 0),
                "draw": r.get("draws", 0),
                "lost": r.get("losses", 0),
                "points": r.get("pts", 0),
                "goalsFor": int(scores[0]) if scores and scores[0].isdigit() else 0,
                "goalsAgainst": int(scores[1]) if len(scores) > 1 and scores[1].isdigit() else 0,
                "goalDifference": r.get("goalConDiff", 0)
            })
    if rows:
        rows.sort(key=lambda r: r.get("position") or 999)
        write_json("standings.json", {
            "competition": "Primeira Liga",
            "season": "2026/27",
            "table": rows,
            "source": "FotMob"
        })

    # ---- Team stats derived from current fixtures + player totals ----
    now = int(time.time())
    played = [f for f in fixtures if f["date"] <= now and f["goals"]["home"] is not None and f.get("competition",{}).get("name") != "Amigável"]
    team = {"matches":0,"wins":0,"draws":0,"losses":0,"goals":0,"goals_against":0,
            "assists":0,"shots":0,"shots_on_target":0,"clean_sheets":0,"form":[],"recent_matches":[]}
    for f in played:
        h, a = int(f["goals"]["home"]), int(f["goals"]["away"])
        home = f["home"].get("id") == team_id
        gf, ga = (h,a) if home else (a,h)
        res = "W" if gf > ga else "D" if gf == ga else "L"
        team["matches"] += 1; team["goals"] += gf; team["goals_against"] += ga
        team[{"W":"wins","D":"draws","L":"losses"}[res]] += 1
        team["clean_sheets"] += int(ga == 0); team["form"].append(res)
        team["recent_matches"].append({
            "date": f["kickoff_date"], "competition": f["competition"]["name"],
            "venue": "Casa" if home else "Fora", "result": res, "gf": gf, "ga": ga,
            "opponent": (f["away"] if home else f["home"])["name"]
        })
    for p in players:
        st = p.get("stats") or {}
        for k in ("assists","shots","shots_on_target"):
            team[k] += int(fnum(st.get(k)) or 0)
    team["form"] = team["form"][-6:]
    team["recent_matches"] = team["recent_matches"][-6:]
    write_json("team-stats.json", {"season":"2026/27","team":team,"source":"FotMob"})

def fetch_sofascore_fixtures():
    """Use SofaScore's public team feed as the primary current-season fixture source."""
    team_id = 3001
    events = []
    for direction in ("last", "next"):
        for page in range(0, 3):
            try:
                payload = sofa_get(f"/team/{team_id}/events/{direction}/{page}")
                batch = payload.get("events", [])
                if not batch:
                    break
                events.extend(batch)
                if not payload.get("hasNextPage"):
                    break
            except Exception as e:
                print("Sofascore fixtures warning:", direction, page, e)
                break

    unique = {str(e.get("id")): e for e in events if e.get("id")}
    normalized = []

    for e in unique.values():
        home = e.get("homeTeam") or {}
        away = e.get("awayTeam") or {}
        if home.get("id") != team_id and away.get("id") != team_id:
            continue

        ts = e.get("startTimestamp")
        if not ts:
            continue

        status = e.get("status") or {}
        stype = clean(status.get("type")).lower()
        finished = stype in {"finished", "afterpenalties", "afterextra"}
        home_score = e.get("homeScore") or {}
        away_score = e.get("awayScore") or {}

        venue = e.get("venue") or {}
        normalized.append({
            "id": e.get("id"),
            "date": int(ts),
            "kickoff_date": datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat(),
            "kickoff_local_time": datetime.fromtimestamp(int(ts), timezone.utc).strftime("%H:%M"),
            "status": {
                "short": "finished" if finished else ("live" if stype == "inprogress" else "scheduled"),
                "long": clean(status.get("description")) or ("Terminado" if finished else "Agendado")
            },
            "referee": None,
            "venue": {
                "name": clean(venue.get("name")),
                "city": clean(venue.get("city")),
                "lat": venue.get("latitude"),
                "lon": venue.get("longitude"),
                "capacity": venue.get("capacity"),
            },
            "competition": {
                "name": clean((e.get("tournament") or {}).get("name")),
                "round": clean((e.get("roundInfo") or {}).get("name")),
                "season": "2026/27"
            },
            "home": {"id": home.get("id"), "name": clean(home.get("name"))},
            "away": {"id": away.get("id"), "name": clean(away.get("name"))},
            "goals": {
                "home": home_score.get("current") if finished else None,
                "away": away_score.get("current") if finished else None
            },
            "half_time": {
                "home": home_score.get("period1") if finished else None,
                "away": away_score.get("period1") if finished else None
            },
            "source": "Sofascore"
        })

    normalized.sort(key=lambda x: x.get("date") or 0)

    # Enrich only the small set the UI actually needs.
    for f in normalized[:12]:
        try:
            detail = sofa_get(f"/event/{f['id']}").get("event") or {}
            ref = detail.get("referee") or {}
            f["referee"] = clean(ref.get("name")) or None
            v = detail.get("venue") or {}
            for key, value in {
                "name": clean(v.get("name")),
                "city": clean(v.get("city")),
                "lat": v.get("latitude"),
                "lon": v.get("longitude"),
                "capacity": v.get("capacity"),
            }.items():
                if value not in (None, ""):
                    f["venue"][key] = value
        except Exception as e:
            print("Sofascore match detail warning:", f.get("id"), e)

    if not normalized:
        raise RuntimeError("Sofascore returned no Sporting fixtures.")

    write_json("fixtures.json", {
        "team_id": team_id,
        "fixtures": normalized,
        "source": "Sofascore",
        "season": "2026/27"
    })
    print(f"Sofascore: {len(normalized)} Sporting fixtures written.")

def fetch_sofascore_match_details():
    """Store recent Sporting match details from SofaScore: lineups, incidents and team stats."""
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    finished = [f for f in fixtures if f.get("status", {}).get("short") == "finished" and f.get("id")]
    selected = sorted(finished, key=lambda x: x.get("date") or 0, reverse=True)[:8]
    details = []
    for f in selected:
        eid = f.get("id")
        try:
            event = sofa_get(f"/event/{eid}").get("event") or {}
            lineups = sofa_get(f"/event/{eid}/lineups")
            incidents = sofa_get(f"/event/{eid}/incidents")
            statistics = sofa_get(f"/event/{eid}/statistics")
            details.append({"match_id": eid, "event": event, "lineups": lineups, "incidents": incidents, "statistics": statistics})
        except Exception as e:
            print("Sofascore match detail warning:", eid, e)
    write_json("match-details.json", {"fixtures": details, "source": "Sofascore"})
    print(f"Sofascore: {len(details)} detailed matches written.")

def enrich_player_profiles():
    """Add stable biographical fields from SofaScore to the FBref squad."""
    squad = safe_existing("squad.json") or {}
    players = squad.get("players") or []
    for p in players:
        sid = p.get("sofascore_id") or p.get("id")
        if not sid:
            continue
        try:
            sp = sofa_get(f"/player/{sid}").get("player") or {}
            for src, dst in [("dateOfBirth","dateOfBirth"),("shirtNumber","shirtNumber"),("nationality","nationality"),("country","nationality")]:
                if sp.get(src) and not p.get(dst):
                    p[dst] = sp.get(src)
            if sp.get("nationality") and not p.get("nation"):
                p["nation"] = (sp.get("nationality") or {}).get("name") if isinstance(sp.get("nationality"),dict) else sp.get("nationality")
        except Exception as e:
            print("Player profile warning:", p.get("name"), e)
    write_json("squad.json", squad)

def fetch_competition_standings():
    """Fetch standings/bracket-table data for Sporting's four requested competitions."""
    competitions = {
        "primeira-liga": (238, "Primeira Liga"),
        "taca-portugal": (336, "Taça de Portugal"),
        "taca-liga": (327, "Taça da Liga"),
        "champions": (7, "Champions League"),
    }
    result = {}
    for key, (tid, name) in competitions.items():
        try:
            sid = _sofa_season(tid)
            if not sid:
                print("No SofaScore season:", name)
                continue
            payload = sofa_get(f"/unique-tournament/{tid}/season/{sid}/standings/total")
            blocks = payload.get("standings") or []
            rows = []
            for block in blocks:
                for r in block.get("rows", []):
                    team = r.get("team") or {}
                    rows.append({
                        "position": r.get("position"),
                        "team": {"id": team.get("id"), "name": team.get("name"), "shortName": team.get("shortName") or team.get("name"), "tla": team.get("nameCode"), "crest": f"https://img.sofascore.com/api/v1/team/{team.get('id')}/image" if team.get("id") else None},
                        "playedGames": r.get("matches", 0), "won": r.get("wins", 0), "draw": r.get("draws", 0), "lost": r.get("losses", 0),
                        "points": r.get("points", 0), "goalsFor": r.get("scoresFor", 0), "goalsAgainst": r.get("scoresAgainst", 0), "goalDifference": r.get("scoreDiff", 0),
                        "groupName": block.get("name") or block.get("groupName")
                    })
            result[key] = {"competition": name, "season": "2026/27", "table": rows, "source": "SofaScore", "tournament_id": tid, "season_id": sid}
        except Exception as e:
            print("Competition standings warning:", name, e)
    write_json("standings-competitions.json", result)
    print("SofaScore: competition standings written:", ", ".join(result.keys()))

def fetch_sofascore_standings():
    """Fetch the current Primeira Liga table from SofaScore."""
    sid = _sofa_season(238)
    if not sid:
        raise RuntimeError("Sofascore Primeira Liga season not found.")

    payload = sofa_get(f"/unique-tournament/238/season/{sid}/standings/total")
    blocks = payload.get("standings") or []
    rows = []
    for block in blocks:
        for r in block.get("rows", []):
            team = r.get("team") or {}
            rows.append({
                "position": r.get("position"),
                "team": {
                    "id": team.get("id"),
                    "name": team.get("name"),
                    "shortName": team.get("shortName") or team.get("name"),
                    "tla": team.get("nameCode"),
                    "crest": f"https://img.sofascore.com/api/v1/team/{team.get('id')}/image" if team.get("id") else None
                },
                "playedGames": r.get("matches", 0),
                "won": r.get("wins", 0),
                "draw": r.get("draws", 0),
                "lost": r.get("losses", 0),
                "points": r.get("points", 0),
                "goalsFor": r.get("scoresFor", 0),
                "goalsAgainst": r.get("scoresAgainst", 0),
                "goalDifference": r.get("scoreDiff", 0)
            })

    if not rows:
        raise RuntimeError("Sofascore returned an empty Primeira Liga table.")

    rows.sort(key=lambda x: x.get("position") or 999)
    write_json("standings.json", {
        "competition": "Primeira Liga",
        "season": "2026/27",
        "table": rows,
        "source": "Sofascore"
    })
    print(f"Sofascore: {len(rows)} standings rows written.")

def build_team_stats_from_sofa():
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    finished = [f for f in fixtures if f.get("status", {}).get("short") == "finished"]
    team = {"matches": 0, "wins": 0, "draws": 0, "losses": 0, "goals": 0,
            "goals_against": 0, "clean_sheets": 0, "assists": 0, "shots": 0,
            "shots_on_target": 0, "possession_avg": None, "form": [], "recent_matches": []}

    for f in finished:
        h = f.get("goals", {}).get("home")
        a = f.get("goals", {}).get("away")
        if h is None or a is None:
            continue
        home = (f.get("home") or {}).get("id") == 3001
        gf, ga = (h, a) if home else (a, h)
        team["matches"] += 1
        team["goals"] += int(gf)
        team["goals_against"] += int(ga)
        team["clean_sheets"] += int(ga == 0)
        result = "W" if gf > ga else "D" if gf == ga else "L"
        team[result_map := {"W":"wins","D":"draws","L":"losses"}[result]] += 1
        team["form"].append(result)
        team["recent_matches"].append({
            "date": f.get("kickoff_date"),
            "competition": (f.get("competition") or {}).get("name"),
            "venue": "Casa" if home else "Fora",
            "result": result,
            "gf": gf, "ga": ga,
            "opponent": (f.get("away") if home else f.get("home") or {}).get("name")
        })

    team["form"] = team["form"][-6:]
    team["recent_matches"] = team["recent_matches"][-6:]

    squad = (safe_existing("squad.json") or {}).get("squad", [])
    for p in squad:
        st = p.get("stats") or {}
        team["assists"] += int(fnum(st.get("assists")) or 0)
        team["shots"] += int(fnum(st.get("shots")) or 0)
        team["shots_on_target"] += int(fnum(st.get("shots_on_target")) or 0)

    write_json("team-stats.json", {
        "season": "2026/27",
        "team": team,
        "source": "Sofascore"
    })

def _sofa_season(tournament_id):
    """Return the current 2026/27 SofaScore season id, tolerating tournament-specific names."""
    try:
        payload = sofa_get(f"/unique-tournament/{tournament_id}/seasons")
        seasons = payload.get("seasons", []) or []
        # SofaScore commonly names current seasons as e.g. "Liga Portugal Betclic 26/27".
        for season in seasons:
            name = clean(season.get("name"))
            year = clean(season.get("year"))
            if year in {"26/27", "2026/27", "2026/2027"} or re.search(r"26/27|2026/27|2026-2027", name):
                return season.get("id")
        for season in seasons:
            if season.get("isCurrent") is True:
                return season.get("id")
        # The endpoint returns seasons newest-first; if no explicit current marker exists,
        # the first season is the safest current-season fallback.
        if seasons:
            return seasons[0].get("id")
    except Exception as e:
        print("Sofascore season warning:", tournament_id, e)
    return None

def fetch_sofascore_player_stats():
    """Fetch Sporting player profiles and 2026/27 season stats from public Sofascore endpoints."""
    team_id = 3001
    roster = sofa_get(f"/team/{team_id}/players").get("players", [])
    if not roster:
        raise RuntimeError("Sofascore returned an empty Sporting roster.")

    tournaments = [
        (238, "Liga Portugal"),
        (7, "UEFA Champions League"),
        (21, "Taça da Liga"),
        (329, "Taça de Portugal"),
    ]
    season_map = {tid: _sofa_season(tid) for tid, _ in tournaments}
    players = []

    for item in roster:
        p = item.get("player") or {}
        pid = p.get("id")
        if not pid:
            continue
        row = {
            "sofascore_id": pid,
            "name": clean(p.get("name")),
            "position": clean(p.get("position")),
            "nationality": clean((p.get("country") or {}).get("name")),
            "dateOfBirth": p.get("dateOfBirth"),
            "height": p.get("height"),
            "preferredFoot": clean(p.get("preferredFoot")),
            "shirtNumber": p.get("shirtNumber") or p.get("jerseyNumber"),
            "photo": f"https://img.sofascore.com/api/v1/player/{pid}/image",
            "competitions": {},
        }
        for tid, tname in tournaments:
            sid = season_map.get(tid)
            if not sid:
                continue
            try:
                payload = sofa_get(f"/player/{pid}/unique-tournament/{tid}/season/{sid}/statistics/overall")
                stats = payload.get("statistics", payload)
                if not isinstance(stats, dict) or not stats:
                    continue
                row["competitions"][tname] = {
                    "tournament_id": tid,
                    "season_id": sid,
                    "appearances": stats.get("appearances"),
                    "starts": stats.get("startingAppearances"),
                    "minutes": stats.get("minutesPlayed"),
                    "goals": stats.get("goals"),
                    "assists": stats.get("assists"),
                    "rating": stats.get("rating"),
                    "yellow": stats.get("yellowCards"),
                    "red": stats.get("redCards"),
                    "shots": stats.get("shots"),
                    "shots_on_target": stats.get("onTargetScoringAttempts"),
                    "key_passes": stats.get("keyPasses"),
                    "big_chances_created": stats.get("bigChancesCreated"),
                    "big_chances_missed": stats.get("bigChancesMissed"),
                    "accurate_passes": stats.get("accuratePasses"),
                    "total_passes": stats.get("totalPasses"),
                    "tackles": stats.get("totalTackles"),
                    "interceptions": stats.get("interceptions"),
                    "clearances": stats.get("clearances"),
                    "saves": stats.get("saves"),
                    "clean_sheets": stats.get("cleanSheet"),
                }
            except Exception as e:
                print("Sofascore stats warning:", row["name"], tname, e)

        comps = list(row["competitions"].values())
        def isum(key):
            vals = [fnum(x.get(key)) for x in comps if fnum(x.get(key)) is not None]
            return int(sum(vals)) if vals else 0
        row["stats"] = {
            "matches": isum("appearances"),
            "starts": isum("starts"),
            "minutes": isum("minutes"),
            "goals": isum("goals"),
            "assists": isum("assists"),
            "yellow": isum("yellow"),
            "red": isum("red"),
            "shots": isum("shots"),
            "shots_on_target": isum("shots_on_target"),
            "key_passes": isum("key_passes"),
            "big_chances_created": isum("big_chances_created"),
            "saves": isum("saves"),
            "clean_sheets": isum("clean_sheets"),
        }
        ratings = [(fnum(x.get("rating")), fnum(x.get("minutes")) or 0) for x in comps]
        ratings = [(r, m) for r, m in ratings if r is not None]
        row["stats"]["rating"] = round(sum(r * max(m, 1) for r, m in ratings) / sum(max(m, 1) for _, m in ratings), 2) if ratings else None
        players.append(row)

    if not players:
        raise RuntimeError("Sofascore returned no usable Sporting players.")

    write_json("squad-stats.json", {
        "season": "2026/27",
        "team": "Sporting CP",
        "team_id": team_id,
        "players": players,
        "source": "Sofascore public data",
    })

    write_json("squad.json", {
        "team": "Sporting Clube de Portugal",
        "crest": "https://img.sofascore.com/api/v1/team/3001/image",
        "coach": "Rui Borges",
        "season": "2026/27",
        "squad": players,
        "source": "Sofascore public data"
    })
    print(f"Sofascore: {len(players)} players enriched; {sum(bool(p.get('stats')) for p in players)} have season stats.")

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
    write_json("fixtures-api.json", {"team_id": team_id, "fixtures": normalized, "source": "Football Soccer API"})


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


def fetch_youtube():
    """Fetch recent/public videos from Sporting CP's official YouTube channel."""
    channel_id = "UCHpcLaddGlZUVdtX302fpHA"
    items = []

    def collect(obj):
        if isinstance(obj, dict):
            vr = obj.get("videoRenderer")
            if isinstance(vr, dict):
                vid = clean(vr.get("videoId"))
                runs = (vr.get("title") or {}).get("runs") or []
                title = clean("".join(x.get("text", "") for x in runs))
                thumbs = (vr.get("thumbnail") or {}).get("thumbnails") or []
                thumb = thumbs[-1].get("url") if thumbs else (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "")
                if vid and title and not any(x["id"] == vid for x in items):
                    items.append({"id": vid, "title": title, "url": f"https://www.youtube.com/watch?v={vid}",
                                  "published": clean((vr.get("publishedTimeText") or {}).get("simpleText")),
                                  "thumbnail": thumb})
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)

    # First choice: normal channel videos page, which is much more stable than
    # the undocumented browse API in GitHub Actions.
    for url in [
        f"https://www.youtube.com/channel/{channel_id}/videos?hl=pt-PT&gl=PT",
        f"https://www.youtube.com/channel/{channel_id}?hl=pt-PT&gl=PT",
    ]:
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            html = r.text
            m = re.search(r"ytInitialData\\?\s*=\\?\s*(\\{.*?\\});", html)
            if not m:
                m = re.search(r"var ytInitialData = (\\{.*?\\});", html)
            if m:
                collect(json.loads(m.group(1)))
            if items:
                break
        except Exception as e:
            print("YouTube page warning:", e)

    # Second choice: Innertube browse.
    if not items:
        try:
            payload = {"context":{"client":{"clientName":"WEB","clientVersion":"2.20260924.01.00","hl":"pt-PT","gl":"PT"}},"browseId":channel_id}
            r = session.post("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false", json=payload, timeout=30)
            r.raise_for_status()
            collect(r.json())
        except Exception as e:
            print("YouTube browse warning:", e)

    # Third choice: official Atom feed.
    if not items:
        try:
            import xml.etree.ElementTree as ET
            r = session.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=25)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns = {"atom":"http://www.w3.org/2005/Atom","yt":"http://www.youtube.com/xml/schemas/2015"}
            for entry in root.findall("atom:entry", ns):
                vid = clean(entry.findtext("yt:videoId","",ns))
                title = clean(entry.findtext("atom:title","",ns))
                published = clean(entry.findtext("atom:published","",ns))
                if vid and title:
                    items.append({"id":vid,"title":title,"url":f"https://www.youtube.com/watch?v={vid}","published":published,
                                  "thumbnail":f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
        except Exception as e:
            print("YouTube RSS warning:", e)

    write_json("youtube.json", {"channel":"Sporting CP","channel_id":channel_id,"items":items[:50],"source":"YouTube"})
    print(f"YouTube: {len(items[:50])} videos written.")

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

    for item in items[:10]:
        if item.get("source") != "Sporting.pt": continue
        try:
            rr = session.get(item["url"], timeout=10); rr.raise_for_status(); ss = BeautifulSoup(rr.text, "html.parser")
            og = ss.find("meta", attrs={"property": "og:image"}) or ss.find("meta", attrs={"name": "twitter:image"})
            desc = ss.find("meta", attrs={"property": "og:description"}) or ss.find("meta", attrs={"name": "description"})
            if og and og.get("content"): item["image"] = og.get("content")
            if desc and desc.get("content"): item["description"] = clean(desc.get("content"))[:280]
        except Exception as e: print("News metadata warning:", item.get("url"), e)

    write_json("news.json", {"items": items})



def build_fixtures_from_fbref():
    """Build the main fixtures.json from the current FBref schedule."""

    schedule_data = safe_existing("fbref-schedule.json") or {}
    schedule = schedule_data.get("fixtures", [])

    if not schedule:
        raise RuntimeError(
            "No FBref schedule available. fixtures.json was not overwritten."
        )

    out = []

    for i, x in enumerate(schedule):
        d = clean(x.get("date"))
        if not d:
            continue

        time_s = clean(x.get("time"))
        if not time_s or time_s in {"—", "-", "None"}:
            time_s = "12:00"

        try:
            stamp = int(
                datetime.fromisoformat(f"{d}T{time_s}")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except Exception:
            try:
                stamp = int(
                    datetime.fromisoformat(d)
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except Exception:
                continue

        opponent = clean(x.get("opponent"))
        venue_side = clean(x.get("venue_side")).lower()
        result = clean(x.get("result"))

        if not opponent:
            continue

        if venue_side == "home":
            home_name = "Sporting CP"
            away_name = opponent
            home_goals = x.get("gf") if result else None
            away_goals = x.get("ga") if result else None
        else:
            home_name = opponent
            away_name = "Sporting CP"
            home_goals = x.get("ga") if result else None
            away_goals = x.get("gf") if result else None

        out.append({
            "id": f"fbref-{d}-{i}",
            "date": stamp,
            "kickoff_date": d,
            "kickoff_local_time": time_s,
            "status": {
                "short": "finished" if result else "scheduled",
                "long": "Terminado" if result else "Agendado"
            },
            "referee": clean(x.get("referee")),
            "venue": {
                "name": clean(x.get("venue")),
                "city": None,
                "lat": None,
                "lon": None,
                "capacity": None
            },
            "competition": {
                "name": clean(x.get("competition")),
                "round": clean(x.get("round")),
                "season": "2026/27"
            },
            "home": {"name": home_name},
            "away": {"name": away_name},
            "goals": {"home": home_goals, "away": away_goals},
            "half_time": {"home": None, "away": None},
            "source": "FBref"
        })

    if not out:
        raise RuntimeError(
            "FBref schedule was found but contained no usable fixtures. "
            "fixtures.json was not overwritten."
        )

    out.sort(key=lambda x: x.get("date") or 0)

    write_json("fixtures.json", {
        "team_id": None,
        "fixtures": out,
        "source": "FBref",
        "season": "2026/27"
    })

    print(f"FBref: {len(out)} fixtures written to fixtures.json.")
    return out

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
    try:
        fetch_youtube()
    except Exception as e:
        errors.append(f"youtube: {e}")

    if mode in {"full", "football"}:
        try:
            fetch_fotmob_core()
        except Exception as e:
            errors.append(f"fotmob-core: {e}")

        # SofaScore is a free secondary calendar source. If available, use it to
        # refresh the public fixture feed; if it fails, keep the FotMob feed.
        try:
            fetch_sofascore_fixtures()
        except Exception as e:
            print(f"SofaScore fixtures skipped: {e}")
        try:
            fetch_sofascore_player_stats()
        except Exception as e:
            print(f"SofaScore player enrichment skipped: {e}")
        try:
            fetch_sofascore_standings()
        except Exception as e:
            print(f"SofaScore standings skipped: {e}")
        try:
            fetch_competition_standings()
        except Exception as e:
            print(f"SofaScore competition standings skipped: {e}")
        try:
            build_team_stats_from_sofa()
        except Exception as e:
            print(f"SofaScore team stats skipped: {e}")
        try:
            fetch_sofascore_match_details()
        except Exception as e:
            print(f"SofaScore match details skipped: {e}")
        try:
            enrich_player_profiles()
        except Exception as e:
            print(f"Player profile enrichment skipped: {e}")

        try:
            geocode_missing_venues()
        except Exception as e:
            print(f"Map enrichment skipped: {e}")

        try:
            fetch_fbref_stats()
        except Exception as e:
            print(f"FBref enrichment skipped: {e}")

        try:
            fetch_fsa_fixtures()
            enrich_match_details()
        except Exception as e:
            print(f"Football API enrichment skipped: {e}")

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
        print("Completed with warnings:", *errors, sep="\\n- ")
    else:
        print("Lion's Den data update completed successfully.")


if __name__ == "__main__":
    main()
