import re, json, time, requests
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

HEAD={"User-Agent":"Mozilla/5.0 LionsDen/1.0","Accept-Language":"pt-PT,pt;q=0.9,en;q=0.8"}
SPORTING_ID="13dc44fd"

def norm(v):
    return re.sub(r"[^a-z0-9]+","",str(v or "").lower())

def fetch_fbref_h2h(upcoming):
    out={}
    s=requests.Session(); s.headers.update(HEAD)
    try:
        html=s.get("https://fbref.com/en/squads/13dc44fd/2026-2027/matchlogs/all_comps/schedule/Sporting-CP-Scores-and-Fixtures-All-Competitions",timeout=40).text
        soup=BeautifulSoup(html,"html.parser")
    except Exception as e:
        print("FBref H2H schedule warning:",e); return out
    refs={}
    for a in soup.find_all("a",href=True):
        m=re.search(r"/squads/([0-9a-f]{8})/[^/]+/[^/]+/[^/]+/([^/]+)",a["href"])
        if m: refs[norm(a.get_text(" ",strip=True))]=(m.group(1),m.group(2))
    for f in upcoming:
        fid=str(f["id"]); home=(f.get("home") or {}).get("name",""); away=(f.get("away") or {}).get("name","")
        opp=away if norm(home)==norm("Sporting CP") else home
        ref=refs.get(norm(opp))
        if not ref:
            out[fid]={"matches":[],"summary":[0,0,0]}; continue
        tid,slug=ref
        url=f"https://fbref.com/en/stathead/matchup/teams/{SPORTING_ID}/{tid}/Sporting-CP-vs-{slug}-History"
        try:
            rr=s.get(url,timeout=40); rr.raise_for_status()
            soup2=BeautifulSoup(rr.text,"html.parser"); rows=[]; seen=set()
            for tr in soup2.select("table tbody tr"):
                cells=[c.get_text(" ",strip=True) for c in tr.find_all(["th","td"])]
                if len(cells)<8: continue
                date=next((x for x in cells if re.fullmatch(r"\d{4}-\d{2}-\d{2}",x)),None)
                score=next((x for x in cells if re.search(r"\d+[-–]\d+",x)),None)
                if not date or not score or len(cells)<6: continue
                home2=next((x for x in cells if x and x not in {"Home","Away","Score"} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}",x) and not re.search(r"\d+[-–]\d+",x)),None)
                # Prefer the explicit Home/Score/Away columns.
                try:
                    hi=cells.index("Home"); home2=cells[hi+1]; score=cells[hi+2]; away2=cells[hi+3]
                except Exception:
                    continue
                m=re.match(r"\s*(\d+)\s*[–-]\s*(\d+)",score)
                if not m: continue
                key=(date,norm(home2),norm(away2),m.group(1),m.group(2))
                if key in seen: continue
                seen.add(key)
                rows.append({"id":"fbref:"+date+":"+norm(home2)+":"+norm(away2),"date":date,"home":home2,"away":away2,"home_score":int(m.group(1)),"away_score":int(m.group(2)),"competition":cells[0] if cells else ""})
            rows.sort(key=lambda x:x["date"],reverse=True)
            hw=dw=aw=0
            for x in rows:
                if x["home_score"]==x["away_score"]: dw+=1
                elif norm(x["home"])==norm("Sporting CP"): hw+=1
                else: aw+=1
            out[fid]={"matches":rows[:4],"summary":[hw,dw,aw]}
            print("FBref H2H OK:",opp,len(rows))
        except Exception as e:
            print("FBref H2H warning:",opp,e); out[fid]={"matches":[],"summary":[0,0,0]}
        time.sleep(.4)
    return out
