# Lion's Den

App não oficial de adeptos do Sporting CP, feita para GitHub Pages e sem custos de APIs pagas.

## Fonte de futebol

A app usa **API-Football (plano gratuito)** para fixtures, classificação, plantel, estatísticas de jogadores/equipa, árbitros, estádios e detalhes de jogo. O plano gratuito disponibiliza 100 pedidos/dia e todos os endpoints; por isso o workflow foi desenhado para cachear os dados no repositório em vez de chamar a API a partir do browser.

## Outras fontes gratuitas

- Notícias: Sporting.pt + Google News RSS.
- Mapas: OpenStreetMap + Nominatim, com cache local.
- Frontend: GitHub Pages.

## Configuração

1. Cria uma conta gratuita em https://dashboard.api-football.com/register.
2. Em **Settings → Secrets and variables → Actions**, cria o secret:
   - `API_FOOTBALL_KEY` = a tua chave API-Football.
3. Em **Settings → Pages**, usa `Deploy from a branch`, `main`, `/(root)`.
4. Em **Actions**, executa `Atualizar dados do Sporting` com `full` uma primeira vez.

## Quota

O free tier tem 100 pedidos/dia. O workflow usa uma atualização core a cada 30 minutos e uma atualização completa a cada 6 horas. Os detalhes de jogos ao vivo só são pedidos quando existe um jogo ao vivo, para evitar desperdiçar quota.

## Estrutura

- `index.html` — app inteira.
- `data/*.json` — cache público consumido pelo frontend.
- `scripts/fetch_data.py` — pipeline de dados.
- `.github/workflows/update-data.yml` — automação.
