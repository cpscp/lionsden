"""
Vai buscar dados reais do Sporting CP e grava-os em /data/*.json.
Corre localmente com `python scripts/fetch_data.py` ou via GitHub Actions (agendado).

Precisa de uma variável de ambiente FOOTBALL_DATA_TOKEN.
Cria uma conta grátis em https://www.football-data.org/client/register
(o plano gratuito inclui a Primeira Liga portuguesa, código de competição "PPL").
"""

import os
import json
import sys
from datetime import datetime, timezone

import requests

TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")
if not TOKEN:
    sys.exit("Falta a variável de ambiente FOOTBALL_DATA_TOKEN. Ver README.md.")

BASE = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": TOKEN}
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)


def get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def save(name, payload):
    payload["_updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"gravado {name}")


def find_sporting_team_id():
    """A Primeira Liga tem o código PPL. Procuramos o Sporting pelo nome
    em vez de assumir um ID fixo, para não depender de um número que pode
    mudar ou estar errado."""
    teams = get("/competitions/PPL/teams")["teams"]
    for t in teams:
        if "sporting" in t["name"].lower() and "braga" not in t["name"].lower():
            return t["id"], t["name"]
    sys.exit("Não encontrei o Sporting CP na lista de equipas da PPL.")


def main():
    team_id, team_name = find_sporting_team_id()
    print(f"Sporting encontrado: {team_name} (id {team_id})")

    # Classificação da Liga Portugal
    standings = get("/competitions/PPL/standings")
    save("standings.json", {
        "competition": "Primeira Liga",
        "table": standings["standings"][0]["table"],
    })

    # Plantel
    team = get(f"/teams/{team_id}")
    save("squad.json", {
        "team": team_name,
        "crest": team.get("crest"),
        "coach": (team.get("coach") or {}).get("name"),
        "squad": [
            {
                "name": p["name"],
                "position": p.get("position"),
                "nationality": p.get("nationality"),
                "dateOfBirth": p.get("dateOfBirth"),
                "shirtNumber": p.get("shirtNumber"),
            }
            for p in team.get("squad", [])
        ],
    })
    # Nota: football-data.org (plano grátis) não dá estatísticas por jogador
    # (golos/assistências). Para isso precisas de uma API como API-Football
    # (RapidAPI) — o mesmo script pode ser estendido com outra função aqui.

    # Últimos e próximos jogos
    matches = get(f"/teams/{team_id}/matches", params={"limit": 20})["matches"]
    save("fixtures.json", {
        "team": team_name,
        "matches": [
            {
                "utcDate": m["utcDate"],
                "status": m["status"],  # SCHEDULED, LIVE, IN_PLAY, PAUSED, FINISHED
                "matchday": m.get("matchday"),
                "competition": m["competition"]["name"],
                "venue": m.get("venue"),
                "referees": [r["name"] for r in m.get("referees", []) if r.get("name")],
                "homeTeam": m["homeTeam"]["name"],
                "awayTeam": m["awayTeam"]["name"],
                "score": m["score"]["fullTime"],
                "minute": m.get("minute"),
            }
            for m in matches
        ],
    })

    print("Concluído.")


if __name__ == "__main__":
    main()
