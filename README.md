# Lion's Den

App de adeptos do Sporting CP: frontend estático (GitHub Pages) + dados reais
atualizados automaticamente por um GitHub Action agendado.

## Como funciona (importante perceber isto primeiro)

O GitHub Pages **só serve ficheiros** — não corre um servidor com o teu código.
Por isso não há aqui um "backend" no sentido clássico (nada fica sempre ligado
à espera de pedidos). Em vez disso:

1. Um **GitHub Action** corre a cada ~30 minutos (ou quando o acionares
   manualmente), executa `scripts/fetch_data.py`, que vai buscar dados reais
   à API [football-data.org](https://www.football-data.org) (classificação,
   plantel, calendário) e grava-os em `data/*.json`.
2. Esse Action faz **commit** dos ficheiros JSON atualizados de volta no
   repositório.
3. O `index.html` (a app) é servido pelo GitHub Pages e lê esses `data/*.json`
   diretamente — sem chamar nenhuma API externa a partir do browser.

**Limitação honesta:** isto não é "ao segundo" durante um jogo. O intervalo
mínimo garantido pelo GitHub Actions é de 5 em 5 minutos, e em picos de
utilização pode atrasar mais. Para minuto-a-minuto verdadeiramente ao vivo
precisarias de um servidor sempre ligado (ver secção "Ir mais longe" abaixo).
Para classificação, plantel e calendário isto é perfeitamente suficiente.

## Passo a passo

### 1. Cria o repositório
No GitHub, cria um repositório novo (público — Pages grátis exige isso, a
menos que tenhas GitHub Pro/Team) chamado, por exemplo, `lions-den`.
Copia todos os ficheiros desta pasta para lá (ou faz upload pelo browser em
"Add file → Upload files").

### 2. Cria a tua chave da API (grátis)
Regista-te em <https://www.football-data.org/client/register> — recebes um
token por email. O plano gratuito inclui a Primeira Liga portuguesa
(código `PPL`), com limite de 10 pedidos/minuto — mais do que suficiente
para uma atualização a cada 30 min.

### 3. Guarda a chave como "secret" no GitHub
No repositório: **Settings → Secrets and variables → Actions → New repository
secret**
- Nome: `FOOTBALL_DATA_TOKEN`
- Valor: o token que recebeste por email

### 4. Ativa o GitHub Pages
**Settings → Pages → Build and deployment → Source: "Deploy from a branch"
→ Branch: `main` / pasta `/(root)` → Save.**
Ao fim de 1–2 minutos a app fica disponível em:
`https://<o-teu-utilizador>.github.io/lions-den/`

### 5. Corre o Action pela primeira vez
**Separador "Actions" → "Atualizar dados do Sporting" → "Run workflow".**
Isto cria os ficheiros em `data/` pela primeira vez (antes disso a app mostra
"sem dados ainda"). A partir daqui, corre sozinho a cada 30 minutos.

## Estrutura

```
index.html                        → a app (frontend)
data/*.json                       → dados gerados automaticamente (não editar à mão)
scripts/fetch_data.py             → vai buscar os dados reais
.github/workflows/update-data.yml → agenda e executa o script, faz commit
```

## O que falta / próximos passos possíveis

- **Estatísticas por jogador** (golos, assistências): o plano gratuito da
  football-data.org não as dá. Precisas de uma API como API-Football
  (RapidAPI) — o `fetch_data.py` está escrito para ser fácil de estender com
  outra função `get_player_stats()`.
- **Notícias**: não incluí scraping automático porque não confirmei URLs de
  RSS válidos para Record/A Bola a partir daqui. Se encontrares o feed RSS
  desses sites (normalmente visível no código-fonte da página como
  `<link type="application/rss+xml">`), diz-me o URL e acrescento a função de
  notícias ao script (usando a biblioteca `feedparser`).
- **Ao vivo ao segundo**: precisarias de um pequeno servidor sempre ligado
  (ex.: um Cloudflare Worker gratuito) que o browser consulta diretamente a
  cada 15–30 segundos durante um jogo. Não é preciso para já — mas é o passo
  seguinte lógico se quiseres isso.
