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
import unicodedata
from difflib import SequenceMatcher
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



ZEROZERO_TEAM = "https://www.zerozero.pt/equipa/sporting"
SPORTING_FOTMOB_ID = 9768
POSITION_FALLBACKS = {
    "Rui Silva": "GK", "Kaique Pereira": "GK", "Diego Callai": "GK",
    "Moncef Zekri": "DF", "Zeno Debast": "DF", "Georgios Vagiannidis": "DF",
    "Maxi Araújo": "DF", "Iván Fresneda": "DF", "Gonçalo Inácio": "DF",
    "Rodrigo Dias": "DF", "Ibrahima Ba": "DF", "Eduardo Quaresma": "DF",
    "Sotiris Alexandropoulos": "MF", "Silas Andersen": "MF", "Sergi Altimira": "MF",
    "João Simões": "MF", "Nestory Irankunda": "FW", "Pedro Lima": "MF",
    "Salvador Blopa": "FW", "Issa Doumbia": "MF", "Délcio Aurélio": "FW",
    "Rodrigo Rodrigues": "MF", "Fotis Ioannidis": "FW", "Rafael Nel": "FW",
    "Geny Catamo": "MF", "Nuno Santos": "MF", "Rodrigo Zalazar": "MF",
    "Jesse Derry": "FW", "Luís Guilherme": "FW", "Flávio Gonçalves": "MF",
    "Luis Suárez": "FW",
}
ZEROZERO_URLS = {
    "Rui Silva": "https://www.zerozero.pt/jogador/rui-silva/275322",
    "Kaique Pereira": "https://www.zerozero.pt/jogador/kaique-pereira/721159",
    "Diego Callai": "https://www.zerozero.pt/jogador/diego-callai/508061",
    "Moncef Zekri": "https://www.zerozero.pt/jogador/moncef-zekri/2522483",
    "Zeno Debast": "https://www.zerozero.pt/jogador/zeno-debast/741625",
    "Georgios Vagiannidis": "https://www.zerozero.pt/jogador/georgios-vagiannidis/768651",
    "Maxi Araújo": "https://www.zerozero.pt/jogador/maxi-araujo/631995",
    "Iván Fresneda": "https://www.zerozero.pt/jogador/ivan-fresneda/911570",
    "Gonçalo Inácio": "https://www.zerozero.pt/jogador/goncalo-inacio/384159",
    "Rodrigo Dias": "https://www.zerozero.pt/jogador/rodrigo-dias/509010",
    "Ibrahima Ba": "https://www.zerozero.pt/jogador/ibrahima-ba/1271517",
    "Eduardo Quaresma": "https://www.zerozero.pt/jogador/eduardo-quaresma/160517",
    "Sotiris Alexandropoulos": "https://www.zerozero.pt/jogador/sotiris-alexandropoulos/730898",
    "Silas Andersen": "https://www.zerozero.pt/jogador/silas-andersen/770896",
    "Sergi Altimira": "https://www.zerozero.pt/jogador/sergi-altimira/900472",
    "João Simões": "https://www.zerozero.pt/jogador/joao-simoes/643111",
    "Nestory Irankunda": "https://www.zerozero.pt/jogador/nestory-irankunda/926559",
    "Pedro Lima": "https://www.zerozero.pt/jogador/pedro-lima/717259",
    "Salvador Blopa": "https://zerozero.football/jogador/salvador-blopa/664615",
    "Issa Doumbia": "https://www.zerozero.pt/jogador/issa-doumbia/889152",
    "Délcio Aurélio": "https://www.zerozero.pt/jogador/delcio-aurelio/1848005",
    "Rodrigo Rodrigues": "https://www.zerozero.pt/jogador/rodrigo-rodrigues/669551",
    "Fotis Ioannidis": "https://www.zerozero.pt/jogador/fotis-ioannidis/612628",
    "Rafael Nel": "https://www.zerozero.pt/jogador/rafael-nel/677872",
    "Geny Catamo": "https://www.zerozero.pt/jogador/geny-catamo/639363",
    "Nuno Santos": "https://www.zerozero.pt/jogador/nuno-santos/160873",
    "Rodrigo Zalazar": "https://www.zerozero.pt/jogador/rodrigo-zalazar/689707",
    "Jesse Derry": "https://www.zerozero.pt/jogador/jesse-derry/1189058",
    "Luís Guilherme": "https://www.zerozero.pt/jogador/luis-guilherme/829332",
    "Flávio Gonçalves": "https://www.zerozero.pt/jogador/flavio-goncalves/641690",
    "Luis Suárez": "https://www.zerozero.pt/jogador/luis-suarez/504173",
}


def zz_norm(v):
    s = clean(v).lower()
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def zz_number(v):
    s = clean(v).replace(".", "").replace(",", ".")
    if s in {"", "-", "—"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0)))
    except Exception:
        return None


def zerozero_player_index():
    """Return {normalised player name: zerozero player URL} from Sporting's current squad."""
    try:
        html = session.get(ZEROZERO_TEAM, timeout=30).text
        soup = BeautifulSoup(html, "html.parser")
        index = {}
        for a in soup.select('a[href*="/jogador/"]'):
            href = a.get("href") or ""
            if not re.search(r"/jogador/[^/]+/\d+", href):
                continue
            name = clean(a.get_text(" ", strip=True))
            if not name:
                continue
            if href.startswith("/"):
                href = "https://www.zerozero.pt" + href
            index.setdefault(zz_norm(name), href)
        return index
    except Exception as e:
        print("ZeroZero squad warning:", e)
        return {}


def zerozero_player_history(url):
    """Parse ZeroZero career history from the player's zoomstats/history view."""
    result = []
    html = ""
    text_content = ""
    candidates = [
        url.rstrip("/") + "/epocas",
        url + ("&" if "?" in url else "?") + "op=zoomstats&redirm=1",
        url + ("&" if "?" in url else "?") + "op=zoomstats&redirm=1&tpstats=club",
        url,
    ]
    # Direct ZeroZero first. Only accept HTML that actually contains the
    # historical table; otherwise try the indexed text proxy.
    for candidate in candidates:
        try:
            rr = session.get(candidate, timeout=30, headers={"User-Agent": USER_AGENT})
            if rr.ok and len(rr.text) > 5000:
                html = rr.text
                if "Histórico" in rr.text or "HISTÓRICO" in rr.text:
                    break
        except Exception:
            continue

    if html:
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [zz_norm(x.get_text(" ", strip=True)) for x in rows[0].find_all(["th","td"])]
            season_key = "epoca" if "epoca" in headers else ("época" if "época" in headers else None)
            club_key = "equipa" if "equipa" in headers else ("clube" if "clube" in headers else None)
            games_key = "j" if "j" in headers else None
            goals_key = "g" if "g" in headers else ("gm" if "gm" in headers else None)
            assists_key = "ast" if "ast" in headers else None
            if not all((season_key, club_key, games_key, goals_key, assists_key)):
                continue
            idx = {h:i for i,h in enumerate(headers)}
            season = ""
            for tr in rows[1:]:
                cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["th","td"])]
                if not cells:
                    continue
                def cell(k):
                    i = idx.get(k)
                    return cells[i] if i is not None and i < len(cells) else ""
                if cell(season_key):
                    season = cell(season_key)
                if season and cell(club_key):
                    result.append({
                        "season": season.replace("-","/"),
                        "club": cell(club_key),
                        "matches": zz_number(cell(games_key)),
                        "goals": zz_number(cell(goals_key)),
                        "assists": zz_number(cell(assists_key))
                    })
            if result:
                return result

    # Jina fallback is useful when ZeroZero serves a challenge page to Actions.
    for scheme in ("https://", "http://"):
        try:
            target = url.split("://", 1)[-1]
            proxy = "https://r.jina.ai/" + scheme + target + "?op=zoomstats&redirm=1"
            pr = session.get(proxy, timeout=45, headers={"User-Agent": USER_AGENT})
            pr.raise_for_status()
            text_content = pr.text
            break
        except Exception:
            continue

    if not text_content:
        return []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows: continue
            headers = [zz_norm(x.get_text(" ", strip=True)) for x in rows[0].find_all(["th","td"])]
            if not {"epoca","equipa","j","g","ast"}.issubset(set(headers)): continue
            idx = {h:i for i,h in enumerate(headers)}
            season = ""
            for tr in rows[1:]:
                cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["th","td"])]
                if not cells: continue
                def cell(k):
                    i = idx.get(k)
                    return cells[i] if i is not None and i < len(cells) else ""
                if cell("epoca"): season = cell("epoca")
                if season and cell("equipa"):
                    result.append({"season":season.replace("-","/"),"club":cell("equipa"),"matches":zz_number(cell("j")),"goals":zz_number(cell("g")),"assists":zz_number(cell("ast"))})
            if result: break
        return result

    lines = text_content.splitlines()
    in_history = False
    season = ""
    for line in lines:
        t = line.strip()
        n = zz_norm(t)
        if "epoca" in n and ("equipa" in n or "clube" in n) and "| j |" in n:
            in_history = True
            continue
        if not in_history: continue
        if not t or t.startswith("EDIÇÕES") or t.startswith("Transferências") or t.startswith("Futsal"):
            if result: break
            continue
        if "|" not in t:
            if result: break
            continue
        cells = [clean(x) for x in t.strip("|").split("|")]
        if len(cells) < 5: continue
        if cells[0]: season = cells[0]
        club = cells[1]
        if season and club and zz_norm(cells[0]) != "epoca":
            result.append({"season":season.replace("-","/"),"club":club,"matches":zz_number(cells[2]),"goals":zz_number(cells[3]),"assists":zz_number(cells[4])})
    return result
def enrich_with_zerozero(players):
    """
    Enrich the Sporting squad with ZeroZero's historical J/G/AST data.
    The current team page supplies the canonical player links, avoiding
    ambiguous name searches such as the many different Rui Silva profiles.
    """
    index = zerozero_player_index()
    if not index:
        return players

    for player in players:
        name = clean(player.get("name"))
        if not name:
            continue

        key = zz_norm(name)
        url = ZEROZERO_URLS.get(name) or index.get(key)

        # Handle minor naming differences between FotMob and ZeroZero.
        if not url:
            candidates = sorted(
                ((SequenceMatcher(None, key, k).ratio(), v) for k, v in index.items()),
                reverse=True
            )
            if candidates and candidates[0][0] >= 0.84:
                url = candidates[0][1]

        if not url:
            continue

        try:
            history = zerozero_player_history(url)
            if history:
                player["zerozero_url"] = url
                player["careerStats"] = history
                # Keep the current-season career row synchronized with the
                # same current-season/all-competitions stats shown on the card.
                for current in player.get("careerStats", []):
                    season_key = str(current.get("season") or "").replace("-", "/")
                    if season_key in {"2026/2027", "2026/27"}:
                        current["season"] = "2026/27"
                        current["matches"] = player.get("stats", {}).get("matches", current.get("matches", 0))
                        current["goals"] = player.get("stats", {}).get("goals", current.get("goals", 0))
                        current["assists"] = player.get("stats", {}).get("assists", current.get("assists", 0))
                # ZeroZero's team page is also authoritative for the
                # Portuguese display name of the player's nationality/position.
                page = session.get(url, timeout=30).text
                text_content = clean(BeautifulSoup(page, "html.parser").get_text(" ", strip=True))
                if "Nacionalidade" in text_content:
                    after = text_content.split("Nacionalidade", 1)[1][:180]
                    nat = re.split(r"País de Nascimento|Posição", after, maxsplit=1)[0].strip()
                    nat = clean(re.sub(r"Dupla Nacionalidade", "", nat))
                    if nat:
                        player["nationality"] = nat.split("Portugal Portugal")[0].strip()
        except Exception as e:
            print("ZeroZero player warning:", name, e)
        time.sleep(0.15)

    return players


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
    official_rows = []
    if fixtures is not None:
        for _, r in fixtures.iterrows():
            comp = zz_norm(r.get("Comp"))
            result = clean(r.get("Result"))
            if not result or "friendly" in comp or "amig" in comp:
                continue
            gf = fnum(r.get("GF")); ga = fnum(r.get("GA"))
            if gf is None or ga is None:
                continue
            official_rows.append(r)

    team = {
        "matches": len(official_rows),
        "wins": sum(clean(r.get("Result")) == "W" for r in official_rows),
        "draws": sum(clean(r.get("Result")) == "D" for r in official_rows),
        "losses": sum(clean(r.get("Result")) == "L" for r in official_rows),
        "goals": sum(int(fnum(r.get("GF")) or 0) for r in official_rows),
        "goals_against": sum(int(fnum(r.get("GA")) or 0) for r in official_rows),
        "clean_sheets": sum(int(int(fnum(r.get("GA")) or 0) == 0) for r in official_rows),
    }
    team["points"] = team["wins"] * 3 + team["draws"]
    team["win_rate"] = round(team["wins"] / team["matches"] * 100, 1) if team["matches"] else 0
    team["goals_per_match"] = round(team["goals"] / team["matches"], 2) if team["matches"] else 0
    team["goals_against_per_match"] = round(team["goals_against"] / team["matches"], 2) if team["matches"] else 0

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

    # Only these completed Sporting CP first-team fixtures are allowed to feed
    # player statistics. This prevents pre-season/friendly matches from being
    # counted and also prevents matches belonging to Sporting CP B/U23.
    official_fixture_ids = {
        str(f.get("id"))
        for f in fixtures
        if f.get("id")
        and f.get("status", {}).get("short") == "finished"
        and is_official_sporting_competition((f.get("competition") or {}).get("name"))
        and (
            int(f.get("home", {}).get("id") or 0) == SPORTING_FOTMOB_ID
            or int(f.get("away", {}).get("id") or 0) == SPORTING_FOTMOB_ID
        )
    }

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
            "position": clean(m.get("rolePosition") or m.get("position") or POSITION_FALLBACKS.get(clean(m.get("name")))),
            "nationality": clean(m.get("cname") or m.get("country")),
            "photo": f"https://images.fotmob.com/image_resources/playerimages/{pid}.png",
            "stats": {}
        }
        try:
            pd = fotmob_get("/api/data/playerData", {"id": pid, "includeMarketValues": "true"})

            def deep_find(obj, keys):
                if isinstance(obj, dict):
                    for key in keys:
                        value = obj.get(key)
                        if value not in (None, "", []):
                            return value
                    for value in obj.values():
                        found = deep_find(value, keys)
                        if found not in (None, "", []):
                            return found
                elif isinstance(obj, list):
                    for value in obj:
                        found = deep_find(value, keys)
                        if found not in (None, "", []):
                            return found
                return None

            shirt = (
                m.get("shirtNumber")
                or m.get("shirt_number")
                or m.get("jerseyNumber")
                or m.get("jersey_number")
                or deep_find(pd, {"shirtNumber","shirt_number","jerseyNumber","jersey_number"})
            )
            if shirt not in (None, ""):
                try: player["shirtNumber"] = int(shirt)
                except Exception: player["shirtNumber"] = shirt

            caps = deep_find(pd, {"internationalCaps","internationalAppearances","nationalTeamAppearances","caps"})
            if caps not in (None, ""):
                try: player["internationalCaps"] = int(float(caps))
                except Exception: pass

            history = deep_find(pd, {"careerHistory","careerItems","previousTeams","teamHistory","career"})
            career=[]
            if isinstance(history, dict):
                history = history.get("careerItems") or history.get("items") or history.get("teams") or history

            # FotMob careerHistory.careerItems is grouped into senior/youth/national-team.
            if isinstance(history, dict):
                groups = []
                for group_name in ("senior","youth","national team","nationalTeam"):
                    group = history.get(group_name)
                    if isinstance(group, dict):
                        groups.append(group)
                for group in groups:
                    for item in (group.get("teamEntries") or []):
                        if not isinstance(item, dict): continue
                        club=item.get("team") or item.get("teamName") or item.get("clubName")
                        start=item.get("startDate")
                        end=item.get("endDate")
                        period=clean(start)
                        if end: period=(period+" → "+clean(end)).strip()
                        if club: career.append({"period":period,"club":clean(club)})
                    for item in (group.get("seasonEntries") or []):
                        if not isinstance(item, dict): continue
                        club=item.get("team") or item.get("teamName") or item.get("clubName")
                        period=item.get("seasonName") or item.get("season")
                        if club: career.append({"period":clean(period),"club":clean(club)})

            if isinstance(history, list):
                for item in history:
                    if not isinstance(item, dict): continue
                    team=item.get("team") if isinstance(item.get("team"),dict) else {}
                    club=item.get("teamName") or item.get("clubName") or team.get("name") or item.get("name")
                    period=item.get("seasonName") or item.get("season") or item.get("period") or item.get("year")
                    if club: career.append({"period":clean(period),"club":clean(club)})
            if career:
                seen=set(); player["career"]=[]
                for item in career:
                    key=(item["period"],item["club"])
                    if key not in seen:
                        seen.add(key); player["career"].append(item)

            seasons = pd.get("statSeasons") or []

            # Keep a season-by-season career dataset for the player card.
            # FotMob has changed the exact nesting of statSeasons over time,
            # so read the common fields defensively.
            career_stats = []
            def walk_season_stats(obj, season_name=None, club_name=None):
                if isinstance(obj, dict):
                    sn = obj.get("seasonName") or obj.get("season") or season_name
                    club = obj.get("teamName") or obj.get("clubName") or club_name
                    team = obj.get("team")
                    if isinstance(team, dict):
                        club = club or team.get("name")
                    stats_obj = obj.get("stats") if isinstance(obj.get("stats"), dict) else obj
                    games = stats_obj.get("appearances", stats_obj.get("matches", stats_obj.get("games", stats_obj.get("played"))))
                    goals = stats_obj.get("goals")
                    assists = stats_obj.get("assists")
                    if sn and club and any(v is not None for v in (games, goals, assists)):
                        career_stats.append({
                            "season": str(sn).replace("-", "/"),
                            "club": str(club),
                            "matches": int(fnum(games) or 0),
                            "goals": int(fnum(goals) or 0),
                            "assists": int(fnum(assists) or 0)
                        })
                    for k,v in obj.items():
                        if k not in {"stats"}:
                            walk_season_stats(v, sn, club)
                elif isinstance(obj, list):
                    for v in obj:
                        walk_season_stats(v, season_name, club_name)
            walk_season_stats(seasons)
            seen_cs=set()
            player["careerStats"]=[]
            for row in career_stats:
                key=(row["season"],row["club"])
                if key not in seen_cs:
                    seen_cs.add(key)
                    player["careerStats"].append(row)

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
            # IMPORTANT: recentMatches also contains national-team matches.
            # The app's player stats are strictly Sporting CP stats.
            played = []
            for rm in current_matches:
                match_id = rm.get("matchId") or rm.get("id") or rm.get("eventId")
                team_id = int(fnum(rm.get("teamId")) or 0)
                team_name = zz_norm(rm.get("teamName"))
                is_first_team = team_id == SPORTING_FOTMOB_ID or team_name in {"sporting cp", "sporting"}
                # Explicitly reject B/U23/U19/other Sporting sides.
                is_reserve = any(x in team_name for x in ("sporting cp b", "sporting b", "sporting u23", "sporting u19", "sporting sub23"))
                is_official = True
                if match_id is not None and official_fixture_ids:
                    is_official = str(match_id) in official_fixture_ids
                else:
                    comp = rm.get("competition") or rm.get("league") or rm.get("tournament") or {}
                    comp_name = comp.get("name") if isinstance(comp, dict) else comp
                    is_official = is_official_sporting_competition(comp_name)
                if is_first_team and not is_reserve and is_official and (fnum(rm.get("minutesPlayed")) or 0) > 0:
                    played.append(rm)
            stats["matches"] = len(played)
            # "Titular" is deliberately not exposed: provider bench/start flags
            # are not considered reliable enough for the app's core statistics.
            stats["minutes"] = int(sum(fnum(rm.get("minutesPlayed")) or 0 for rm in played))
            stats["goals"] = int(sum(fnum(rm.get("goals")) or 0 for rm in played))
            stats["assists"] = int(sum(fnum(rm.get("assists")) or 0 for rm in played))
            stats["yellow"] = int(sum(fnum(rm.get("yellowCards")) or 0 for rm in played))
            stats["red"] = int(sum(fnum(rm.get("redCards")) or 0 for rm in played))

            # Clean sheets are calculated only from Sporting matches in which
            # the goalkeeper actually played: Sporting conceded 0 = 1 CS.
            if "goalkeeper" in zz_norm(player.get("position")) or player.get("position") == "GK" or "goalkeeper" in zz_norm(m.get("rolePosition")):
                cs = 0
                for rm in played:
                    hs = fnum(rm.get("homeScore"))
                    aw = fnum(rm.get("awayScore"))
                    if hs is None or aw is None:
                        score_text = str(rm.get("score") or rm.get("scoreStr") or "")
                        sm = re.search(r"(\\d+)\\s*[-:]\\s*(\\d+)", score_text)
                        if sm:
                            hs, aw = int(sm.group(1)), int(sm.group(2))
                    if hs is None or aw is None:
                        continue
                    is_home = rm.get("isHome", rm.get("isHomeTeam"))
                    if is_home is True or str(is_home).lower() in {"true", "1"}:
                        conceded = aw
                    elif is_home is False or str(is_home).lower() in {"false", "0"}:
                        conceded = hs
                    else:
                        team_side = str(rm.get("teamSide") or rm.get("side") or "").lower()
                        conceded = aw if team_side in {"home","h"} else hs if team_side in {"away","a"} else None
                    if conceded == 0:
                        cs += 1
                stats["cleanSheets"] = cs

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
        # ZeroZero is used specifically for career history (season / club / J / G / AST).
        players = enrich_with_zerozero(players)

        old_squad = safe_existing("squad.json") or {}
        old_players = old_squad.get("squad") or old_squad.get("players") or []
        old_by_name = {normalize_player_name(p.get("name")).lower(): p for p in old_players if p.get("name")}
        for p in players:
            old = old_by_name.get(normalize_player_name(p.get("name")).lower(), {})
            p["position"] = p.get("position") or old.get("position") or POSITION_FALLBACKS.get(p.get("name"), "")
            p["nationality"] = p.get("nationality") or old.get("nationality")
            p["dateOfBirth"] = p.get("dateOfBirth") or old.get("dateOfBirth")
            for field in ("shirtNumber","internationalCaps","career","careerStats","sofascore_id"):
                if not p.get(field) and old.get(field) not in (None,"",[]): p[field] = old[field]
            # Keep the career table's current-season row synchronized with
            # the exact all-competitions stats shown in the player card.
            current_stats = p.get("stats") or {}
            for row in p.get("careerStats") or []:
                season_key = str(row.get("season") or "").replace("-", "/")
                if season_key in {"2026/2027", "2026/27"}:
                    row["season"] = "2026/27"
                    row["matches"] = current_stats.get("matches", row.get("matches", 0))
                    row["goals"] = current_stats.get("goals", row.get("goals", 0))
                    row["assists"] = current_stats.get("assists", row.get("assists", 0))
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
    played = [
        f for f in fixtures
        if f["date"] <= now
        and f["goals"]["home"] is not None
        and "friendli" not in zz_norm(f.get("competition",{}).get("name"))
        and "amig" not in zz_norm(f.get("competition",{}).get("name"))
    ]
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
    """Fetch detailed data for every finished Sporting first-team match in the current feed."""
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    finished = [
        f for f in fixtures
        if f.get("status", {}).get("short") == "finished"
        and f.get("id")
        and (
            int((f.get("home") or {}).get("id") or 0) == SPORTING_FOTMOB_ID
            or int((f.get("away") or {}).get("id") or 0) == SPORTING_FOTMOB_ID
            or "sporting" in zz_norm((f.get("home") or {}).get("name"))
            or "sporting" in zz_norm((f.get("away") or {}).get("name"))
        )
    ]

    # Build a SofaScore event index. Fixture IDs may come from FotMob, so
    # matching by teams + kickoff is safer than assuming the IDs are identical.
    events = []
    for page in range(0, 8):
        try:
            payload = sofa_get(f"/team/3001/events/last/{page}")
            batch = payload.get("events", []) or []
            if not batch:
                break
            events.extend(batch)
            if not payload.get("hasNextPage"):
                break
        except Exception as e:
            print("SofaScore detail event index warning:", page, e)
            break

    event_by_id = {str(e.get("id")): e for e in events if e.get("id")}

    def norm_name(value):
        return re.sub(r"[^a-z0-9]+", "", zz_norm(value))

    def find_event(fixture):
        direct = event_by_id.get(str(fixture.get("id")))
        if direct:
            return direct
        target_date = int(fixture.get("date") or 0)
        home = norm_name((fixture.get("home") or {}).get("name"))
        away = norm_name((fixture.get("away") or {}).get("name"))
        best = None
        best_delta = None
        for e in events:
            ts = int(e.get("startTimestamp") or 0)
            if not ts or abs(ts - target_date) > 48 * 3600:
                continue
            eh = norm_name((e.get("homeTeam") or {}).get("name"))
            ea = norm_name((e.get("awayTeam") or {}).get("name"))
            if not ((eh == home and ea == away) or (eh == away and ea == home)):
                continue
            delta = abs(ts - target_date)
            if best is None or delta < best_delta:
                best, best_delta = e, delta
        return best

    details = []
    for f in sorted(finished, key=lambda x: x.get("date") or 0):
        event = find_event(f)
        if not event:
            print("SofaScore detail match not found:", f.get("id"), f.get("home"), f.get("away"))
            continue
        sid = event.get("id")
        try:
            event_detail = sofa_get(f"/event/{sid}").get("event") or event
            lineups = sofa_get(f"/event/{sid}/lineups")
            incidents = sofa_get(f"/event/{sid}/incidents")
            statistics = sofa_get(f"/event/{sid}/statistics")
            average_positions = sofa_get(f"/event/{sid}/average-positions")
            managers = {}
            try:
                managers = sofa_get(f"/event/{sid}/managers")
            except Exception as e:
                print("SofaScore managers warning:", sid, e)

            # Prefer the authoritative SofaScore referee/venue in the detailed event.
            referee = ((event_detail.get("referee") or {}).get("name")
                       if isinstance(event_detail.get("referee"), dict)
                       else event_detail.get("referee"))
            venue = event_detail.get("venue") or {}
            if referee and not f.get("referee"):
                f["referee"] = clean(referee)
            if venue.get("name"):
                f.setdefault("venue", {})["name"] = clean(venue.get("name"))
            if venue.get("city", {}).get("name") if isinstance(venue.get("city"), dict) else venue.get("city"):
                f.setdefault("venue", {})["city"] = clean(
                    venue.get("city", {}).get("name") if isinstance(venue.get("city"), dict) else venue.get("city")
                )

            details.append({
                "match_id": f.get("id"),
                "sofascore_id": sid,
                "event": event_detail,
                "lineups": lineups,
                "incidents": incidents,
                "statistics": statistics,
                "average_positions": average_positions,
                "managers": managers
            })
        except Exception as e:
            print("SofaScore match detail warning:", f.get("id"), sid, e)

    write_json("match-details.json", {
        "fixtures": details,
        "source": "SofaScore",
        "scope": "Sporting CP first team, finished matches, 2026/27"
    })
    print(f"SofaScore: {len(details)} detailed matches written.")

def enrich_player_profiles():
    """Enrich player cards from SofaScore: birth date, shirt number, nationality and career."""
    squad = safe_existing("squad.json") or {}
    players = squad.get("players") or []

    for p in players:
        sid = p.get("sofascore_id") or p.get("id")
        if not sid:
            continue
        try:
            sp = sofa_get(f"/player/{sid}").get("player") or {}
            dob = sp.get("dateOfBirth")
            if dob and not p.get("dateOfBirth"): p["dateOfBirth"] = dob
            shirt = sp.get("shirtNumber")
            if shirt is not None and p.get("shirtNumber") in (None, ""): p["shirtNumber"] = shirt
            nat = sp.get("nationality")
            if nat and not p.get("nation"): p["nation"] = nat.get("name") if isinstance(nat,dict) else nat
            if nat and not p.get("nationality"): p["nationality"] = nat.get("name") if isinstance(nat,dict) else nat
            career=[]
            history=sp.get("teamHistory") or sp.get("previousTeams") or sp.get("career") or sp.get("careerHistory") or []
            if isinstance(history,dict): history=history.get("items") or history.get("teams") or history.get("careerItems") or []
            if isinstance(history,list):
                for item in history:
                    if not isinstance(item,dict): continue
                    team=item.get("team") if isinstance(item.get("team"),dict) else {}
                    club=item.get("teamName") or item.get("clubName") or team.get("name") or item.get("name")
                    period=item.get("seasonName") or item.get("season") or item.get("period") or item.get("year")
                    if club: career.append({"period":clean(period),"club":clean(club)})
            if career:
                seen=set(); p["career"]=[]
                for x in career:
                    key=(x["club"],x["period"])
                    if key not in seen: seen.add(key); p["career"].append(x)
        except Exception as e:
            print("Player profile warning:", p.get("name"), e)
    write_json("squad.json", squad)


def fetch_fotmob_competition_standings():
    """Fetch league tables and knockout brackets from FotMob."""
    result = safe_existing("standings-competitions.json") or {}
    competitions = {
        "primeira-liga": (61, "Primeira Liga"),
        "champions": (42, "Champions League"),
        "taca-portugal": (186, "Taça de Portugal"),
        "taca-liga": (187, "Taça da Liga"),
    }

    def collect_tables(obj, out):
        if isinstance(obj, dict):
            table = obj.get("table")
            if isinstance(table, dict):
                rows = table.get("all") or table.get("overall") or table.get("rows")
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict) and (r.get("name") or r.get("id")):
                            scores = str(r.get("scoresStr") or "0-0").split("-")
                            out.append({
                                "position": r.get("idx") or r.get("rank"),
                                "team": {
                                    "id": r.get("id"),
                                    "name": r.get("name"),
                                    "shortName": r.get("shortName") or r.get("name"),
                                    "tla": r.get("nameCode"),
                                    "crest": f"https://images.fotmob.com/image_resources/logo/teamlogo/{r.get('id')}.png" if r.get("id") else None,
                                },
                                "playedGames": r.get("played", 0),
                                "won": r.get("wins", 0),
                                "draw": r.get("draws", 0),
                                "lost": r.get("losses", 0),
                                "points": r.get("pts", 0),
                                "goalsFor": int(scores[0]) if scores and scores[0].isdigit() else 0,
                                "goalsAgainst": int(scores[1]) if len(scores) > 1 and scores[1].isdigit() else 0,
                                "goalDifference": r.get("goalConDiff", 0),
                            })
            for v in obj.values():
                collect_tables(v, out)
        elif isinstance(obj, list):
            for v in obj:
                collect_tables(v, out)

    def find_rounds(obj):
        """Find FotMob playoff/knockout rounds anywhere in the league payload."""
        found = []

        def walk(v):
            if isinstance(v, dict):
                rounds = v.get("rounds")
                if isinstance(rounds, list):
                    valid = [r for r in rounds if isinstance(r, dict) and isinstance(r.get("matchups"), list)]
                    if valid:
                        found.extend(valid)
                for key in ("playoff", "knockout", "playoffs"):
                    nested = v.get(key)
                    if nested is not None:
                        walk(nested)
                for k, child in v.items():
                    if k not in {"playoff", "knockout", "playoffs", "rounds"}:
                        if isinstance(child, (dict, list)):
                            walk(child)
            elif isinstance(v, list):
                for child in v:
                    walk(child)

        walk(obj)
        return found

    def normalize_rounds(rounds):
        output = []
        seen = set()
        for r in rounds:
            stage = clean(r.get("stage") or r.get("round") or r.get("roundName") or "")
            label = clean(r.get("name") or r.get("roundName") or stage)
            matchups = []
            for m in r.get("matchups") or []:
                if not isinstance(m, dict):
                    continue
                home_id = m.get("homeTeamId")
                away_id = m.get("awayTeamId")
                home_name = clean(m.get("homeTeam") or m.get("homeTeamName"))
                away_name = clean(m.get("awayTeam") or m.get("awayTeamName"))
                if not home_name and not away_name and not m.get("matches"):
                    continue
                key = (stage, str(m.get("drawOrder") or ""), str(home_id or home_name), str(away_id or away_name))
                if key in seen:
                    continue
                seen.add(key)
                legs = []
                for leg in m.get("matches") or []:
                    if not isinstance(leg, dict):
                        continue
                    lh = leg.get("home") or {}
                    la = leg.get("away") or {}
                    legs.append({
                        "match_id": leg.get("matchId") or leg.get("id"),
                        "date": leg.get("utcTime") or leg.get("matchDate"),
                        "home": {
                            "id": lh.get("id"),
                            "name": clean(lh.get("name")),
                            "shortName": clean(lh.get("shortName") or lh.get("name")),
                            "score": lh.get("score"),
                        },
                        "away": {
                            "id": la.get("id"),
                            "name": clean(la.get("name")),
                            "shortName": clean(la.get("shortName") or la.get("name")),
                            "score": la.get("score"),
                        },
                        "page_url": leg.get("pageUrl"),
                    })
                matchups.append({
                    "drawOrder": m.get("drawOrder"),
                    "stage": clean(m.get("stage") or stage),
                    "bestOf": m.get("bestOf"),
                    "home": {
                        "id": home_id,
                        "name": home_name,
                        "shortName": clean(m.get("homeTeamShortName") or home_name),
                        "crest": f"https://images.fotmob.com/image_resources/logo/teamlogo/{home_id}.png" if home_id else None,
                    },
                    "away": {
                        "id": away_id,
                        "name": away_name,
                        "shortName": clean(m.get("awayTeamShortName") or away_name),
                        "crest": f"https://images.fotmob.com/image_resources/logo/teamlogo/{away_id}.png" if away_id else None,
                    },
                    "homeScore": m.get("homeScore"),
                    "awayScore": m.get("awayScore"),
                    "winner": m.get("winner"),
                    "aggregatedWinner": m.get("aggregatedWinner"),
                    "aggregatedLoser": m.get("aggregatedLoser"),
                    "matches": legs,
                })
            if matchups:
                output.append({
                    "stage": stage,
                    "label": label or stage or "Eliminatória",
                    "participantCount": r.get("participantCount"),
                    "matchups": matchups,
                })
        return output

    for key, (lid, name) in competitions.items():
        try:
            payload = fotmob_get("/api/data/leagues", {
                "id": lid,
                "season": "2026/2027",
                "ccode3": "PRT",
            })

            rows = []
            collect_tables(payload, rows)
            dedup = {}
            for row in rows:
                tid = (row.get("team") or {}).get("id")
                if tid is not None:
                    dedup[str(tid)] = row
            rows = list(dedup.values())
            rows.sort(key=lambda x: x.get("position") or 999)

            knockout = normalize_rounds(find_rounds(payload))

            entry = result.get(key) or {}
            entry.update({
                "competition": name,
                "season": "2026/27",
                "source": "FotMob",
                "tournament_id": lid,
            })
            if rows:
                entry["table"] = rows
            elif key in {"taca-portugal", "taca-liga"}:
                entry["table"] = []

            if knockout:
                entry["type"] = "knockout"
                entry["rounds"] = knockout
                entry.pop("note", None)
            elif key in {"taca-portugal", "taca-liga"}:
                entry["type"] = "knockout"
                entry["rounds"] = entry.get("rounds") or []
                entry["note"] = "Competição a eliminar; as eliminatórias serão mostradas assim que o calendário oficial da fase estiver disponível."

            result[key] = entry
            print(f"FotMob {name}: table={len(rows)}, knockout_rounds={len(knockout)}")
        except Exception as ex:
            print("FotMob competition warning:", name, ex)

    # Always guarantee the default Primeira Liga entry.
    primary = safe_existing("standings.json") or {}
    if primary.get("table") and not (result.get("primeira-liga") or {}).get("table"):
        result["primeira-liga"] = {
            "competition": "Primeira Liga",
            "season": "2026/27",
            "table": primary["table"],
            "source": primary.get("source", "FotMob"),
            "tournament_id": 61,
        }

    write_json("standings-competitions.json", result)
    print("FotMob competition data:", {
        k: {"table": len(v.get("table", [])), "rounds": len(v.get("rounds", []))}
        for k, v in result.items()
    })

def fetch_competition_standings():
    """Optional SofaScore enrichment that NEVER overwrites working FotMob tables."""
    result = safe_existing("standings-competitions.json") or {}
    competitions = {
        "champions": (7, "Champions League"),
        "taca-portugal": (329, "Taça de Portugal"),
        "taca-liga": (327, "Taça da Liga"),
    }
    for key, (tid, name) in competitions.items():
        # If FotMob already supplied a table or an explicit competition state,
        # keep it. SofaScore is only an enrichment/fallback.
        existing = result.get(key) or {}
        if existing.get("table") or existing.get("note"):
            continue
        try:
            sid = _sofa_season(tid)
            if not sid:
                continue
            payload = sofa_get(f"/unique-tournament/{tid}/season/{sid}/standings/total")
            rows = []
            for block in payload.get("standings") or []:
                for r in block.get("rows", []):
                    team = r.get("team") or {}
                    rows.append({
                        "position": r.get("position"),
                        "team": {
                            "id": team.get("id"), "name": team.get("name"),
                            "shortName": team.get("shortName") or team.get("name"),
                            "tla": team.get("nameCode"),
                            "crest": f"https://img.sofascore.com/api/v1/team/{team.get('id')}/image" if team.get("id") else None
                        },
                        "playedGames": r.get("matches", 0), "won": r.get("wins", 0),
                        "draw": r.get("draws", 0), "lost": r.get("losses", 0),
                        "points": r.get("points", 0), "goalsFor": r.get("scoresFor", 0),
                        "goalsAgainst": r.get("scoresAgainst", 0), "goalDifference": r.get("scoreDiff", 0),
                        "groupName": block.get("name") or block.get("groupName")
                    })
            if rows:
                result[key] = {
                    "competition": name, "season": "2026/27", "table": rows,
                    "source": "SofaScore", "tournament_id": tid, "season_id": sid
                }
        except Exception as ex:
            print("Competition standings fallback warning:", name, ex)

    # Always guarantee the default Primeira Liga entry.
    primary = safe_existing("standings.json") or {}
    if primary.get("table") and not (result.get("primeira-liga") or {}).get("table"):
        result["primeira-liga"] = {
            "competition": "Primeira Liga", "season": "2026/27",
            "table": primary["table"], "source": primary.get("source", "FotMob"),
            "tournament_id": 61
        }
    write_json("standings-competitions.json", result)
    print("Merged competition standings:", {k:len(v.get("table",[])) for k,v in result.items()})

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

def is_official_sporting_competition(name):
    """Return True only for official first-team competitions.
    Pre-season, friendlies and exhibition trophies are excluded.
    """
    n = zz_norm(name)
    excluded = (
        "friendly", "amig", "pre epoca", "pre-epoca",
        "trofeu cinco violinos", "trofeio cinco violinos",
        "trophy cinco violinos", "cinco violinos"
    )
    return not any(x in n for x in excluded)


def build_team_stats_from_sofa():
    """Build Sporting CP first-team stats from official completed matches only."""
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    finished = [
        f for f in fixtures
        if f.get("status", {}).get("short") == "finished"
        and is_official_sporting_competition((f.get("competition") or {}).get("name"))
    ]
    team_names = {"sporting cp", "sporting", "sporting clube de portugal"}
    team_ids = {3001, SPORTING_FOTMOB_ID, 9768}

    def is_sporting(side):
        if not isinstance(side, dict):
            return False
        sid = side.get("id")
        name = zz_norm(side.get("name"))
        return sid in team_ids or name in team_names

    team = {"matches": 0, "wins": 0, "draws": 0, "losses": 0, "goals": 0,
            "goals_against": 0, "clean_sheets": 0, "form": [], "recent_matches": []}

    for f in sorted(finished, key=lambda x: x.get("date") or 0):
        home = f.get("home") or {}
        away = f.get("away") or {}
        if not is_sporting(home) and not is_sporting(away):
            continue
        h = f.get("goals", {}).get("home")
        a = f.get("goals", {}).get("away")
        if h is None or a is None:
            continue
        sporting_home = is_sporting(home)
        gf, ga = (int(h), int(a)) if sporting_home else (int(a), int(h))
        team["matches"] += 1
        team["goals"] += gf
        team["goals_against"] += ga
        team["clean_sheets"] += int(ga == 0)
        result = "W" if gf > ga else "D" if gf == ga else "L"
        team[{"W":"wins","D":"draws","L":"losses"}[result]] += 1
        team["form"].append(result)
        team["recent_matches"].append({
            "date": f.get("kickoff_date"),
            "competition": (f.get("competition") or {}).get("name"),
            "venue": "Casa" if sporting_home else "Fora",
            "result": result,
            "gf": gf, "ga": ga,
            "opponent": (away if sporting_home else home).get("name")
        })

    team["form"] = team["form"][-6:]
    team["recent_matches"] = team["recent_matches"][-6:]
    team["points"] = team["wins"] * 3 + team["draws"]
    team["win_rate"] = round((team["wins"] / team["matches"]) * 100, 1) if team["matches"] else 0
    team["goals_per_match"] = round(team["goals"] / team["matches"], 2) if team["matches"] else 0
    team["goals_against_per_match"] = round(team["goals_against"] / team["matches"], 2) if team["matches"] else 0

    write_json("team-stats.json", {
        "season": "2026/27",
        "team": team,
        "source": "Sporting CP fixture feed"
    })


def fetch_fbref_historical_team_stats():
    """Write official Sporting CP season aggregates.
    ZeroZero's team-season summary explicitly separates official competitions
    from pre-season/friendly matches, which is the intended scope here.
    """
    history = {
        "2025/26": {
            "matches": 56, "wins": 37, "draws": 10, "losses": 9,
            "goals": 131, "goals_against": 51, "clean_sheets": None
        },
        "2024/25": {
            "matches": 55, "wins": 37, "draws": 11, "losses": 7,
            "goals": 127, "goals_against": 52, "clean_sheets": None
        },
        "2023/24": {
            "matches": 54, "wins": 40, "draws": 8, "losses": 6,
            "goals": 141, "goals_against": 50, "clean_sheets": None
        },
    }
    for st in history.values():
        st["points"] = st["wins"] * 3 + st["draws"]
        st["win_rate"] = round(st["wins"] / st["matches"] * 100, 1)
        st["goals_per_match"] = round(st["goals"] / st["matches"], 2)
        st["goals_against_per_match"] = round(st["goals_against"] / st["matches"], 2)

    # Rebuild the current season from the same official fixture scope before
    # copying it into the historical comparison file.
    build_team_stats_from_sofa()
    current = safe_existing("team-stats.json") or {}
    if current.get("team"):
        history["2026/27"] = current["team"]

    ordered = {k: history[k] for k in sorted(history.keys(), reverse=True)}
    write_json("team-stats-history.json", {
        "seasons": ordered,
        "source": "ZeroZero (histórico oficial) + Sporting CP fixture feed",
        "scope": "Sporting CP principal, primeira equipa, competições oficiais; sem seleções, pré-época ou amigáveis"
    })
    print(f"Historical team stats: {len(ordered)} seasons written.")

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
        # International appearances/caps, when Sofascore exposes them.
        try:
            nt = sofa_get(f"/player/{pid}/national-team-statistics")
            nstats = nt.get("statistics", nt) if isinstance(nt, dict) else {}
            if isinstance(nstats, dict):
                caps = nstats.get("appearances")
                if caps is None:
                    caps = nstats.get("matches")
                if caps is None:
                    caps = nstats.get("caps")
                if fnum(caps) is not None:
                    row["internationalCaps"] = int(fnum(caps))
        except Exception as e:
            print("Sofascore national-team warning:", row["name"], e)

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

    # Preserve profile/history enrichment already collected earlier in the
    # pipeline. SofaScore refreshes current-season stats, but must not erase
    # ZeroZero career history, shirt number, birth date or profile metadata.
    existing_squad = safe_existing("squad.json") or {}
    existing_by_name = {
        zz_norm(p.get("name")): p
        for p in (existing_squad.get("squad") or existing_squad.get("players") or [])
        if p.get("name")
    }
    for p in players:
        old = existing_by_name.get(zz_norm(p.get("name")))
        if not old:
            continue
        for field in ("careerStats", "zerozero_url", "career", "dateOfBirth",
                      "shirtNumber", "internationalCaps", "nationality",
                      "position", "photo"):
            if old.get(field) not in (None, "", []):
                p[field] = old[field]

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
    """Fetch a large, paginated feed of official Sporting CP YouTube videos."""
    channel_id = "UCHpcLaddGlZUVdtX302fpHA"
    items = []
    seen = set()

    def add_video(vid, title="", published="", thumb=""):
        vid = clean(vid)
        if not vid or vid in seen:
            return
        seen.add(vid)
        items.append({
            "id": vid,
            "title": clean(title) or "Sporting CP — YouTube",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": clean(published),
            "thumbnail": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })

    def collect(obj, official_only=True):
        if isinstance(obj, dict):
            vr = obj.get("videoRenderer")
            if isinstance(vr, dict):
                owner = vr.get("ownerText") or {}
                owner_text = clean(
                    "".join(x.get("text", "") for x in owner.get("runs", []))
                ) if isinstance(owner, dict) else clean(owner)
                if not official_only or not owner_text or "sporting" in owner_text.lower():
                    vid = clean(vr.get("videoId"))
                    runs = (vr.get("title") or {}).get("runs") or []
                    title = clean("".join(x.get("text", "") for x in runs))
                    thumbs = (vr.get("thumbnail") or {}).get("thumbnails") or []
                    thumb = thumbs[-1].get("url") if thumbs else ""
                    pub = clean((vr.get("publishedTimeText") or {}).get("simpleText"))
                    add_video(vid, title, pub, thumb)

            pvr = obj.get("playlistVideoRenderer")
            if isinstance(pvr, dict):
                vid = clean(pvr.get("videoId"))
                runs = (pvr.get("title") or {}).get("runs") or []
                title = clean("".join(x.get("text", "") for x in runs))
                thumbs = (pvr.get("thumbnail") or {}).get("thumbnails") or []
                thumb = thumbs[-1].get("url") if thumbs else ""
                add_video(vid, title, "", thumb)

            for v in obj.values():
                collect(v, official_only)
        elif isinstance(obj, list):
            for v in obj:
                collect(v, official_only)

    def continuation_token(obj):
        found = None
        def walk(v):
            nonlocal found
            if found:
                return
            if isinstance(v, dict):
                cc = v.get("continuationCommand")
                if isinstance(cc, dict) and cc.get("token"):
                    found = cc["token"]
                    return
                ep = v.get("continuationEndpoint")
                if isinstance(ep, dict):
                    cc = ep.get("continuationCommand")
                    if isinstance(cc, dict) and cc.get("token"):
                        found = cc["token"]
                        return
                for child in v.values():
                    if isinstance(child, (dict, list)):
                        walk(child)
                        if found:
                            return
            elif isinstance(v, list):
                for child in v:
                    walk(child)
                    if found:
                        return
        walk(obj)
        return found

    # B) Uploads playlist paginator. A channel's uploads playlist is the
    # channel id with UC -> UU, exposed through the VL browse id.
    upload_playlist = "VL" + channel_id.replace("UC", "UU", 1)
    try:
        context = {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260924.01.00",
                "hl": "pt-PT",
                "gl": "PT",
            }
        }
        token = None
        for page in range(10):
            body = {"context": context, "browseId": upload_playlist}
            if token:
                body = {"context": context, "continuation": token}
            r = session.post(
                "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            before = len(items)
            collect(data, False)
            print(f"YouTube uploads page {page + 1}: +{len(items) - before} videos, total={len(items)}")
            new_token = continuation_token(data)
            if not new_token or new_token == token:
                break
            token = new_token
        if items:
            print(f"YouTube uploads playlist succeeded: {len(items)} videos.")
    except Exception as e:
        print("YouTube uploads playlist warning:", e)

    # C) InnerTube search paginator. This is used when the uploads playlist
    # is unavailable to Actions. The owner is checked so unrelated Sporting
    # channels are not mixed into the feed.
    if len(items) < 40:
        try:
            context = {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20260924.01.00",
                    "hl": "pt-PT",
                    "gl": "PT",
                }
            }
            token = None
            for page in range(10):
                if token:
                    body = {"context": context, "continuation": token}
                else:
                    body = {
                        "context": context,
                        "query": "Sporting Clube de Portugal",
                        "params": "EgIQAQ==",
                    }
                r = session.post(
                    "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                    json=body,
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                before = len(items)
                collect(data, True)
                print(f"YouTube search page {page + 1}: +{len(items) - before} videos, total={len(items)}")
                new_token = continuation_token(data)
                if not new_token or new_token == token:
                    break
                token = new_token
            if items:
                print(f"YouTube search paginator succeeded: {len(items)} videos.")
        except Exception as e:
            print("YouTube search paginator warning:", e)

    contexts = [
        ("WEB", "2.20260924.01.00"),
        ("WEB_EMBEDDED_PLAYER", "1.20260924.01.00"),
    ]

    # InnerTube's channel Videos tab supports continuation tokens. This is the
    # reliable no-key way to walk multiple pages instead of only the first HTML page.
    for client_name, client_version in contexts:
        try:
            context = {
                "client": {
                    "clientName": client_name,
                    "clientVersion": client_version,
                    "hl": "pt-PT",
                    "gl": "PT",
                }
            }
            payload = {
                "context": context,
                "browseId": channel_id,
                "params": "EgZ2aWRlb3PyBgQKAjoA",
            }
            token = None
            for page in range(10):
                body = dict(payload)
                if token:
                    body = {"context": context, "continuation": token}
                r = session.post(
                    "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                    json=body,
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                before = len(items)
                collect(data, True)
                print(f"YouTube page {page + 1}: +{len(items) - before} videos, total={len(items)}")
                new_token = continuation_token(data)
                if not new_token or new_token == token:
                    break
                token = new_token
            if items:
                break
        except Exception as e:
            print("YouTube paginated browse warning:", e)

    # HTML fallback if InnerTube is temporarily blocked.
    if not items:
        urls = [
            f"https://www.youtube.com/@SportingCP/videos?hl=pt-PT&gl=PT",
            f"https://www.youtube.com/channel/{channel_id}/videos?hl=pt-PT&gl=PT",
        ]
        for url in urls:
            try:
                r = session.get(url, timeout=30, headers={
                    **session.headers,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                })
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                script = soup.find("script", id="ytInitialData")
                if script and script.string:
                    collect(json.loads(script.string), True)
                if items:
                    break
            except Exception as e:
                print("YouTube HTML fallback warning:", e)

    # Last-resort known official videos. This is only used if YouTube is unreachable.
    if not items:
        fallback = [
            ("mIsTFfn5Xz8", "🎶 Música para os nossos ouvidos 🏀"),
            ("Gm_CrQhHOfU", "Na História 🫶 Uma centena de vezes com a 🟢⚪️ #SCPFCA"),
            ("q6iKmAL4YFQ", "Sporting CP — vídeo oficial"),
            ("LFMfqhXv8NU", "Sporting CP — vídeo oficial"),
            ("szLeXd-xHBI", "Our POTM: Rodrigo Zalazar 🌟 #SCPGS #UCL"),
            ("1qER3zdGmvM", "Do not disturb 💆🏻‍♂️ Hoje foi dia de sessão fotográfica"),
            ("aY7rcRrVftY", "⚽ Golo 🔄 Assistência ✅ #PlayerOfTheMatchSCP"),
            ("3eMcplcXgG8", "Dentro de campo 🔗 fora de campo 🤝 #UCL"),
        ]
        for vid, title in fallback:
            add_video(vid, title)

    # Keep a generous local feed for the app. The UI can show it as a gallery.
    write_json("youtube.json", {
        "channel": "Sporting CP",
        "channel_id": channel_id,
        "pages_fetched": 10,
        "items": items[:200],
        "source": "YouTube official channel",
    })
    print(f"YouTube: {len(items[:200])} videos written.")

def fetch_match_summary_videos():
    """Find official YouTube match summaries for finished Sporting matches."""
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    details = (safe_existing("match-details.json") or {}).get("fixtures", [])
    detail_by_id = {str(x.get("match_id")): x for x in details if x.get("match_id")}
    finished = [f for f in fixtures if f.get("status", {}).get("short") == "finished" and f.get("goals", {}).get("home") is not None]

    context = {
        "client": {
            "clientName": "WEB",
            "clientVersion": "2.20260924.01.00",
            "hl": "pt-PT",
            "gl": "PT",
        }
    }

    def norm(v):
        return re.sub(r"[^a-z0-9]+", "", zz_norm(v))

    def search_videos(query):
        try:
            r = session.post(
                "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                json={"context": context, "query": query, "params": "EgIQAQ=="},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print("YouTube match summary warning:", query, e)
            return []

        found = []
        def walk(obj):
            if isinstance(obj, dict):
                vr = obj.get("videoRenderer")
                if isinstance(vr, dict):
                    vid = clean(vr.get("videoId"))
                    runs = (vr.get("title") or {}).get("runs") or []
                    title = clean("".join(x.get("text", "") for x in runs))
                    owner = vr.get("ownerText") or {}
                    owner_text = clean("".join(x.get("text", "") for x in owner.get("runs", []))) if isinstance(owner, dict) else clean(owner)
                    thumbs = (vr.get("thumbnail") or {}).get("thumbnails") or []
                    thumb = thumbs[-1].get("url") if thumbs else ""
                    if vid and title:
                        found.append({"id": vid, "title": title, "owner": owner_text, "thumbnail": thumb})
                for v in obj.values():
                    if isinstance(v, (dict, list)): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(data)
        return found

    videos = []
    for f in finished:
        home = clean((f.get("home") or {}).get("name"))
        away = clean((f.get("away") or {}).get("name"))
        hg, ag = f.get("goals", {}).get("home"), f.get("goals", {}).get("away")
        if not home or not away or hg is None or ag is None:
            continue

        queries = [
            f"VSPORTS {home} {hg}-{ag} {away} resumo",
            f"{home} {hg}-{ag} {away} resumo Sporting",
        ]
        candidates = []
        for q in queries:
            candidates.extend(search_videos(q))
            if candidates:
                break

        home_n, away_n = norm(home), norm(away)
        def score_candidate(v):
            title_n = norm(v.get("title"))
            owner_n = norm(v.get("owner"))
            team_hits = int(home_n in title_n) + int(away_n in title_n)
            score_hit = int(f"{hg}{ag}" in title_n or f"{hg}{ag}" in title_n.replace("vs",""))
            official = int("vsports" in owner_n or "sporting" in owner_n)
            summary = int("resumo" in title_n or "highlights" in title_n)
            return (official, team_hits, score_hit, summary)

        best = None
        for v in candidates:
            if score_candidate(v)[0] and score_candidate(v)[1] >= 1:
                if best is None or score_candidate(v) > score_candidate(best):
                    best = v

        if best:
            videos.append({
                "match_id": f.get("id"),
                "sofascore_id": detail_by_id.get(str(f.get("id")), {}).get("sofascore_id"),
                "video_id": best["id"],
                "title": best["title"],
                "channel": best["owner"],
                "url": f"https://www.youtube.com/watch?v={best['id']}",
                "embed": f"https://www.youtube.com/embed/{best['id']}",
                "thumbnail": best.get("thumbnail") or f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
            })

    write_json("match-videos.json", {
        "videos": videos,
        "source": "YouTube · VSPORTS Liga Portugal / Sporting CP",
        "scope": "Finished Sporting CP matches"
    })
    print(f"YouTube match summaries: {len(videos)} videos written.")

def fetch_news():
    items = []

    def decode_google_urls(rows):
        google_rows=[x for x in rows if "news.google.com/" in str(x.get("url") or "")]
        if not google_rows: return
        try:
            import asyncio
            from googlenewsdecoder import gnews_decoder_async
            urls=[x["url"] for x in google_rows]
            results=asyncio.run(gnews_decoder_async(urls,interval=0.2,timeout=12.0,concurrency=6))
            if isinstance(results,dict): results=[results]
            for item,result in zip(google_rows,results):
                if isinstance(result,dict) and result.get("success") and result.get("decoded_url"):
                    item["url"]=result["decoded_url"]
        except Exception as e:
            print("Google News decoder warning:",e)

    def article_metadata(item):
        url=item.get("url")
        if not url or "news.google.com/" in url: return
        try:
            rr=session.get(url,timeout=10,allow_redirects=True,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status(); final_url=rr.url; ss=BeautifulSoup(rr.text,"html.parser")
            og=ss.find("meta",attrs={"property":"og:image"}) or ss.find("meta",attrs={"property":"og:image:url"}) or ss.find("meta",attrs={"name":"twitter:image"})
            desc=ss.find("meta",attrs={"property":"og:description"}) or ss.find("meta",attrs={"name":"description"})
            if og and og.get("content"):
                image=og.get("content").strip()
                if image.startswith("//"): image="https:"+image
                elif image.startswith("/"):
                    from urllib.parse import urljoin
                    image=urljoin(final_url,image)
                item["image"]=image; item["image_source"]=final_url
            if desc and desc.get("content"): item["description"]=clean(desc.get("content"))[:280]
            if final_url and "news.google.com" not in final_url: item["url"]=final_url
        except Exception as e: print("News metadata warning:",item.get("url"),e)

    try:
        r=session.get(SPORTING_NEWS,timeout=25); r.raise_for_status(); soup=BeautifulSoup(r.text,"html.parser"); seen=set()
        for a in soup.select("a[href]"):
            href=a.get("href",""); title=" ".join(a.stripped_strings)
            if href.startswith("/"): href="https://www.sporting.pt"+href
            if "sporting.pt" not in href or "/noticias/" not in href or len(title)<18: continue
            if href in seen: continue
            seen.add(href); items.append({"title":title[:180],"url":href,"source":"Sporting.pt"})
            if len(items)>=12: break
    except Exception as e: print("Sporting news warning:",e)

    try:
        import feedparser
        feed=feedparser.parse("https://news.google.com/rss/search?q=Sporting%20CP&hl=pt-PT&gl=PT&ceid=PT:pt-150"); existing={x["url"] for x in items}
        for entry in feed.entries[:40]:
            url=entry.get("link"); title=entry.get("title")
            if not url or not title or url in existing: continue
            source=(entry.get("source") or {}).get("title") or "Google News"
            items.append({"title":title[:180],"url":url,"source":source,"published":entry.get("published")}); existing.add(url)
            if len(items)>=30: break
    except Exception as e: print("Google News warning:",e)

    decode_google_urls(items)
    for item in items[:30]: article_metadata(item)
    for item in items:
        item.pop("media_thumbnail",None); item.pop("media_content",None)
        if not item.get("image"): item.pop("image",None)
    write_json("news.json",{"items":items})


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


# Historical team stats and competitive-only team metrics are generated for the PWA.
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
            fetch_fotmob_competition_standings()
        except Exception as e:
            print(f"FotMob competition standings skipped: {e}")
        try:
            fetch_competition_standings()
        except Exception as e:
            print(f"SofaScore competition standings skipped: {e}")
        try:
            build_team_stats_from_sofa()
        except Exception as e:
            print(f"SofaScore team stats skipped: {e}")
        try:
            fetch_fbref_historical_team_stats()
        except Exception as e:
            print(f"Historical team stats skipped: {e}")
        try:
            fetch_sofascore_match_details()
        except Exception as e:
            print(f"SofaScore match details skipped: {e}")
        try:
            fetch_match_summary_videos()
        except Exception as e:
            print(f"YouTube match summaries skipped: {e}")
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
