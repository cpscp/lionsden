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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                href = "https://zerozero.dk" + href
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
        time.sleep(0.5)

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
    url = "https://api.sofascore.com/api/v1" + path
    r = session.get(url, params=params or {}, timeout=10,
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
            "custom_id": clean(e.get("customId")),
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

    # GitHub Actions may receive 403 from SofaScore. The fixture feed itself
    # comes from FotMob, whose matchDetails endpoint exposes the same high-value
    # data (lineups, ratings, events and stats), so use it as a transparent fallback.
    if not details:
        print("SofaScore unavailable; falling back to FotMob matchDetails.")
        for f in sorted(finished, key=lambda x: x.get("date") or 0):
            try:
                raw = fotmob_get("/api/data/matchDetails", {"matchId": f.get("id")})
                content = raw.get("content") or {}
                facts = content.get("matchFacts") or {}
                info = facts.get("infoBox") or {}
                lineup_raw = content.get("lineup") or {}
                teams = lineup_raw.get("lineups") or lineup_raw.get("lineup") or []
                if not teams:
                    for key in ("homeTeam", "awayTeam"):
                        if isinstance(lineup_raw.get(key), dict):
                            teams.append(lineup_raw[key])

                home_id = int((f.get("home") or {}).get("id") or 0)
                def flatten_players(value):
                    out = []
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, list):
                                out.extend(flatten_players(item))
                            elif isinstance(item, dict):
                                out.append(item)
                    return out

                def norm_player(p, substitute=False):
                    performance = p.get("performance") if isinstance(p.get("performance"), dict) else {}
                    rating = performance.get("rating")
                    if rating is None:
                        rating = p.get("rating")
                    return {
                        "player": {
                            "name": clean(p.get("name")),
                            "shortName": clean(p.get("shortName") or p.get("name")),
                            "id": p.get("id")
                        },
                        "shirtNumber": p.get("shirtNumber") or p.get("shirt") or p.get("jerseyNumber"),
                        "position": clean(p.get("positionStringShort") or p.get("position") or p.get("role") or ""),
                        "substitute": substitute,
                        "horizontalLayout": p.get("horizontalLayout"),
                        "statistics": {"rating": rating}
                    }

                def norm_team(t):
                    starters = flatten_players(t.get("starters") or t.get("players") or [])
                    bench = flatten_players(t.get("subs") or t.get("bench") or [])
                    coach = t.get("coach") or []
                    coach_name = ""
                    if isinstance(coach, list) and coach and isinstance(coach[0], dict):
                        coach_name = clean(coach[0].get("name"))
                    elif isinstance(coach, dict):
                        coach_name = clean(coach.get("name"))
                    return {
                        "id": t.get("teamId") or t.get("id"),
                        "name": clean(t.get("teamName") or t.get("name")),
                        "formation": clean(t.get("formation") or t.get("lineup")),
                        "coach": coach_name,
                        "players": [norm_player(p, False) for p in starters if clean(p.get("name"))] + [norm_player(p, True) for p in bench if clean(p.get("name"))]
                    }

                home_lu = None
                away_lu = None
                for t in teams:
                    nt = norm_team(t)
                    tid = int(nt.get("id") or 0)
                    if tid == home_id:
                        home_lu = nt
                    elif not away_lu:
                        away_lu = nt
                if home_lu is None and teams:
                    home_lu = norm_team(teams[0])
                if away_lu is None and len(teams) > 1:
                    away_lu = norm_team(teams[1])

                raw_events = ((facts.get("events") or {}).get("events") or [])
                incidents_norm = []
                def extract_card_type(e):
                    candidates = [
                        e.get("cardType"), e.get("card"), e.get("cardTypeName"),
                        e.get("cardDescription"), e.get("bookingType")
                    ]
                    for c in candidates:
                        if isinstance(c, dict):
                            c = c.get("type") or c.get("name") or c.get("text") or c.get("description")
                        c = clean(c)
                        if c:
                            n = zz_norm(c)
                            if "second" in n and "yellow" in n: return "second_yellow"
                            if "red" in n: return "red"
                            if "yellow" in n: return "yellow"
                    return ""
                for e in raw_events:
                    et = clean(e.get("type")).lower()
                    if et in {"goal", "card", "substitution"}:
                        incidents_norm.append({
                            "incidentType": et,
                            "time": e.get("time"),
                            "player": e.get("player") or {},
                            "isHome": e.get("isHome"),
                            "description": clean(e.get("nameStr") or e.get("type")),
                            "swap": e.get("swap") or [],
                            "cardType": extract_card_type(e) if et == "card" else ""
                        })

                stats_all = (((content.get("stats") or {}).get("Periods") or {}).get("All") or {})
                stats_items = []
                for group in stats_all.get("stats") or []:
                    for item in group.get("stats") or []:
                        if isinstance(item, dict):
                            vals = item.get("stats") or [None, None]
                            stats_items.append({
                                "name": clean(item.get("title")),
                                "home": vals[0] if len(vals) > 0 else None,
                                "away": vals[1] if len(vals) > 1 else None
                            })

                referee = info.get("Referee")
                if isinstance(referee, dict):
                    referee = referee.get("text") or referee.get("name")
                stadium = info.get("Stadium")
                if isinstance(stadium, dict):
                    stadium_name = stadium.get("name")
                    stadium_city = stadium.get("city")
                else:
                    stadium_name, stadium_city = stadium, ""

                event_header = raw.get("header") or {}
                score_parts = clean((event_header.get("status") or {}).get("scoreStr")).split("-")
                event_obj = {
                    "referee": {"name": clean(referee)} if referee else None,
                    "attendance": info.get("Attendance"),
                    "venue": {"name": clean(stadium_name), "city": clean(stadium_city)}
                }
                if len(score_parts) == 2:
                    event_obj["homeScore"] = {"current": score_parts[0].strip()}
                    event_obj["awayScore"] = {"current": score_parts[1].strip()}

                details.append({
                    "match_id": f.get("id"),
                    "source_match_id": f.get("id"),
                    "source": "FotMob",
                    "event": event_obj,
                    "lineups": {"home": home_lu or {"players": []}, "away": away_lu or {"players": []}},
                    "incidents": {"incidents": incidents_norm},
                    "statistics": {"statistics": [{"period": "ALL", "groups": [{"groupName": "FotMob", "statisticsItems": stats_items}]}]},
                    "managers": {
                        "homeManager": {"name": clean((home_lu or {}).get("coach") or "")},
                        "awayManager": {"name": clean((away_lu or {}).get("coach") or "")}
                    }
                })
            except Exception as e:
                print("FotMob match detail fallback warning:", f.get("id"), e)

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
            "published_text": clean(published),
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
    """Find official YouTube match summaries; prefer Sporting CP for home games."""
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
                        found.append({"id": vid, "title": title, "owner": owner_text, "thumbnail": thumb, "published_text": clean((vr.get("publishedTimeText") or {}).get("simpleText"))})
                for v in obj.values():
                    if isinstance(v, (dict, list)): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(data)
        return found

    from datetime import datetime, timezone, timedelta
    def published_date(text):
        t=zz_norm(text or "")
        now=datetime.now(timezone.utc)
        m=re.search(r"(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years|segundo|segundos|minuto|minutos|hora|horas|dia|dias|semana|semanas|mes|meses|ano|anos)",t)
        if m:
            n=int(m.group(1)); unit=m.group(2)
            days=n/24 if ("hour" in unit or "hora" in unit) else n/1440 if ("minute" in unit or "minuto" in unit) else n/86400 if ("second" in unit or "segundo" in unit) else n if ("day" in unit or "dia" in unit) else n*7 if ("week" in unit or "semana" in unit) else n*30 if ("month" in unit or "mes" in unit) else n*365
            return now-timedelta(days=days)
        for fmt in ("%b %d, %Y","%d %b %Y","%b %d %Y"):
            try:return datetime.strptime(text.strip(),fmt).replace(tzinfo=timezone.utc)
            except Exception:pass
        return None

    previous = safe_existing("match-videos.json") or {}
    previous_by_match = {str(x.get("match_id")): x for x in previous.get("videos", []) if x.get("match_id") and x.get("video_id")}
    videos = []
    for f in finished:
        home = clean((f.get("home") or {}).get("name"))
        away = clean((f.get("away") or {}).get("name"))
        hg, ag = f.get("goals", {}).get("home"), f.get("goals", {}).get("away")
        if not home or not away or hg is None or ag is None:
            continue

        is_sporting_home = norm(home) == norm("Sporting CP")
        queries = []
        if is_sporting_home:
            queries = [
                f"Sporting CP {hg}-{ag} {away} resumo Sporting",
                f"Sporting {hg}-{ag} {away} resumo",
                f"VSPORTS Sporting CP {hg}-{ag} {away} resumo",
            ]
        else:
            queries = [
                f"VSPORTS {home} {hg}-{ag} Sporting CP resumo",
                f"{home} {hg}-{ag} Sporting CP resumo",
                f"Sporting CP {hg}-{ag} {away} resumo",
            ]

        # Preserve an explicitly configured official VSPORTS player. YouTube may
        # be searchable, but rights holders can block YouTube embeds; a direct
        # VSPORTS player is therefore authoritative when already configured.
        previous_match = previous_by_match.get(str(f.get("id")))
        if previous_match and previous_match.get("provider") == "vsports" and previous_match.get("embed"):
            previous_match = dict(previous_match)
            previous_match["embed_allowed"] = False
            videos.append(previous_match)
            continue

        candidates = []
        for q in queries:
            batch = search_videos(q)
            candidates.extend(batch)
            if batch:
                break

        home_n, away_n = norm(home), norm(away)
        def score_candidate(v):
            title_n = norm(v.get("title"))
            owner_n = norm(v.get("owner"))
            # Only official sources are eligible for match summaries.
            # Home matches: Sporting CP is the preferred source.
            # Away matches: VSPORTS - Liga Portugal is preferred, with Sporting CP
            # as an official fallback when VSPORTS has no suitable summary.
            is_sporting_source = "sporting clube de portugal" in owner_n or owner_n == "sporting cp"
            is_vsports_source = "vsports" in owner_n and "liga portugal" in owner_n
            if is_sporting_home:
                if not is_sporting_source:
                    return (-1, -1, -1, -1, -1, -1)
            else:
                if not (is_vsports_source or is_sporting_source):
                    return (-1, -1, -1, -1, -1, -1)
            team_hits = int(home_n in title_n) + int(away_n in title_n)
            score_hit = int(f"{hg}{ag}" in title_n or f"{hg} - {ag}" in title_n)
            sporting = int(is_sporting_source)
            vsports = int(is_vsports_source)
            summary = int("resumo" in title_n or "highlights" in title_n)
            # Never accept gaming/simulation content even if the title contains the
            # exact teams and score.
            forbidden = any(x in title_n for x in ("simulacao", "simulação", "efootball", "pes 21", "pes2021", "gaming"))
            if forbidden:
                return (-1, -1, -1, -1, -1, -1)
            match_dt=None
            try: match_dt=datetime.fromtimestamp(float(f.get("date") or 0),tz=timezone.utc)
            except Exception: pass
            pub_dt=published_date(v.get("published_text"))
            if match_dt and pub_dt:
                delta=abs((pub_dt-match_dt).total_seconds())/86400
                freshness=int(delta<=21)
                proximity=max(0,21-delta)
            else:
                freshness=0; proximity=0
            source_priority = sporting * 3 if is_sporting_home else vsports * 3
            return (freshness, source_priority, team_hits, score_hit, summary, proximity)

        best = None
        for v in candidates:
            if score_candidate(v)[0] and score_candidate(v)[2] >= 1 and score_candidate(v)[4]:
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
                "thumbnail": best.get("thumbnail") or f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg",
                "embed_allowed": not (
                    "sporting clube de portugal" in norm(best.get("owner"))
                    or norm(best.get("owner")) == "sporting cp"
                    or ("vsports" in norm(best.get("owner")) and "liga portugal" in norm(best.get("owner")))
                ),
                "preferred_for_home": bool(is_sporting_home and "sporting" in norm(best.get("owner"))),
                "published_text": best.get("published_text","")
            })
        elif str(f.get("id")) in previous_by_match:
            # Never erase a previously valid official summary just because YouTube search
            # temporarily returned no candidates or changed its published-time label.
            videos.append(previous_by_match[str(f.get("id"))])

    write_json("match-videos.json", {
        "videos": videos,
        "source": "YouTube · Sporting CP / VSPORTS Liga Portugal",
        "scope": "Finished Sporting CP matches",
        "policy": "Sporting CP channel preferred for home matches; VSPORTS preferred for away matches; only summaries published within 21 days of the match are eligible."
    })
    print(f"YouTube match summaries: {len(videos)} videos written.")

def fetch_news():
    """Build a fast, football-first Sporting news feed.

    Priority:
      1) Sporting football news from Record, A Bola, O Jogo and Zerozero
      2) Sporting.pt football
      3) other Sporting news from Google News
    Source-specific Google RSS queries are intentional: a generic Sporting
    query is too broad and under-represents the Portuguese football press.
    """
    import asyncio
    import re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import quote

    items = []
    seen = set()

    source_priority = {
        "Record": 100,
        "A Bola": 98,
        "O Jogo": 96,
        "Zerozero": 94,
        "Sporting.pt": 92,
    }
    football_terms = (
        "futebol", "sporting", "rui borges", "jogador", "jogadores",
        "treino", "alvalade", "liga", "champions", "champions league",
        "taça", "uefa", "mercado", "transferência", "contratação",
        "convocados", "lesão", "onze", "jogo", "golo", "defesa",
        "avançado", "médio", "guarda-redes", "futebolista"
    )
    non_football_penalty = (
        "futsal", "andebol", "hóquei", "voleibol", "basquetebol",
        "atletismo", "natação", "modalidades"
    )

    def source_from_url(url, fallback="Google News"):
        u = str(url or "").lower()
        if "record.pt" in u: return "Record"
        if "abola.pt" in u: return "A Bola"
        if "ojogo.pt" in u: return "O Jogo"
        if "zerozero.pt" in u: return "Zerozero"
        if "sporting.pt" in u: return "Sporting.pt"
        return fallback

    def add(item):
        url = item.get("url")
        title = clean(item.get("title") or "")
        if not url or not title or len(title) < 18:
            return
        # Prefer decoded/canonical article URLs when available.
        key = re.sub(r"[?#].*$", "", str(url).rstrip("/")).lower()
        if key in seen:
            return
        item["title"] = title[:180]
        item["source"] = source_from_url(url, item.get("source") or "Google News")
        item["_priority"] = source_priority.get(item["source"], 50)
        blob = title.lower()
        football = any(t in blob for t in football_terms)
        modalities = any(t in blob for t in non_football_penalty)
        item["_football"] = football and not (modalities and "futebol" not in blob)
        excluded_sporting = (
            "sporting kansas city", "sporting kc", "sporting seis de diciembre",
            "sporting de gijón", "sporting gijon"
        )
        item["_sporting"] = any(t in blob for t in (
            "sporting", "alvalade", "rui borges", "leões", "leoes", "leoas", "leonino",
            "verde e branco", "verde-e-branco"
        )) and not any(t in blob for t in excluded_sporting)
        item["_priority"] += 25 if item["_football"] else 0
        item["_priority"] += 40 if item["_sporting"] else 0
        seen.add(key)
        items.append(item)

    def decode_google_urls(rows):
        google_rows=[x for x in rows if "news.google.com/" in str(x.get("url") or "")]
        if not google_rows:
            return
        try:
            from googlenewsdecoder import gnews_decoder_async
            urls=[x["url"] for x in google_rows]
            results=asyncio.run(gnews_decoder_async(
                urls, interval=0.15, timeout=10.0, concurrency=8
            ))
            if isinstance(results,dict):
                results=[results]
            for item,result in zip(google_rows,results):
                if isinstance(result,dict) and result.get("success") and result.get("decoded_url"):
                    item["url"]=result["decoded_url"]
                    item["source"]=source_from_url(item["url"], item.get("source"))
        except Exception as e:
            print("Google News decoder warning:",e)

    def article_metadata(item):
        url=item.get("url")
        if not url:
            return item
        # Zerozero often blocks article HTML from GitHub Actions. Never use
        # Jina/Cloudflare placeholder images. If no editorial image is available
        # from the article metadata, recover the image through Google Images using
        # the exact article title; the article/source shown to users remains Zerozero.
        if item.get("source")=="Zerozero":
            item.pop("image",None)
            item.pop("image_source",None)
            try:
                from urllib.parse import quote as _quote
                proxy="https://translate.google.com/translate?sl=pt&tl=en&u="+_quote(url,safe="")
                zr=session.get(proxy,timeout=20,headers={"User-Agent":USER_AGENT})
                if zr.ok:
                    zs=BeautifulSoup(zr.text,"html.parser")
                    for tag, attrs in [
                        ("meta",{"property":"og:image"}),
                        ("meta",{"property":"og:image:url"}),
                        ("meta",{"name":"twitter:image"}),
                    ]:
                        node=zs.find(tag,attrs=attrs)
                        if node and node.get("content"):
                            src=node.get("content").strip()
                            if "zerozero" in src.lower():
                                item["image"]=src
                                item["image_source"]="Zerozero article via Google Translate"
                                break
                    if not item.get("description"):
                        d=zs.find("meta",attrs={"property":"og:description"}) or zs.find("meta",attrs={"name":"description"})
                        if d and d.get("content"):
                            item["description"]=clean(d.get("content"))[:280]
            except Exception as ex:
                print("Zerozero Translate image warning:",item.get("url"),ex)
            if not item.get("image"):
                try:
                    from urllib.parse import quote as _q
                    mr=session.get("https://api.microlink.io/?url="+_q(url,safe="")+"&meta=true",
                                   timeout=20,headers={"User-Agent":USER_AGENT})
                    if mr.ok:
                        md=mr.json().get("data",{})
                        image=((md.get("image") or {}).get("url") if isinstance(md.get("image"),dict) else md.get("image"))
                        desc=md.get("description")
                        if image and not re.search(r"(google|gstatic|cloudflare|captcha|favicon|logo)",str(image),re.I):
                            item["image"]=image
                            item["image_source"]="Zerozero article metadata"
                        if desc and not re.search(r"(just a moment|captcha|cloudflare)",str(desc),re.I):
                            item["description"]=clean(desc)[:280]
                except Exception as ex:
                    print("Zerozero metadata service warning:",item.get("url"),ex)
            try:
                q=quote((item.get("title") or "")+" site:zerozero.pt")
                gr=session.get("https://www.google.com/search?tbm=isch&q="+q,
                               timeout=15,headers={"User-Agent":USER_AGENT})
                if gr.ok:
                    gs=BeautifulSoup(gr.text,"html.parser")
                    for im in gs.find_all("img"):
                        src=(im.get("data-iurl") or im.get("data-original") or
                             im.get("data-src") or im.get("src"))
                        if not src or str(src).startswith("data:"):
                            continue
                        low=str(src).lower()
                        if any(x in low for x in ("google","gstatic","favicon","logo")):
                            continue
                        if im.get("width") and int(im.get("width")) < 200:
                            continue
                        item["image"]=src
                        item["image_source"]="Zerozero article image search"
                        break
            except Exception as ex:
                print("Zerozero Google image search warning:",item.get("url"),ex)
            if not item.get("image"):
                try:
                    bq=quote((item.get("title") or "").replace(" - zerozero.pt","")+" zerozero")
                    br=session.get("https://www.bing.com/images/search?q="+bq,
                                   timeout=15,headers={"User-Agent":USER_AGENT})
                    if br.ok:
                        bs=BeautifulSoup(br.text,"html.parser")
                        for node in bs.select("a.iusc"):
                            raw=node.get("m")
                            if not raw:
                                continue
                            try:
                                meta=json.loads(raw)
                            except Exception:
                                continue
                            src=meta.get("murl")
                            origin=meta.get("purl") or ""
                            if not src or "zerozero.pt/noticias/" not in origin:
                                continue
                            low=str(src).lower()
                            if any(x in low for x in ("bing.com","microsoft.com","favicon","logo","ytimg.com","youtube.com","pngimg.com")):
                                continue
                            item["image"]=src
                            item["image_source"]="Zerozero article image search"
                            break
                except Exception as ex:
                    print("Zerozero Bing image search warning:",item.get("url"),ex)

        try:
            rr=session.get(url,timeout=12,allow_redirects=True,
                            headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            final_url=rr.url
            ss=BeautifulSoup(rr.text,"html.parser")
            item["source"]=source_from_url(final_url,item.get("source"))
            # Try the common image metadata used by O Jogo/Zerozero.
            image = None
            for tag, attrs in [
                ("meta", {"property":"og:image"}),
                ("meta", {"property":"og:image:url"}),
                ("meta", {"name":"og:image"}),
                ("meta", {"name":"twitter:image"}),
                ("meta", {"name":"twitter:image:src"}),
                ("meta", {"itemprop":"image"}),
            ]:
                node=ss.find(tag,attrs=attrs)
                if node and node.get("content"):
                    image=node.get("content").strip()
                    break
            if not image:
                # Some pages expose the hero image through JSON-LD.
                import json as _json
                for script in ss.find_all("script",attrs={"type":"application/ld+json"}):
                    try:
                        data=_json.loads(script.string or script.get_text())
                        candidates=data if isinstance(data,list) else [data]
                        for obj in candidates:
                            if not isinstance(obj,dict):
                                continue
                            val=obj.get("image")
                            if isinstance(val,str): image=val
                            elif isinstance(val,dict): image=val.get("url")
                            elif isinstance(val,list) and val: image=val[0]
                            if image: break
                        if image: break
                    except Exception:
                        pass
            if not image:
                for img in ss.select("article img, main img, img"):
                    image=img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                    if image and not str(image).startswith("data:"):
                        break
            if image and (item.get("source") != "Zerozero" or "zerozero.pt" in str(image).lower()):
                image=str(image).strip()
                if image.startswith("//"): image="https:"+image
                elif image.startswith("/"):
                    from urllib.parse import urljoin
                    image=urljoin(final_url,image)
                item["image"]=image
                item["image_source"]=final_url
            # Never allow blocked/placeholder Zerozero metadata to replace
            # an image already recovered from the editorial image search.
            if item.get("source")=="Zerozero" and item.get("image_source")=="Jina Reader":
                item.pop("image",None)
                item.pop("image_source",None)
            # Always recover the publication timestamp from the article,
            # because listing feeds from some sources omit it.
            published = None
            for tag, attrs in [
                ("meta", {"property":"article:published_time"}),
                ("meta", {"name":"article:published_time"}),
                ("meta", {"property":"og:published_time"}),
            ]:
                node=ss.find(tag,attrs=attrs)
                if node and node.get("content"):
                    published=node.get("content").strip()
                    break
            if not published:
                time_node=ss.find("time")
                if time_node:
                    published=time_node.get("datetime") or time_node.get_text(" ",strip=True)
            if not published:
                import json as _json
                for script in ss.find_all("script",attrs={"type":"application/ld+json"}):
                    try:
                        data=_json.loads(script.string or script.get_text())
                        candidates=data if isinstance(data,list) else [data]
                        for obj in candidates:
                            if isinstance(obj,dict) and obj.get("datePublished"):
                                published=obj["datePublished"]
                                break
                        if published: break
                    except Exception:
                        pass
            if published:
                item["published"]=published
            desc = (ss.find("meta",attrs={"property":"og:description"}) or ss.find("meta",attrs={"name":"description"}) or ss.find("meta",attrs={"name":"twitter:description"}))
            if desc and desc.get("content"):
                item["description"]=clean(desc.get("content"))[:280]
            if final_url and "news.google.com" not in final_url:
                item["url"]=final_url
        except Exception as e:
            try:
                jina="https://r.jina.ai/"+url
                jr=session.get(jina,timeout=15,headers={"User-Agent":USER_AGENT})
                jr.raise_for_status()
                js=BeautifulSoup(jr.text,"html.parser")
                if not item.get("description"):
                    text_blob=js.get_text(" ",strip=True)
                    if text_blob:
                        item["description"]=clean(text_blob)[:280]
                for img in js.select("img"):
                    src=img.get("src")
                    if src and not src.startswith("data:"):
                        item["image"]=src
                        item["image_source"]=jina
                        break
            except Exception as e2:
                print("News metadata warning:",item.get("url"),e2)
        return item

    def scrape_page(url, source):
        """Scrape a publisher listing with direct + Jina fallback."""
        candidates = [url, "https://r.jina.ai/" + url]
        for candidate in candidates:
            try:
                r=session.get(candidate,timeout=15,headers={"User-Agent":USER_AGENT})
                r.raise_for_status()
                soup=BeautifulSoup(r.text,"html.parser")
                local=[]
                for a in soup.select("a[href]"):
                    href=a.get("href","")
                    title=" ".join(a.stripped_strings)
                    if href.startswith("/"):
                        from urllib.parse import urljoin
                        href=urljoin(url,href)
                    # Jina may expose canonical absolute links.
                    if source_from_url(href) != source or len(title)<18:
                        continue
                    # O Jogo's "Últimas" page is broad; keep only articles
                    # explicitly tied to Sporting CP so other clubs never leak.
                    if source == "O Jogo" and "sporting" not in title.lower():
                        continue
                    # sporting.pt contains corporate, academy, membership,
                    # foundation and other institutional content. Keep it only
                    # when the item is clearly an editorial Sporting CP story.
                    if source == "Sporting.pt":
                        low_title = title.lower()
                        low_href = href.lower()
                        blocked_terms = (
                            "corporate", "sporting corporate", "fundação",
                            "fundacao", "foundation", "business", "parceiros",
                            "parceiro", "membership", "bilhetes", "ticketing",
                            "academia", "formação", "formacao", "e-learning",
                            "sustentabilidade", "responsabilidade social",
                            "sporting solidário", "sporting solidario"
                        )
                        editorial_paths = ("/noticias/",)
                        if any(term in low_title for term in blocked_terms):
                            continue
                        if not any(path in low_href for path in editorial_paths):
                            continue
                    if any(x in href.lower() for x in ("/video", "/videos", "/fotogaleria", "/multimedia")):
                        continue
                    image = None
                    parent = a
                    for _ in range(6):
                        if parent is None: break
                        img = parent.find("img")
                        if img:
                            image = (img.get("data-src") or img.get("data-lazy-src") or
                                     img.get("data-original") or img.get("src"))
                            if image and not str(image).startswith("data:"):
                                break
                            image = None
                        parent = parent.parent
                    if image:
                        from urllib.parse import urljoin
                        image=urljoin(url,str(image))
                    preview = None
                    for attr in ("data-description","data-summary","data-excerpt","aria-label"):
                        val=a.get(attr)
                        if val and len(str(val)) > 20:
                            preview=clean(val)
                            break
                    local.append({"title":title,"url":href,"source":source,
                                  **({"image":image,"image_source":url} if image else {}),
                                  **({"description":preview} if preview else {})})
                    if len(local)>=18:
                        break
                if not local and candidate.startswith("https://r.jina.ai/"):
                    # Jina Reader may return Markdown rather than HTML.
                    for m in re.finditer(r"\[([^\]]{18,180})\]\((https?://[^)]+)\)", r.text):
                        title=clean(m.group(1))
                        href=m.group(2)
                        if source_from_url(href) != source or len(title)<18:
                            continue
                        if source == "O Jogo" and "sporting" not in title.lower():
                            continue
                        local.append({"title":title,"url":href,"source":source})
                        if len(local)>=18:
                            break
                if local:
                    return local
            except Exception as e:
                print(f"{source} news warning ({candidate}):",e)
        return []


    # Direct source pages. These are deliberately football-specific where the
    # publisher exposes a Sporting football section.
    direct_sources = [
        ("https://www.record.pt/futebol/futebol-nacional/liga-betclic/sporting", "Record"),
        ("https://www.abola.pt/futebol/sporting-448", "A Bola"),
        ("https://www.zerozero.pt/equipa/sporting/noticias", "Zerozero"),
        ("https://www.ojogo.pt/futebol/1a-liga/sporting/", "O Jogo"),
        (SPORTING_NEWS, "Sporting.pt"),
    ]
    # Zerozero publishes an official RSS feed. It is much lighter and more
    # reliable from Actions than scraping the site.
    try:
        import feedparser
        feed=feedparser.parse("https://www.zerozero.pt/rss_list.php?equipa=9")
        for entry in feed.entries[:80]:
            title=clean(entry.get("title") or "")
            link=entry.get("link")
            summary=clean(entry.get("summary") or "")
            blob=(title+" "+summary).lower()
            if not link or "sporting" not in blob:
                continue
            image=None
            media=entry.get("media_content") or entry.get("media_thumbnail") or []
            if media and isinstance(media,list):
                image=media[0].get("url")
            if not image:
                image=(entry.get("image") or {}).get("href") if isinstance(entry.get("image"),dict) else None
            add({
                "title":title,
                "url":link,
                "source":"Zerozero",
                "published":entry.get("published"),
                **({"image":image,"image_source":"https://www.zerozero.pt/rss.php"} if image else {}),
            })
    except Exception as e:
        print("Zerozero RSS warning:",e)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(scrape_page,u,s) for u,s in direct_sources]
        for future in as_completed(futures):
            for item in future.result():
                add(item)

    # Publisher discovery fallback via Bing News RSS. Bing is only used as
    # a transport/discovery layer; the stored source is always the publisher
    # URL (A Bola, O Jogo or Zerozero), never Bing.
    try:
        import feedparser
        from urllib.parse import quote
        publisher_queries = [
            ("abola.pt", "A Bola"),
            ("ojogo.pt", "O Jogo"),
            ("zerozero.pt", "Zerozero"),
        ]
        for domain, expected_source in publisher_queries:
            rss_url = (
                "https://www.bing.com/news/search?q=" +
                quote("site:" + domain + " Sporting") +
                "&format=rss&setlang=pt-PT"
            )
            try:
                feed=feedparser.parse(rss_url)
                for entry in feed.entries[:40]:
                    title=clean(entry.get("title") or "")
                    link=entry.get("link")
                    summary=clean(entry.get("summary") or "")
                    if not link or len(title)<18:
                        continue
                    if "sporting" not in (title+" "+summary).lower():
                        continue
                    source=source_from_url(link, expected_source)
                    if source not in ("A Bola","O Jogo","Zerozero"):
                        continue
                    image=None
                    media=entry.get("media_content") or entry.get("media_thumbnail") or []
                    if media and isinstance(media,list):
                        image=media[0].get("url")
                    add({
                        "title":title,
                        "url":link,
                        "source":source,
                        "published":entry.get("published") or entry.get("pubDate"),
                        "description":summary[:280] if summary else None,
                        **({"image":image,"image_source":"Bing News RSS"} if image else {}),
                    })
            except Exception as e:
                print("Publisher RSS discovery warning:", expected_source, e)

    except Exception as e:
        print("Publisher RSS import warning:", e)

    # Fallback discovery for publishers that block their listing pages.
    try:
        from urllib.parse import quote
        for domain, expected_source in (("ojogo.pt","O Jogo"),("zerozero.pt","Zerozero")):
            qurl="https://html.duckduckgo.com/html/?q="+quote("site:"+domain+" Sporting")
            rr=session.get(qurl,timeout=20,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            soup=BeautifulSoup(rr.text,"html.parser")
            for a in soup.select("a.result__a")[:30]:
                href=a.get("href","")
                title=clean(a.get_text(" ",strip=True))
                if href and title and domain in href.lower() and "sporting" in title.lower():
                    add({"title":title,"url":href,"source":expected_source})
    except Exception as e:
        print("Web discovery warning:",e)

    # Search discovery fallback for publishers that block GitHub Actions.
    # Search is only transport; the stored URL/source is the original publisher.
    try:
        from urllib.parse import quote
        for domain, expected_source in (("ojogo.pt","O Jogo"),("zerozero.pt","Zerozero")):
            qurl="https://www.google.com/search?q="+quote("site:"+domain+" Sporting")+"&num=20&hl=pt-PT"
            rr=session.get(qurl,timeout=20,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            soup=BeautifulSoup(rr.text,"html.parser")
            for a in soup.select("a[href]"):
                href=a.get("href","")
                title=clean(a.get_text(" ",strip=True))
                if href.startswith("/url?q="):
                    href=href.split("/url?q=",1)[1].split("&",1)[0]
                if href.startswith("http") and domain in href.lower() and "sporting" in title.lower():
                    add({"title":title,"url":href,"source":expected_source})
    except Exception as e:
        print("Search discovery warning:",e)

    # Jina-backed search discovery for publishers blocking GitHub Actions.
    try:
        from urllib.parse import quote
        for domain, expected_source in (("ojogo.pt","O Jogo"),("zerozero.pt","Zerozero")):
            q="site:"+domain+" Sporting"
            jurl="https://r.jina.ai/http://www.google.com/search?q="+quote(q)
            rr=session.get(jurl,timeout=25,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            text_body=rr.text
            for m in re.finditer(r"\[([^\]]{18,180})\]\((https?://[^)]+)\)",text_body):
                title=clean(m.group(1))
                href=m.group(2)
                if domain in href.lower() and "sporting" in title.lower():
                    add({"title":title,"url":href,"source":expected_source})
    except Exception as e:
        print("Jina search discovery warning:",e)

    # Publisher-specific Sporting sections: these are much more complete
    # than generic "latest" pages and should be the primary discovery route.
    publisher_sections = [
        ("https://www.abola.pt/futebol/sporting-448", "A Bola"),
        ("https://www.record.pt/futebol/futebol-nacional/liga-betclic/sporting", "Record"),
        ("https://www.zerozero.pt/noticias?keyword=117&order=recent-desc&redird=1", "Zerozero"),
    ]
    for section_url, source in publisher_sections:
        try:
            rr=session.get(section_url,timeout=20,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            ss=BeautifulSoup(rr.text,"html.parser")
            for a in ss.select("a[href]"):
                href=urljoin(section_url,a.get("href",""))
                title=clean(a.get_text(" ",strip=True))
                if not href or len(title)<18 or source_from_url(href)!=source:
                    continue
                if source=="Zerozero" and "/noticias/" not in href:
                    continue
                if source=="Record" and "/sporting/" not in href:
                    continue
                if source=="A Bola" and "/noticias/" not in href:
                    continue
                add({"title":title,"url":href,"source":source})
        except Exception as e:
            print("Publisher section warning:",source,e)

    # Zerozero fallback: Google News RSS is used only to discover current
    # Zerozero URLs when the publisher blocks GitHub Actions. URLs are decoded
    # before they enter the feed, so Google is never exposed as the source.
    try:
        import feedparser
        from urllib.parse import quote
        queries=("site:zerozero.pt/noticias/ Sporting","site:zerozero.pt/noticias/ Sporting CP","site:zerozero.pt/noticias/ Sporting modalidades")
        feeds=[]
        for query in queries:
            rss="https://news.google.com/rss/search?q="+quote(query)+"&hl=pt-PT&gl=PT&ceid=PT:pt"
            raw=session.get(rss,timeout=20,headers={"User-Agent":USER_AGENT})
            raw.raise_for_status()
            parsed=feedparser.parse(raw.text)
            feeds.append(parsed)
        for feed in feeds:
            for entry in feed.entries[:40]:
                title=clean(entry.get("title") or "")
                link=entry.get("link")
                summary_raw=entry.get("summary") or ""
                summary_soup=BeautifulSoup(summary_raw,"html.parser")
                summary=clean(summary_soup.get_text(" ",strip=True))
                if link and title and "sporting" in (title+" "+summary).lower():
                    media=entry.get("media_content") or entry.get("media_thumbnail") or []
                    image=None
                    # Google News sometimes embeds the publisher's editorial
                    # thumbnail directly in the item description.
                    for im in summary_soup.find_all("img"):
                        u=im.get("src") or im.get("data-src")
                        if u and not re.search(r"(favicon|logo)",u,re.I):
                            image=u
                            break
                    if media and isinstance(media,list):
                        for m in media:
                            u=m.get("url")
                            if u and not re.search(r"(favicon|logo)",u,re.I):
                                image=u
                                break
                    # Google News may expose an image URL in the raw RSS even when
                    # feedparser does not populate media_content.
                    if not image:
                        m=re.search(r'<media:content[^>]+url="([^"]+)"',raw.text,re.I)
                        if m and not re.search(r"(favicon|logo)",m.group(1),re.I):
                            image=m.group(1)
                    add({"title":title,"url":link,"source":"Zerozero",
                         "published":entry.get("published"),
                         "description":summary[:280] if summary else None,
                         **({"image":image,"image_source":"Zerozero RSS"} if image else {})})
    except Exception as e:
        print("Zerozero Google RSS warning:",e)

    # Google News is only a discovery/transport layer for Zerozero. Decode
    # those links now so the final feed contains only the original publisher URL.
    decode_google_urls(items)

    # Re-score after URL decoding because Google News initially labels every
    # item as the expected feed source, while the final URL reveals the actual
    # publisher. Deduplicate again after decoding.
    dedup={}
    for item in items:
        url=item.get("url")
        if not url:
            continue
        key=re.sub(r"[?#].*$","",str(url).rstrip("/")).lower()
        item["source"]=source_from_url(url,item.get("source") or "Google News")
        p=source_priority.get(item["source"],50)
        title=item.get("title","").lower()
        item["_football"]=any(t in title for t in football_terms) and not (
            any(t in title for t in non_football_penalty) and "futebol" not in title
        )
        item["_sporting"]=any(t in title for t in (
            "sporting", "alvalade", "rui borges", "leões", "leoes", "leoas", "leonino",
            "verde e branco", "verde-e-branco"
        )) and not any(t in title for t in (
            "sporting kansas city", "sporting kc", "sporting seis de diciembre",
            "sporting de gijón", "sporting gijon"
        ))
        item["_priority"]=p + (25 if item["_football"] else 0) + (40 if item["_sporting"] else 0)
        old=dedup.get(key)
        if old is None or item["_priority"] > old["_priority"]:
            dedup[key]=item
    items=list(dedup.values())

    # Newest first within the football/source priority. This prevents a large
    # volume of old generic Google results from pushing current football news
    # out of the app.
    from email.utils import parsedate_to_datetime
    def pub_ts(item):
        value=item.get("published")
        if not value:
            return 0
        try:
            return parsedate_to_datetime(value).timestamp()
        except Exception:
            try:
                return datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()
            except Exception:
                return 0
    # Only Sporting Clube de Portugal news is allowed into the app.
    # Football and all Sporting CP modalities are intentionally treated equally.
    items = [x for x in items if x.get("_sporting")]

    # Strict chronological order: newest publication first.
    items.sort(key=pub_ts, reverse=True)

    # Metadata enrichment is the expensive part. Only enrich the visible top
    # 24 and do it concurrently so a slow publisher cannot stall the whole feed.
    top=items[:24]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(article_metadata,item) for item in top]
        enriched=[]
        for future in as_completed(futures):
            enriched.append(future.result())
    # Restore ranking after concurrent enrichment.
    enriched.sort(key=pub_ts, reverse=True)

    for item in enriched:
        item.pop("_priority",None)
        item.pop("_football",None)
        item.pop("_sporting",None)
        item.pop("media_thumbnail",None)
        item.pop("media_content",None)
        if not item.get("image"):
            item.pop("image",None)
    write_json("news.json",{"items":enriched[:30]})
    print("News feed:", len(enriched[:30]), "items;",
          "football:", sum(1 for x in enriched[:30] if source_from_url(x.get("url")) in source_priority))

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

VENUE_FALLBACKS = {
    "sporting cp": ("Estádio José Alvalade", "Lisboa", 38.761207, -9.160876),
    "braga": ("Estádio Municipal de Braga", "Braga", 41.562554, -8.429904),
    "lens": ("Stade Bollaert-Delelis", "Lens", 50.432867, 2.814941),
    "lask": ("Raiffeisen Arena", "Linz", 48.290900, 14.275900),
    "academico viseu": ("Estádio do Fontelo", "Viseu", 40.657200, -7.906500),
    "maritimo": ("Estádio do Marítimo", "Funchal", 32.649500, -16.921900),
    "casa pia ac": ("Estádio Municipal de Rio Maior", "Rio Maior", 39.344541, -8.935162),
    "shakhtar donetsk": ("Stamford Bridge", "London", 51.481711, -0.190975),
    "fc porto": ("Estádio do Dragão", "Porto", 41.161778, -8.584028),
    "porto": ("Estádio do Dragão", "Porto", 41.161778, -8.584028),
    "roma": ("Stadio Olimpico", "Roma", 41.933886, 12.454786),
    "as roma": ("Stadio Olimpico", "Roma", 41.933886, 12.454786),
    "gil vicente": ("Estádio Cidade de Barcelos", "Barcelos", 41.551230, -8.623110),
    "benfica": ("Estádio da Luz", "Lisboa", 38.752778, -9.184722),
    "estoril": ("Estádio António Coimbra da Mota", "Estoril", 38.705278, -9.393889),
    "moreirense": ("Parque Desportivo Comendador Joaquim de Almeida Freitas", "Moreira de Cónegos", 41.388889, -8.343611),
    "man united": ("Old Trafford", "Manchester", 53.463056, -2.291389),
    "manchester united": ("Old Trafford", "Manchester", 53.463056, -2.291389),
    "barcelona": ("Spotify Camp Nou", "Barcelona", 41.380900, 2.122800),
    "manchester city": ("Etihad Stadium", "Manchester", 53.483056, -2.200278)
}

def parse_python_venue(value):
    text = clean(value)
    if not text.startswith("{"):
        return None
    def pick(key):
        m = re.search(r"['\"]"+re.escape(key)+r"['\"]\s*:\s*['\"]([^'\"]*)['\"]", text)
        return m.group(1) if m else ""
    def num(key):
        m = re.search(r"['\"]"+re.escape(key)+r"['\"]\s*:\s*(-?\d+(?:\.\d+)?)", text)
        return float(m.group(1)) if m else None
    lon = num("lon")
    return {"name":pick("name"),"city":pick("city"),"country":pick("country"),
            "lat":num("lat"),"lon":lon if lon is not None else num("long"),"capacity":num("capacity")}

def geocode_missing_venues():
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    cache = safe_existing("venues.json") or {"venues": {}}
    venues = cache.get("venues", {})
    changed = False
    for f in fixtures:
        v = f.get("venue") or {}
        if isinstance(v.get("name"), str) and v.get("name").lstrip().startswith("{"):
            parsed = parse_python_venue(v["name"])
            if parsed:
                v = {**v, **parsed}
        home_key = zz_norm((f.get("home") or {}).get("name"))
        fallback = VENUE_FALLBACKS.get(home_key)
        if fallback:
            fb_name, fb_city, fb_lat, fb_lon = fallback
            if not v.get("name"): v["name"] = fb_name
            if not v.get("city"): v["city"] = fb_city
            if v.get("lat") is None: v["lat"] = fb_lat
            if v.get("lon") is None: v["lon"] = fb_lon
        f["venue"] = v
        name, city = v.get("name"), v.get("city")
        if not name or v.get("lat") is not None:
            continue
        key = f"{name}|{city or ''}"
        if key in venues:
            v["lat"], v["lon"] = venues[key].get("lat"), venues[key].get("lon")
            continue
        try:
            time.sleep(1.1)
            rr = session.get("https://nominatim.openstreetmap.org/search",
                             params={"q":quote_plus(f"{name}, {city or ''}"),"format":"jsonv2","limit":1},
                             timeout=20)
            rr.raise_for_status()
            rows = rr.json()
            if rows:
                venues[key]={"lat":float(rows[0]["lat"]),"lon":float(rows[0]["lon"]),"display":rows[0].get("display_name")}
                v["lat"],v["lon"]=venues[key]["lat"],venues[key]["lon"]
                changed=True
        except Exception as e:
            print("Nominatim warning:", e)
    try:
        existing=safe_existing("fixtures.json") or {}
        existing["fixtures"]=fixtures
        existing["_updated_at"]=now_iso()
        (DATA/"fixtures.json").write_text(json.dumps(existing,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception as e:
        print("Fixture venue persist warning:",e)
    if changed or not (DATA/"venues.json").exists():
        write_json("venues.json",{"venues":venues,"source":"OpenStreetMap Nominatim"})




def _context_team_form(team_payload, limit=5):
    """Normalize FotMob's recent team form to compact, app-friendly rows."""
    overview = _first_dict(team_payload, "overview")
    raw = overview.get("teamForm") or []
    rows = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        result = clean(x.get("resultString") or "")
        if result not in {"W", "D", "L"}:
            continue
        comp = clean(x.get("tournamentName") or x.get("competitionName") or x.get("leagueName") or "")
        if comp and not is_official_sporting_competition(comp):
            continue
        home = x.get("home") or {}
        away = x.get("away") or {}
        opponent = away if home.get("isOurTeam") else home
        dt = ((x.get("date") or {}).get("utcTime") if isinstance(x.get("date"), dict) else x.get("utcTime"))
        rows.append({
            "result": result,
            "score": clean(x.get("score") or ""),
            "date": clean(dt),
            "competition": comp,
            "opponent": clean(opponent.get("name") or ""),
            "match_id": x.get("id") or x.get("matchId"),
        })
    return rows[-limit:]


def _standings_context(competition_name=""):
    all_data = safe_existing("standings-competitions.json") or {}
    wanted = zz_norm(competition_name)
    key_alias = "primeira-liga" if "liga portugal" in wanted or "primeira liga" in wanted else ("champions" if "champions" in wanted or "liga dos campeoes" in wanted else "")
    data = all_data.get(key_alias) if key_alias else None
    if not isinstance(data, dict):
        data = safe_existing("standings.json") or {}
    rows = data.get("table") or []
    out = {}
    for row in rows:
        team = row.get("team") if isinstance(row.get("team"), dict) else {}
        name = clean(team.get("name") or row.get("teamName") or row.get("name"))
        tid = team.get("id") or row.get("teamId")
        entry = {
            "position": row.get("position") or row.get("rank"),
            "played": row.get("playedGames") or row.get("played"),
            "points": row.get("points"),
            "name": name,
        }
        if tid is not None:
            out["id:"+str(tid)] = entry
        if name:
            out["name:"+zz_norm(name)] = entry
    return out



def _h2h_rows(value, home_id, away_id, home_name, away_name):
    """Parse FotMob content.h2h.matches across competitions."""
    found = []
    if not isinstance(value, dict):
        return found
    target_ids = {str(home_id), str(away_id)}
    target_names = {zz_norm(home_name), zz_norm(away_name)}

    def add_match(obj):
        if not isinstance(obj, dict):
            return
        h = obj.get("home") or obj.get("homeTeam") or {}
        a = obj.get("away") or obj.get("awayTeam") or {}
        if not isinstance(h, dict) or not isinstance(a, dict):
            return
        hid, aid = h.get("id"), a.get("id")
        hn, an = zz_norm(h.get("name")), zz_norm(a.get("name"))
        same = ((hid is not None and aid is not None and
                 {str(hid), str(aid)} == target_ids)
                or ({hn, an} == target_names))
        if not same:
            return

        status = obj.get("status") or {}
        score_str = status.get("scoreStr") or obj.get("scoreStr") or ""
        hs = aas = None
        if isinstance(score_str, str) and "-" in score_str:
            parts = [p.strip() for p in score_str.split("-", 1)]
            try:
                hs, aas = int(parts[0]), int(parts[1])
            except Exception:
                pass
        score = obj.get("score") if isinstance(obj.get("score"), dict) else {}
        hs = h.get("score", score.get("home", hs))
        aas = a.get("score", score.get("away", aas))
        if hs is None or aas is None:
            return

        tm = obj.get("time") or obj.get("date") or {}
        utc = tm.get("utcTime") if isinstance(tm, dict) else tm
        league = obj.get("league") or obj.get("tournament") or {}
        comp = league.get("name") if isinstance(league, dict) else league
        found.append({
            "id": obj.get("id") or obj.get("matchId"),
            "date": clean(utc or obj.get("matchDate")),
            "home": clean(h.get("name") or home_name),
            "away": clean(a.get("name") or away_name),
            "home_score": hs,
            "away_score": aas,
            "competition": clean(comp or obj.get("tournamentName") or obj.get("competitionName") or ""),
        })

    matches = value.get("matches")
    if isinstance(matches, list):
        for m in matches:
            add_match(m)

    def walk(obj):
        if isinstance(obj, dict):
            if obj is not value:
                add_match(obj)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(matches if isinstance(matches, (dict, list)) else value)
    dedup = {}
    for row in found:
        key = str(row.get("id") or (row.get("date"), row.get("home"), row.get("away"),
                                    row.get("home_score"), row.get("away_score")))
        dedup[key] = row
    return list(dedup.values())


ZEROZERO_AGENDA = "https://www.zerozero.pt/equipa/sporting/agenda"

ZEROZERO_TEAM_ALIASES = {
    "sporting cp": "sporting", "sporting": "sporting",
    "man united": "manchester-united", "manchester united": "manchester-united",
    "fc porto": "fc-porto", "porto": "fc-porto",
    "gil vicente": "gil-vicente", "braga": "braga", "sc braga": "braga",
    "lens": "lens", "lask": "lask", "shakhtar donetsk": "shakhtar-donetsk",
    "as roma": "roma", "roma": "roma", "benfica": "benfica",
    "maritimo": "maritimo", "marítimo": "maritimo", "moreirense": "moreirense",
    "estoril": "estoril",
    "academico viseu": "academico-viseu", "académico viseu": "academico-viseu",
    "casa pia ac": "casa-pia", "casa pia": "casa-pia",
    "estrela da amadora": "estrela-amadora", "vitoria de guimaraes": "vitoria-guimaraes",
    "vitória de guimarães": "vitoria-guimaraes", "alverca": "alverca",
    "rio ave": "rio-ave", "nacional": "nacional",
    "famalicao": "famalicao", "famalicão": "famalicao",
    "arouca": "arouca", "santa clara": "santa-clara",
    "man city": "manchester-city", "manchester city": "manchester-city",
    "barcelona": "barcelona", "shakhtar": "shakhtar-donetsk"
}
_zerozero_team_cache = {}

def zerozero_get(url):
    """Fetch ZeroZero with content validation and multiple fallbacks.

    GitHub/proxy endpoints can return HTTP 200 challenge pages. We must not
    treat those as successful ZeroZero responses because doing so silently
    produces empty H2H data.
    """
    encoded = quote_plus(url)
    mirror_url = url.replace("https://www.zerozero.pt", "https://zerozero.dk").replace("https://zerozero.pt", "https://zerozero.dk")
    football_url = url.replace("https://www.zerozero.pt", "https://zerozero.football").replace("https://zerozero.pt", "https://zerozero.football")
    mirror_encoded = quote_plus(mirror_url)
    football_encoded = quote_plus(football_url)
    # Prefer Jina first: it has been the most reliable low-request path
    # from GitHub Actions. Keep direct ZeroZero as a fallback and avoid
    # multiplying 403/429-triggering proxy calls.
    candidates = [
        "https://r.jina.ai/" + football_url,
        "https://r.jina.ai/" + mirror_url,
        football_url,
        mirror_url,
        url,
    ]
    is_stats = "/estatisticas/" in url
    last = None

    for candidate in candidates:
        try:
            rr = session.get(
                candidate,
                timeout=8,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            rr.raise_for_status()
            body = rr.text or ""
            if len(body) < 500:
                continue

            # Do not accept a generic 200 challenge/error page.
            if is_stats:
                # A stats page is only useful if it contains actual H2H data.
                # A generic challenge shell can still contain the page title,
                # so the title alone is NOT a valid response.
                has_summary = bool(re.search(
                    r"Em todas as competições.*?(?:\d+ jogos|nunca se defrontaram)",
                    body, re.I | re.S
                ))
                has_no_history = "nunca se defrontaram" in zz_norm(body)
                has_games = bool(re.search(r"20\\d{2}-\\d{2}-\\d{2}", body))
                if not (has_summary or has_no_history or has_games):
                    print("ZeroZero incomplete stats response:", candidate.split("/")[2])
                    continue
            else:
                if "zerozero" not in body.lower() and "Página Inicial" not in body:
                    continue

            print("ZeroZero fetch OK:", candidate.split("/")[2])
            return body
        except Exception as ex:
            last = ex
            print("ZeroZero fetch failed:", candidate.split("/")[2], ex)

    raise RuntimeError(f"ZeroZero request failed or returned invalid content: {url}: {last}")


def _zz_name_key(value):
    return zz_norm(value)


def _zz_parse_games(html):
    """Parse ZeroZero H2H rows from table markup; supports all competitions."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    def add_cells(cells):
        cells = [clean(re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", x)) for x in cells if clean(x)]
        if len(cells) < 4:
            return
        date_value = next((x for x in cells if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", x)), None)
        if not date_value:
            return
        score_idx = None
        score_home = score_away = None
        for idx, cell in enumerate(cells):
            m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})(?:a\.p\.|\s*)?", cell, re.I)
            if m:
                score_idx = idx
                score_home, score_away = int(m.group(1)), int(m.group(2))
                break
        if score_idx is None or score_idx < 1 or score_idx + 1 >= len(cells):
            return
        home_name, away_name = cells[score_idx - 1], cells[score_idx + 1]
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", home_name):
            return

        competition = ""
        season = ""
        round_name = ""
        for cell in cells:
            sm = re.search(r"\b(\d{2}/\d{2})\b", cell)
            if sm:
                season = sm.group(1)
                competition = clean(re.sub(r"\s*\d{2}/\d{2}\s*", " ", cell))
            elif re.fullmatch(r"(?:J\d+|QF|SF|MF|1/8|1/4|1/2|F|FL|FG|PO|R\d+)", cell, re.I):
                round_name = cell
        if not competition:
            for cell in cells:
                if any(k in zz_norm(cell) for k in (
                    "liga portugal","liga dos campeoes","uefa champions league",
                    "taca de portugal","taca da liga","supercopa","supertaca",
                    "premier league","fa cup","league cup","champions"
                )):
                    competition = cell
                    break
        found.append({
            "id": None, "date": date_value, "home": home_name, "away": away_name,
            "home_score": score_home, "away_score": score_away,
            "competition": competition, "season": season, "round": round_name,
        })

    for tr in soup.find_all("tr"):
        add_cells([x.get_text(" ", strip=True) for x in tr.find_all(["th","td"])])

    if not found:
        lines = [clean(x) for x in soup.get_text("\n", strip=True).splitlines() if clean(x)]
        for idx, line in enumerate(lines):
            if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", line):
                continue
            window = lines[idx:idx+12]
            score_pos = next((j for j,x in enumerate(window)
                              if re.fullmatch(r"\d{1,2}\s*-\s*\d{1,2}(?:a\.p\.)?", x, re.I)), None)
            if score_pos is not None and score_pos >= 1 and score_pos + 1 < len(window):
                add_cells([line, window[score_pos-1], window[score_pos], window[score_pos+1], *window[score_pos+2:]])

    dedup = {}
    for row in found:
        key = (row["date"], zz_norm(row["home"]), zz_norm(row["away"]), row["home_score"], row["away_score"])
        dedup[key] = row
    return sorted(dedup.values(), key=lambda x: x.get("date") or "", reverse=True)


def zerozero_team_ref(name):
    key = zz_norm(name)
    if key in _zerozero_team_cache:
        return _zerozero_team_cache[key]
    slug = ZEROZERO_TEAM_ALIASES.get(key) or re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    try:
        page = zerozero_get(f"https://www.zerozero.pt/equipa/{slug}")
        patterns = [
            r'href=["\'](/equipa/[^"\']+/\d+)["\']',
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https?://(?:www\.)?zerozero\.(?:pt|dk|football)/equipa/[^"\']+/\d+)',
        ]
        href = None
        for pat in patterns:
            m = re.search(pat, page, re.I)
            if m:
                href = m.group(1)
                break

        # r.jina.ai returns Markdown when ZeroZero blocks the runner.
        # In that case there are no HTML href attributes; recover the team
        # reference from Markdown/plain URLs instead.
        if not href:
            m = re.search(r"https?://(?:www\.)?zerozero\.(?:pt|dk|football)/equipa/([^/\s)]+)/([0-9]+)", page)
            if m:
                href = m.group(0)
            else:
                m = re.search(r"\]\((/equipa/[^/\s)]+/[0-9]+)\)", page)
                if m:
                    href = m.group(1)

        if href:
            if href.startswith("/"):
                href = "https://www.zerozero.pt" + href
            mm = re.search(r"/equipa/([^/]+)/([0-9]+)", href)
            if mm:
                ref = {"slug": mm.group(1), "id": int(mm.group(2)), "url": href}
                _zerozero_team_cache[key] = ref
                return ref
    except Exception as e:
        print("ZeroZero team ref warning:", name, e)
    return None


def _zz_xray_for_fixture(f):
    home = clean((f.get("home") or {}).get("name"))
    away = clean((f.get("away") or {}).get("name"))
    h = zerozero_team_ref(home)
    a = zerozero_team_ref(away)
    if not h or not a:
        raise RuntimeError(f"ZeroZero team reference unavailable: {home} / {away}")

    url = f"https://www.zerozero.pt/estatisticas/{h['slug']}-{a['slug']}/t{h['id']}-t{a['id']}"
    html = zerozero_get(url)
    games = _zz_parse_games(html)
    wanted = {_zz_name_key(home), _zz_name_key(away)}
    rows = [g for g in games if {_zz_name_key(g.get("home")), _zz_name_key(g.get("away"))} == wanted]
    rows.sort(key=lambda x: x.get("date") or "", reverse=True)

    text_content = clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    if "nunca se defrontaram" in zz_norm(text_content):
        return [], [0, 0, 0, 0]

    summary = None
    pat = re.search(
        r"Em todas as competições .*?(\d+) jogos.*?(\d+) vitórias do (.*?), "
        r"(\d+) empates e (\d+) (?:triunfos|vitórias) do (.*?)(?:\.|\s+Em casa)",
        text_content, re.I
    )
    if pat:
        first_team, second_team = clean(pat.group(3)), clean(pat.group(6))
        vals = [int(pat.group(2)), int(pat.group(4)), int(pat.group(5)), int(pat.group(1))]
        summary = vals if _zz_name_key(first_team) == _zz_name_key(home) else [vals[2], vals[1], vals[0], vals[3]]
        if not summary[-1]:
            return [], [0, 0, 0, 0]

    if summary is None:
        hw = dw = aw = 0
        for x in rows:
            hs, ascore = fnum(x.get("home_score")), fnum(x.get("away_score"))
            if hs is None or ascore is None:
                continue
            if hs == ascore:
                dw += 1
            elif _zz_name_key(x.get("home")) == _zz_name_key(home):
                hw += 1
            else:
                aw += 1
        if rows:
            summary = [hw, dw, aw, len(rows)]
    if not rows and summary is None:
        raise RuntimeError(f"ZeroZero H2H page parsed without history data: {home} / {away}")
    return rows[:4], summary


_fotmob_team_matches_cache = None

def _fotmob_find_recent_h2h_event(f):
    """Find a recent completed Sporting-opponent event from the team feed.

    Upcoming matchDetails often has no H2H payload. A completed meeting from
    the same team feed does, so reuse that event instead of querying the
    future fixture. The team feed is fetched once per H2H run.
    """
    global _fotmob_team_matches_cache
    if _fotmob_team_matches_cache is None:
        raw = fotmob_get("/api/data/teams", {"id": SPORTING_FOTMOB_ID, "ccode3": "PRT"})
        found = []
        def walk(v):
            if isinstance(v, dict):
                home = v.get("home") or {}
                away = v.get("away") or {}
                if isinstance(home, dict) and isinstance(away, dict) and v.get("id"):
                    if home.get("id") and away.get("id"):
                        found.append(v)
                for child in v.values():
                    if isinstance(child, (dict, list)):
                        walk(child)
            elif isinstance(v, list):
                for child in v:
                    walk(child)
        walk(raw)
        # Newest first; duplicate event IDs are collapsed.
        dedup = {}
        for x in found:
            dedup[str(x.get("id"))] = x
        _fotmob_team_matches_cache = list(dedup.values())

    opponent_id = ((f.get("away") or {}).get("id")
                   if int(((f.get("home") or {}).get("id") or 0)) == SPORTING_FOTMOB_ID
                   else (f.get("home") or {}).get("id"))
    opponent_name = zz_norm(
        ((f.get("away") or {}).get("name")
         if int(((f.get("home") or {}).get("id") or 0)) == SPORTING_FOTMOB_ID
         else (f.get("home") or {}).get("name"))
    )
    candidates = []
    for x in _fotmob_team_matches_cache:
        home, away = x.get("home") or {}, x.get("away") or {}
        if int(home.get("id") or 0) != SPORTING_FOTMOB_ID and int(away.get("id") or 0) != SPORTING_FOTMOB_ID:
            continue
        other = away if int(home.get("id") or 0) == SPORTING_FOTMOB_ID else home
        same_id = opponent_id is not None and int(other.get("id") or 0) == int(opponent_id)
        same_name = opponent_name and zz_norm(other.get("name")) == opponent_name
        if same_id or same_name:
            status = x.get("status") or {}
            finished = status.get("finished") is True or status.get("short") in {"FT", "finished"}
            if finished or (x.get("homeScore") is not None and x.get("awayScore") is not None):
                candidates.append(x)
    if not candidates:
        return None
    candidates.sort(key=lambda x: int(x.get("utcTime") or x.get("startTime") or x.get("id") or 0), reverse=True)
    return candidates[0].get("id")

def _fotmob_h2h_from_recent_event(f):
    event_id = _fotmob_find_recent_h2h_event(f)
    if not event_id:
        return [], None
    raw = fotmob_get("/api/data/matchDetails", {"matchId": event_id})
    content = _first_dict(raw, "content")
    payload = content.get("h2h") or {}
    rows = _h2h_rows(payload,
                      (f.get("home") or {}).get("id"),
                      (f.get("away") or {}).get("id"),
                      (f.get("home") or {}).get("name"),
                      (f.get("away") or {}).get("name"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not (isinstance(summary, list) and len(summary) >= 3):
        summary = None
    return rows, summary

def fetch_zerozero_h2h(upcoming):
    """Fetch H2H once per unique opponent, concurrently and with fallbacks."""
    result = {}

    # One representative fixture per unique pair. The same opponent can occur
    # several times in the calendar; the historical H2H is identical.
    representatives = {}
    for f in upcoming:
        home = clean((f.get("home") or {}).get("name"))
        away = clean((f.get("away") or {}).get("name"))
        key = tuple(sorted((zz_norm(home), zz_norm(away))))
        representatives.setdefault(key, f)

    # Resolve every distinct team once before starting the pair collector.
    # In particular, Sporting's ZeroZero page must never be fetched once per
    # opponent: that repetition is what triggers Jina 429 from Actions.
    team_refs = {}
    team_names = set()
    for f in upcoming:
        team_names.add(clean((f.get("home") or {}).get("name")))
        team_names.add(clean((f.get("away") or {}).get("name")))
    for team_name in team_names:
        if not team_name:
            continue
        try:
            team_refs[zz_norm(team_name)] = zerozero_team_ref(team_name)
        except Exception:
            team_refs[zz_norm(team_name)] = None

    def collect(item):
        key, f = item
        home = clean((f.get("home") or {}).get("name"))
        away = clean((f.get("away") or {}).get("name"))
        rows, summary, source = [], None, "unavailable"

        # Use the proven low-request ZeroZero path first. Only if it fails do
        # we spend calls on SofaScore/FotMob fallbacks.
        try:
            h = team_refs.get(zz_norm(home))
            a = team_refs.get(zz_norm(away))
            if not h or not a:
                raise RuntimeError(f"ZeroZero team reference unavailable: {home} / {away}")
            url = f"https://www.zerozero.pt/estatisticas/{h['slug']}-{a['slug']}/t{h['id']}-t{a['id']}"
            html = zerozero_get(url)
            games = _zz_parse_games(html)
            wanted = {_zz_name_key(home), _zz_name_key(away)}
            rows = [g for g in games if {_zz_name_key(g.get("home")), _zz_name_key(g.get("away"))} == wanted]
            rows.sort(key=lambda x: x.get("date") or "", reverse=True)
            text_content = clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
            if "nunca se defrontaram" in zz_norm(text_content):
                rows, summary = [], [0, 0, 0, 0]
            else:
                summary = None
                pat = re.search(r"Em todas as competições .*?(\d+) jogos.*?(\d+) vitórias do (.*?), (\d+) empates e (\d+) (?:triunfos|vitórias) do (.*?)(?:\.|\s+Em casa)", text_content, re.I)
                if pat:
                    first_team = clean(pat.group(3))
                    vals = [int(pat.group(2)), int(pat.group(4)), int(pat.group(5)), int(pat.group(1))]
                    summary = vals if _zz_name_key(first_team) == _zz_name_key(home) else [vals[2], vals[1], vals[0], vals[3]]
                if summary is None and rows:
                    hw = dw = aw = 0
                    for x in rows:
                        hs, ascore = fnum(x.get("home_score")), fnum(x.get("away_score"))
                        if hs is None or ascore is None:
                            continue
                        if hs == ascore: dw += 1
                        elif _zz_name_key(x.get("home")) == _zz_name_key(home): hw += 1
                        else: aw += 1
                    summary = [hw, dw, aw, len(rows)]
            if not rows and summary is None:
                raise RuntimeError(f"ZeroZero H2H page parsed without history data: {home} / {away}")
            source = "ZeroZero"
            print("H2H ZeroZero primary:", home, "vs", away, "=>", len(rows))
        except Exception as e:
            print("H2H ZeroZero primary failed:", home, "vs", away, e)

        if not rows and summary is None:
            try:
                rows, summary = _fotmob_h2h_from_recent_event(f)
                if rows or summary is not None:
                    source = "FotMob historical event"
                    print("H2H FotMob historical-event fallback:", home, "vs", away, "=>", len(rows))
            except Exception as e:
                print("H2H FotMob historical-event failed:", home, "vs", away, e)

        if not rows and summary is None:
            try:
                event_id = _sofa_scheduled_event_for_fixture(f)
                rows, summary = _sofa_h2h_from_event(event_id, f)
                if rows or summary is not None:
                    source = "SofaScore"
                    print("H2H SofaScore fallback:", home, "vs", away, "=>", len(rows))
            except Exception as e:
                print("H2H SofaScore fallback failed:", home, "vs", away, e)

        if not rows and summary is None:
            try:
                rows, summary = _sofa_h2h_for_fixture(f)
                if rows or summary is not None:
                    source = "SofaScore"
                    print("H2H SofaScore secondary fallback:", home, "vs", away, "=>", len(rows))
            except Exception as e:
                print("H2H SofaScore secondary fallback failed:", home, "vs", away, e)

        if not rows and summary is None:
            try:
                raw = fotmob_get("/api/data/matchDetails", {"matchId": f["id"]})
                content = _first_dict(raw, "content")
                payload = content.get("h2h") or {}
                rows = _h2h_rows(payload, (f.get("home") or {}).get("id"),
                                  (f.get("away") or {}).get("id"), home, away)
                summary = payload.get("summary") if isinstance(payload, dict) else None
                if not (isinstance(summary, list) and len(summary) >= 3):
                    summary = None
                if rows or summary is not None:
                    source = "FotMob"
                    print("H2H FotMob fallback:", home, "vs", away, "=>", len(rows))
            except Exception as e:
                print("H2H FotMob failed:", home, "vs", away, e)

        return key, rows[:4], summary[:3] if isinstance(summary, list) else summary, source

    # Four concurrent pairs keeps the collector fast without triggering source rate limits.
    collected = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(collect, item) for item in representatives.items()]
        for future in as_completed(futures):
            key, rows, summary, source = future.result()
            collected[key] = (rows, summary, source)

    for f in upcoming:
        fid = str(f["id"])
        home = clean((f.get("home") or {}).get("name"))
        away = clean((f.get("away") or {}).get("name"))
        key = tuple(sorted((zz_norm(home), zz_norm(away))))
        rows, summary, source = collected.get(key, ([], None, "unavailable"))
        result[fid] = {
            "matches": rows[:4],
            "summary": summary,
            "source": source,
            "source_ok": source != "unavailable",
            "history_found": bool(rows or summary),
        }

    return result


def _sofa_scheduled_event_for_fixture(f):
    kickoff = clean(f.get("kickoff_date"))
    if not kickoff and f.get("date"):
        try:
            kickoff = datetime.fromtimestamp(int(f["date"]), timezone.utc).date().isoformat()
        except Exception:
            kickoff = ""
    if not kickoff:
        return None
    raw = sofa_get(f"/sport/football/scheduled-events/{kickoff}")
    events = raw.get("events") if isinstance(raw, dict) else []
    home = zz_norm((f.get("home") or {}).get("name"))
    away = zz_norm((f.get("away") or {}).get("name"))
    aliases = {
        "man united": {"manchester united", "manchester united fc", "man united"},
        "fc porto": {"porto", "fc porto", "fc do porto"},
        "sporting cp": {"sporting", "sporting cp", "sporting clube de portugal"},
    }
    def same(a,b):
        a,b=zz_norm(a),zz_norm(b)
        return a==b or b in aliases.get(a,{a}) or a in aliases.get(b,{b})
    for e in events or []:
        ht=(e.get("homeTeam") or {}).get("name")
        at=(e.get("awayTeam") or {}).get("name")
        if (same(ht,home) and same(at,away)) or (same(ht,away) and same(at,home)):
            return e.get("id")
    return None

def _sofa_h2h_from_event(event_id, f):
    if not event_id:
        return [], None
    summary_raw=None
    rows=[]
    try:
        summary_raw=sofa_get(f"/event/{quote_plus(str(event_id))}/h2h")
    except Exception:
        pass
    try:
        raw=sofa_get(f"/event/{quote_plus(str(event_id))}/h2h/events")
        matches=raw.get("events") if isinstance(raw,dict) else []
        if not matches and isinstance(raw,dict):
            matches=raw.get("matches") or []
        for m in matches or []:
            ht=m.get("homeTeam") or {}; at=m.get("awayTeam") or {}
            hs=(m.get("homeScore") or {}).get("current")
            aas=(m.get("awayScore") or {}).get("current")
            if hs is None or aas is None: continue
            ts=m.get("startTimestamp")
            dt=datetime.fromtimestamp(int(ts),timezone.utc).isoformat().replace("+00:00","Z") if ts else ""
            tr=m.get("tournament") or {}
            rows.append({"id":m.get("id"),"date":dt,"home":clean(ht.get("name")),"away":clean(at.get("name")),
                         "home_score":hs,"away_score":aas,
                         "competition":clean(tr.get("name") if isinstance(tr,dict) else "")})
    except Exception as ex:
        print("Sofa scheduled-event H2H warning:", event_id, ex)
    duel=(summary_raw or {}).get("teamDuel") if isinstance(summary_raw,dict) else {}
    summary=None
    if isinstance(duel,dict) and duel:
        summary=[int(duel.get("homeWins") or 0),int(duel.get("draws") or 0),int(duel.get("awayWins") or 0)]
        if sum(summary)==0 and not rows: summary=None
    if not summary and rows:
        home_name=zz_norm((f.get("home") or {}).get("name")); hw=dw=aw=0
        for x in rows:
            hs,aas=fnum(x.get("home_score")),fnum(x.get("away_score"))
            if hs is None or aas is None: continue
            if hs==aas: dw+=1
            elif zz_norm(x.get("home"))==home_name: hw+=1
            else: aw+=1
        summary=[hw,dw,aw]
    rows.sort(key=lambda x:x.get("date") or "",reverse=True)
    return rows[:4],summary

def _sofa_h2h_for_fixture(f):
    """SofaScore H2H using the event customId when available, with numeric-id fallback."""
    event_id = f.get("id")
    custom_id = clean(f.get("custom_id") or f.get("customId"))
    refs = [custom_id, event_id]
    seen_refs = set()
    try:
        summary_raw = None
        rows = []
        for ref in refs:
            if not ref or str(ref) in seen_refs:
                continue
            seen_refs.add(str(ref))
            try:
                summary_raw = sofa_get(f"/event/{quote_plus(str(ref))}/h2h")
                if summary_raw:
                    break
            except Exception:
                continue

        for ref in refs:
            if not ref:
                continue
            try:
                raw = sofa_get(f"/event/{quote_plus(str(ref))}/h2h/events")
                matches = raw.get("events") if isinstance(raw, dict) else []
                if not matches and isinstance(raw, dict):
                    matches = raw.get("matches") or []
                if matches:
                    for m in matches:
                        ht = m.get("homeTeam") or {}
                        at = m.get("awayTeam") or {}
                        hs = (m.get("homeScore") or {}).get("current")
                        aas = (m.get("awayScore") or {}).get("current")
                        if hs is None or aas is None:
                            continue
                        ts = m.get("startTimestamp")
                        dt = datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00","Z") if ts else ""
                        tournament = m.get("tournament") or {}
                        rows.append({
                            "id": m.get("id"),
                            "date": dt,
                            "home": clean(ht.get("name")),
                            "away": clean(at.get("name")),
                            "home_score": hs,
                            "away_score": aas,
                            "competition": clean(tournament.get("name") if isinstance(tournament, dict) else ""),
                        })
                    if rows:
                        break
            except Exception as ex:
                print("Sofa H2H events warning:", ref, ex)

        # Prefer SofaScore's official duel summary when available.
        duel = (summary_raw or {}).get("teamDuel") if isinstance(summary_raw, dict) else {}
        if not isinstance(duel, dict):
            duel = {}
        summary = [
            int(duel.get("homeWins") or 0),
            int(duel.get("draws") or 0),
            int(duel.get("awayWins") or 0),
        ] if duel else None

        # If no summary is exposed, calculate from returned meetings.
        if not summary or sum(summary) == 0:
            # SofaScore can return a synthetic 0/0/0 duel even when it has
            # no H2H events. Treat that as unavailable so we can fall through.
            if not rows:
                return [], None
            home_name = zz_norm((f.get("home") or {}).get("name"))
            away_name = zz_norm((f.get("away") or {}).get("name"))
            hw = dw = aw = 0
            for x in rows:
                hs, aas = fnum(x.get("home_score")), fnum(x.get("away_score"))
                if hs is None or aas is None:
                    continue
                if hs == aas:
                    dw += 1
                elif zz_norm(x.get("home")) == home_name:
                    hw += 1
                elif zz_norm(x.get("away")) == home_name:
                    aw += 1
            summary = [hw, dw, aw]

        return rows, summary
    except Exception as e:
        print("Sofa H2H warning:", event_id, e)
        return [], None

def fetch_match_contexts():
    """Build form, standings, H2H and optional Betano odds for upcoming matches."""
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    upcoming = [f for f in fixtures if f.get("status", {}).get("short") == "scheduled" and f.get("id")]
    upcoming = sorted(upcoming, key=lambda x: x.get("date") or 0)[:30]
    standings_cache = {}

    # Reuse Sporting's team payload and only fetch unique opponents.
    team_payloads = {str(SPORTING_FOTMOB_ID): fotmob_get("/api/data/teams", {"id": SPORTING_FOTMOB_ID, "ccode3": "PRT"})}
    opponent_ids = set()
    for f in upcoming:
        for side in ("home", "away"):
            team = f.get(side) or {}
            tid = team.get("id")
            if tid and str(tid) != str(SPORTING_FOTMOB_ID):
                opponent_ids.add(str(tid))
    for tid in sorted(opponent_ids):
        try:
            team_payloads[tid] = fotmob_get("/api/data/teams", {"id": tid, "ccode3": "PRT"})
        except Exception as e:
            print("Team context warning:", tid, e)

    contexts = {}
    for f in upcoming:
        fid = str(f["id"])
        sides = {}
        standings = standings_cache.setdefault(clean((f.get("competition") or {}).get("name")), _standings_context(clean((f.get("competition") or {}).get("name"))))
        for side in ("home", "away"):
            team = f.get(side) or {}
            tid = str(team.get("id") or "")
            name = clean(team.get("name") or "")
            payload = team_payloads.get(tid)
            form = _context_team_form(payload, 5) if payload else []
            table = standings.get("id:"+tid) or standings.get("name:"+zz_norm(name))
            sides[side] = {"id": team.get("id"), "name": name, "form": form, "table": table}


        h2h = []
        h2h_summary = None
        try:
            raw = fotmob_get("/api/data/matchDetails", {"matchId": f["id"]})
            content = _first_dict(raw, "content")
            h2h_payload = content.get("h2h") or {}
            if isinstance(h2h_payload, dict):
                summary = h2h_payload.get("summary")
                if isinstance(summary, list) and len(summary) >= 3:
                    try:
                        h2h_summary = [int(summary[0]), int(summary[1]), int(summary[2])]
                    except Exception:
                        h2h_summary = None
            h2h = _h2h_rows(
                h2h_payload,
                (f.get("home") or {}).get("id"),
                (f.get("away") or {}).get("id"),
                (f.get("home") or {}).get("name"),
                (f.get("away") or {}).get("name")
            )
        except Exception as e:
            print("H2H detail warning:", fid, e)

        h2h = sorted({str(x.get("id") or (x.get("date"),x.get("home"),x.get("away"),x.get("home_score"),x.get("away_score"))): x for x in h2h}.values(),
                     key=lambda x: x.get("date") or "", reverse=True)

        if h2h_summary:
            hw, dw, aw = h2h_summary
        else:
            hw = dw = aw = 0
            for x in h2h:
                hs, aas = fnum(x.get("home_score")), fnum(x.get("away_score"))
                if hs is None or aas is None:
                    continue
                if hs > aas: hw += 1
                elif hs < aas: aw += 1
                else: dw += 1


        contexts[fid] = {
            "home": sides["home"],
            "away": sides["away"],
            "h2h": {"matches": h2h[:4], "home_wins": hw, "draws": dw, "away_wins": aw, "total": hw+dw+aw},
            "updated_at": now_iso(),
        }

    write_json("match-context.json", {
        "fixtures": contexts,
        "source": "FotMob · current standings · team form · match H2H",
        "scope": "Upcoming Sporting CP fixtures; form is last 5 official first-team matches."
    })


def fetch_betano_odds():
    """Optional Betano feed via Odds-API.io. Requires BETANO_ODDS_API_KEY."""
    api_key = os.environ.get("BETANO_ODDS_API_KEY")
    fixtures = (safe_existing("fixtures.json") or {}).get("fixtures", [])
    upcoming = sorted(
        [f for f in fixtures if f.get("status", {}).get("short") == "scheduled" and f.get("id")],
        key=lambda x: x.get("date") or 0
    )[:12]
    result = {}
    if not api_key:
        write_json("betano-odds.json", {
            "fixtures": result,
            "source": "Betano via Odds-API.io",
            "status": "not_configured"
        })
        return

    try:
        r = session.get(
            "https://api.odds-api.io/v3/events",
            params={"apiKey": api_key, "sport": "football", "bookmaker": "Betano"},
            timeout=30
        )
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list):
            events = events.get("events", []) if isinstance(events, dict) else []
    except Exception as e:
        print("Betano events warning:", e)
        write_json("betano-odds.json", {
            "fixtures": result,
            "source": "Betano via Odds-API.io",
            "status": "error"
        })
        return

    def norm(v):
        return zz_norm(v).replace("futebol clube", "").replace("sc ", "").strip()

    matched = []
    for f in upcoming:
        hn, an = norm((f.get("home") or {}).get("name")), norm((f.get("away") or {}).get("name"))
        best = None
        best_delta = None
        target = float(f.get("date") or 0)
        for e in events:
            eh, ea = norm(e.get("home")), norm(e.get("away"))
            if not eh or not ea or eh != hn or ea != an:
                continue
            try:
                ed = datetime.fromisoformat(str(e.get("date")).replace("Z","+00:00")).timestamp()
            except Exception:
                ed = 0
            delta = abs(ed-target)
            if best is None or delta < best_delta:
                best, best_delta = e, delta
        if best:
            matched.append((f, best))

    for f, e in matched:
        try:
            rr = session.get(
                "https://api.odds-api.io/v3/odds",
                params={"apiKey": api_key, "eventId": e.get("id"), "bookmakers": "Betano"},
                timeout=25
            )
            rr.raise_for_status()
            payload = rr.json()
            result[str(f["id"])] = {
                "event_id": e.get("id"),
                "updated_at": now_iso(),
                "bookmakers": payload.get("bookmakers") if isinstance(payload, dict) else None
            }
        except Exception as ex:
            print("Betano odds warning:", f.get("id"), ex)

    write_json("betano-odds.json", {
        "fixtures": result,
        "source": "Betano via Odds-API.io",
        "status": "ok"
    })


ODDSPAPI_BASE = "https://api.oddspapi.io/v4"

def _public_odds_from_sportytrader(upcoming):
    """Public 1X2 fallback when no OddsPapi key is configured."""
    texts=[]
    for url in ("https://www.sportytrader.pt/quotas/futebol/","https://www.sportytrader.pt/quotas/"):
        try:
            rr=session.get(url,timeout=35,headers={"User-Agent":USER_AGENT})
            rr.raise_for_status()
            texts.append(BeautifulSoup(rr.text,"html.parser").get_text(" ",strip=True))
        except Exception as e:
            print("SportyTrader odds warning:",url,e)
    if not texts: return {}

    def norm(v): return re.sub(r"[^a-z0-9]+","",zz_norm(v))
    aliases={
        "sportingcp":"sporting","sporting":"sporting","braga":"braga","fcporto":"fcporto","porto":"fcporto",
        "manunited":"manchesterunited","manchesterunited":"manchesterunited","gilvicente":"gilvicente",
        "asroma":"roma","roma":"roma","shakhtardonetsk":"shakhtardonetsk","lask":"lasklinz"
    }
    def nk(v): return aliases.get(norm(v),norm(v))
    pattern=re.compile(
        r"(\d{1,2}\s+[a-zçãéêíóú]+\.?(?:\s*[-/]\s*\d{1,2})?\s*-\s*\d{2}:\d{2})\s+"
        r"(.+?)\s+-\s+(.+?)\s+1\s+([0-9]+(?:[.,][0-9]+)?)\s+X\s+([0-9]+(?:[.,][0-9]+)?)\s+2\s+([0-9]+(?:[.,][0-9]+)?)",
        re.I
    )
    matches=[]
    for text in texts: matches.extend(pattern.findall(text))
    result={}
    for f in upcoming:
        hn,an=nk((f.get("home") or {}).get("name")),nk((f.get("away") or {}).get("name"))
        for _,home,away,oh,od,oa in matches:
            if nk(home)==hn and nk(away)==an:
                result[str(f["id"])]={
                    "updated_at":now_iso(),
                    "source":"SportyTrader public odds comparator",
                    "bookmaker_name":"Mercado",
                    "bookmakers":{"Mercado":[{"name":"ML","odds":[{"home":oh.replace(",","."),"draw":od.replace(",","."),"away":oa.replace(",",".")}]}]}
                }
                break
    return result

def fetch_oddspapi_betano_odds():
    """Use Betano via OddsPapi when configured; otherwise never leave odds empty."""
    api_key=os.environ.get("ODDSPAPI_API_KEY")
    fixtures=(safe_existing("fixtures.json") or {}).get("fixtures",[])
    upcoming=sorted([f for f in fixtures if f.get("status",{}).get("short")=="scheduled" and f.get("id")],key=lambda x:x.get("date") or 0)[:30]
    if not api_key:
        result=_public_odds_from_sportytrader(upcoming)
        write_json("betano-odds.json",{"fixtures":result,"source":"SportyTrader public odds comparator","status":"public_fallback","coverage":len(result)})
        return

    try:
        tr=session.get(f"{ODDSPAPI_BASE}/tournaments",params={"sportId":10,"apiKey":api_key},timeout=30)
        tr.raise_for_status()
        tournaments=tr.json()
        if not isinstance(tournaments,list): tournaments=tournaments.get("data",[]) if isinstance(tournaments,dict) else []
        wanted_ids=[]
        for t in tournaments:
            n=zz_norm(t.get("tournamentName") or t.get("name") or "")
            s=zz_norm(t.get("tournamentSlug") or t.get("slug") or "")
            if any(k in n+s for k in ("liga portugal","primeira liga","uefa champions league","champions league","allianz cup","taca da liga")) and t.get("tournamentId") is not None:
                wanted_ids.append(str(t["tournamentId"]))
        rr=session.get(f"{ODDSPAPI_BASE}/odds-by-tournaments",params={"bookmaker":"betano","tournamentIds":",".join(dict.fromkeys(wanted_ids)),"apiKey":api_key,"oddsFormat":"decimal"},timeout=40)
        rr.raise_for_status()
        payload=rr.json()
        rows=payload.get("data",[]) if isinstance(payload,dict) else payload
        if not isinstance(rows,list): rows=[]
        def norm(v): return re.sub(r"[^a-z0-9]+","",zz_norm(v))
        result={}
        for f in upcoming:
            hn,an=norm((f.get("home") or {}).get("name")),norm((f.get("away") or {}).get("name"))
            target=int(f.get("date") or 0); best=None; best_delta=None
            for row in rows:
                if not row.get("hasOdds"): continue
                p1,p2=norm(row.get("participant1Name") or ""),norm(row.get("participant2Name") or "")
                if p1 and p2 and not ((p1==hn and p2==an) or (p1==an and p2==hn)): continue
                try: rd=datetime.fromisoformat(str(row.get("startTime")).replace("Z","+00:00")).timestamp()
                except Exception: rd=0
                delta=abs(rd-target)
                if best is None or delta<best_delta: best,best_delta=row,delta
            if not best: continue
            book=(best.get("bookmakerOdds") or {}).get("betano") or {}
            outcomes=((book.get("markets") or {}).get("101") or {}).get("outcomes") or {}
            vals={}
            for oid,key in (("101","home"),("102","draw"),("103","away")):
                players=(outcomes.get(oid) or {}).get("players") or {}
                p=players.get("0") or next(iter(players.values()),{})
                if p.get("price") not in (None,""): vals[key]=p.get("price")
            if len(vals)==3:
                result[str(f["id"])]={
                    "updated_at":clean(best.get("updatedAt")) or now_iso(),
                    "source":"Betano via OddsPapi","bookmaker_name":"Betano",
                    "bookmakers":{"Betano PT":[{"name":"ML","odds":[vals]}]}
                }
        write_json("betano-odds.json",{"fixtures":result,"source":"Betano via OddsPapi","status":"ok","coverage":len(result)})
    except Exception as e:
        print("OddsPapi Betano warning:",e)
        result=_public_odds_from_sportytrader(upcoming)
        write_json("betano-odds.json",{"fixtures":result,"source":"SportyTrader public odds comparator","status":"public_fallback","coverage":len(result),"error":str(e)})



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
            fetch_match_contexts()
        except Exception as e:
            print(f"Match context enrichment skipped: {e}")
        try:
            fetch_oddspapi_betano_odds()
        except Exception as e:
            print(f"OddsPapi Betano odds skipped: {e}")
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
