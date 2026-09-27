import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

BASE_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36", "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"}

def norm(v):
    s = "" if v is None else str(v).strip().lower()
    repl = {"á":"a","à":"a","ã":"a","â":"a","é":"e","ê":"e","í":"i","ó":"o","ô":"o","õ":"o","ú":"u","ç":"c"}
    return "".join(repl.get(ch,ch) for ch in s)

def fetch_betstudy_h2h(upcoming):
    out = {}
    try:
        html = requests.get("https://www.betstudy.com/soccer-stats/teams/sporting-cp/58/fixtures/", headers=BASE_HEADERS, timeout=40).text
        soup = BeautifulSoup(html, "html.parser")
        refs = {}
        for a in soup.find_all("a", href=True):
            m = re.search(r"/soccer-stats/teams/([^/]+)/([0-9]+)/", a["href"])
            if m:
                refs[norm(a.get_text(" ", strip=True))] = (m.group(1), m.group(2))
    except Exception as e:
        print("BetStudy fixtures warning:", e)
        return out

    aliases = {"braga":"sporting braga","fc porto":"porto","man united":"manchester united","man city":"manchester city","lask":"lask linz","barcelona":"fc barcelona","vitoria de guimaraes":"vitoria sc","as roma":"roma"}
    cache = {}

    def parse(html, opponent):
        soup = BeautifulSoup(html, "html.parser")
        rows, seen = [], set()
        date_re = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
        for node in soup.find_all(string=date_re):
            p, txt = node.parent, ""
            for _ in range(7):
                if p is None: break
                txt = " | ".join(x.strip() for x in p.stripped_strings if x.strip())
                if re.search(r"\d{1,2}\s*-\s*\d{1,2}", txt): break
                p = p.parent
            m = re.search(r"(\d{2}\.\d{2}\.\d{4}).*?([^|]+?)\s*\|\s*(?:[PE]\s*)?(\d{1,2})\s*-\s*(\d{1,2})\s*\|\s*([^|]+?)\s*\|\s*([^|]+)", txt)
            if not m: continue
            d = datetime.strptime(m.group(1), "%d.%m.%Y").strftime("%Y-%m-%d")
            h, a = m.group(2).strip(), m.group(5).strip()
            if {norm(h), norm(a)} != {norm("Sporting CP"), norm(opponent)}: continue
            key = (d, norm(h), norm(a), m.group(3), m.group(4))
            if key in seen: continue
            seen.add(key)
            rows.append({"id":"betstudy:"+d+":"+norm(h)+":"+norm(a), "date":d, "home":h, "away":a, "home_score":int(m.group(3)), "away_score":int(m.group(4)), "competition":m.group(6).strip()})
        return sorted(rows, key=lambda x:x["date"], reverse=True)

    for f in upcoming:
        fid = str(f["id"])
        home = (f.get("home") or {}).get("name","")
        away = (f.get("away") or {}).get("name","")
        opponent = away if norm(home) == norm("Sporting CP") else home
        key = norm(opponent)
        ref = refs.get(key) or refs.get(norm(aliases.get(key,"")))
        if not ref:
            out[fid] = {"matches": [], "summary": [0,0,0]}
            print("BetStudy H2H ref missing:", opponent)
            continue
        if ref not in cache:
            slug, oid = ref
            url = f"https://www.betstudy.com/pt/h2h/sporting-cp-{slug}-58-{oid}/"
            try:
                rr = requests.get(url, headers=BASE_HEADERS, timeout=40)
                rr.raise_for_status()
                rows = parse(rr.text, opponent)
                hw = dw = aw = 0
                for x in rows:
                    if x["home_score"] == x["away_score"]: dw += 1
                    elif norm(x["home"]) == norm("Sporting CP"): hw += 1
                    else: aw += 1
                cache[ref] = {"matches": rows, "summary": [hw,dw,aw]}
                print("BetStudy H2H OK:", opponent, len(rows))
            except Exception as e:
                print("BetStudy H2H warning:", opponent, e)
                cache[ref] = {"matches": [], "summary": [0,0,0]}
            time.sleep(0.5)
        out[fid] = cache[ref]
    return out
