# Lion's Den — Sporting CP

Versão **€0** da app, pensada para GitHub Pages.

## Fontes

- Football Soccer API: fixtures atuais/próximos, estádio, coordenadas e árbitro.
- FBref: classificação da Primeira Liga, plantel e estatísticas 2026/27.
- Sporting.pt + Google News RSS: notícias.
- OpenStreetMap/Nominatim: mapa/localização do estádio.

A chave de futebol fica apenas no GitHub Actions.

## Configuração

1. Cria uma chave gratuita no Football Soccer API.
2. GitHub → Settings → Secrets and variables → Actions.
3. Cria:
   `FSAPI_KEY`
4. Substitui o conteúdo do teu repositório por esta versão.
5. Mantém `main` + `/ (root)` em GitHub Pages.
6. Vai a Actions → **Atualizar futebol e estatísticas** → Run workflow.
7. Vai a Actions → **Atualizar notícias** → Run workflow.

## Atualização

- Futebol/estatísticas: a cada 6 horas.
- Notícias: duas vezes por hora.
- Também podes executar ambos manualmente.

## Nota sobre dados

A camada gratuita do Football Soccer API é limitada a dados recentes/próximos e não inclui estatísticas/standings do arquivo. Por isso o projeto usa FBref para estatísticas e classificação, e a API gratuita apenas onde ela é forte: dados atuais/próximos de jogos, estádio e árbitro.

Se uma fonte externa estiver temporariamente indisponível, o workflow não apaga automaticamente os JSON anteriores.
