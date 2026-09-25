# Lion's Den — versão gratuita corrigida

## O que foi corrigido
- Compatibilidade entre as estruturas dos JSON e o frontend.
- Classificação sem `[object Object]` / `undefined`.
- Próximo jogo usa `fixtures.json` + fallback FBref.
- Jogos usam o calendário completo do Sporting/FBref.
- Plantel aceita `players` e `squad`.
- Estatísticas individuais e da equipa continuam a vir do FBref.
- Primeira Liga usa o endpoint atual do FBref, não a época 2025.
- Calendário da Primeira Liga é usado para enriquecer estádio, árbitro e assistência.
- API Football Soccer continua opcional e fica apenas como fonte complementar.
- Notícias continuam independentes do pipeline de futebol.

## Instalação
Substituir no repositório:
- `index.html`
- `scripts/fetch_data.py`
- `scripts/requirements.txt`
- `.github/workflows/update-football.yml`
- `.github/workflows/update-news.yml`

Manter a pasta `data/`.

Depois fazer commit + push e executar `Actions → Atualizar futebol e estatísticas`.
