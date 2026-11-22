"""Testes funcionais do Lion's Den (Playwright).

Uso:
  BASE_URL=https://lionsdenscp.web.app python tests/functional_test.py
  BASE_URL=http://127.0.0.1:8000 CHROME=/usr/bin/google-chrome python tests/functional_test.py

Percorre os ecrãs, abre todos os jogos (passados e futuros), valida os dados
e o comportamento da janela de detalhes, em telemóvel e computador, para a
versão principal (/) e a versão app (/app/).
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "https://lionsdenscp.web.app").rstrip("/")
CHROME = os.environ.get("CHROME") or None
OFFLINE_CDN = os.environ.get("BLOCK_EXTERNAL") == "1"  # ambiente sem internet

results = []


def check(name, ok, detail="", warn=False):
    status = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append((bool(ok) or warn, name, detail, status))
    print(status + " " + name + (f" — {detail}" if detail else ""), flush=True)


def is_friendly(f):
    c = (f.get("comp") or "").lower()
    return "friend" in c or "amig" in c or "particular" in c


def run_variant(browser, path, label, viewport, mobile):
    ctx = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile, locale="pt-PT",
                              timezone_id="Europe/Lisbon")
    page = ctx.new_page()
    js_errors, failed_data = [], []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("response", lambda r: failed_data.append(f"{r.status} {r.url}") if "/data/" in r.url and r.status >= 400 else None)
    if OFFLINE_CDN:
        page.route("**/*", lambda route: route.abort() if not route.request.url.startswith(BASE) else route.continue_())

    t0 = time.time()
    page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function("document.getElementById('nextGame') && document.getElementById('nextGame').innerText.trim().length>10", timeout=30000)
        ready = time.time() - t0
    except Exception:
        ready = None
    check(f"[{label}] página inicial carrega e mostra o próximo jogo", ready is not None,
          f"{ready:.1f}s até estar pronta" if ready else "o cartão do próximo jogo ficou vazio")

    # Dados
    data = page.evaluate("Object.fromEntries(Object.entries(D).map(([k,v])=>[k, v && typeof v==='object']))")
    expected = page.evaluate("files")
    missing = [f for f in expected if not data.get(f)]
    check(f"[{label}] todos os ficheiros de dados carregam", not missing and not failed_data,
          ", ".join(missing + failed_data) or f"{len(expected)} ficheiros")

    # Ecrãs
    screens = page.evaluate("[...document.querySelectorAll('.screen')].map(s=>s.id)")
    empty = []
    for sid in screens:
        page.evaluate(f"go('{sid}')")
        page.wait_for_timeout(250)
        active = page.evaluate(f"document.getElementById('{sid}').classList.contains('active')")
        text = page.evaluate(f"document.getElementById('{sid}').innerText.trim().length")
        if not active or text < 20:
            empty.append(f"{sid}({'inativo' if not active else 'vazio'})")
    check(f"[{label}] todos os ecrãs abrem com conteúdo ({len(screens)})", not empty, ", ".join(empty))

    # Sem scroll horizontal no telemóvel
    if mobile:
        overflow = []
        for sid in screens:
            page.evaluate(f"go('{sid}')")
            page.wait_for_timeout(150)
            w = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            if w[0] > w[1] + 2:
                overflow.append(f"{sid}:{w[0]}>{w[1]}")
        check(f"[{label}] sem deslocamento horizontal em nenhum ecrã", not overflow, ", ".join(overflow))

    # Jogos: listas
    page.evaluate("go('games')")
    page.evaluate("setGamesTab('upcoming')")
    up = page.evaluate("document.querySelectorAll('#gamesList [data-game-id], #gamesList [onclick*=openGame]').length")
    page.evaluate("setGamesTab('past')")
    past_n = page.evaluate("document.querySelectorAll('#gamesList [data-game-id], #gamesList [onclick*=openGame]').length")
    check(f"[{label}] lista de jogos (próximos {up}, terminados {past_n})", up > 0 and past_n > 0)

    # Abrir todos os jogos
    fixtures = page.evaluate("allFixtures().map(f=>({id:String(f.id),past:!isFuture(f),h:f.home?.name,a:f.away?.name,comp:f.competition?.name||''}))")
    broken, no_ref, no_xi, stacked, no_stats, slow = [], [], [], [], [], []
    for f in fixtures:
        errs_before = len(js_errors)
        t1 = time.time()
        page.evaluate(f"openGame('{f['id']}')")
        page.wait_for_timeout(120)
        dt = time.time() - t1
        info = page.evaluate("""()=>{const b=document.getElementById('modalBody'),t=document.getElementById('modalTitle');
          const txt=b?b.innerText:'';const pitches=[...b.querySelectorAll('.lineup-pitch')];const pts=pitches.flatMap((pp,i)=>[...pp.querySelectorAll('.pitch-player')].map(p=>i+':'+p.style.left+'|'+p.style.top));
          const ref=[...b.querySelectorAll('.game-info-row')].map(r=>r.innerText).find(x=>/Árbitro/i.test(x))||'';
          return {open:document.getElementById('modal').classList.contains('open'),title:t?t.textContent:'',txt:txt.slice(0,20000),
            pitch:pts.length,distinct:new Set(pts).size,list:b.querySelectorAll('.lineup-list-row').length,
            ref:ref,stats:b.querySelectorAll('.game-stats-row').length}}""")
        name = f"{f['h']} x {f['a']}" + (" (particular)" if is_friendly(f) else "")
        if not info["open"] or "Detalhes indisponíveis" in info["txt"] or len(js_errors) > errs_before:
            broken.append(name + (f" ({js_errors[-1][:80]})" if len(js_errors) > errs_before else ""))
        if dt > 1.5:
            slow.append(f"{name} {dt:.1f}s")
        if f["past"]:
            if "Por confirmar" in info["ref"] or not info["ref"]:
                no_ref.append(name)
            if info["pitch"] == 0 and info["list"] == 0:
                no_xi.append(name)
            if info["pitch"] and info["distinct"] < min(info["pitch"], 18):
                stacked.append(f"{name} ({info['distinct']}/{info['pitch']} posições)")
            if info["stats"] == 0:
                no_stats.append(name)
        page.evaluate("closeModal()")
    n_past = sum(1 for f in fixtures if f["past"])
    check(f"[{label}] todos os {len(fixtures)} jogos abrem sem erros", not broken, "; ".join(broken))
    check(f"[{label}] janela de jogo abre em menos de 1,5 s", not slow, "; ".join(slow))
    check(f"[{label}] jogadores nunca empilhados no campo", not stacked, "; ".join(stacked))
    for title, miss in ((f"11 inicial presente nos {n_past} jogos terminados", no_xi),
                        ("árbitro presente nos jogos terminados", no_ref),
                        ("estatísticas presentes nos jogos terminados", no_stats)):
        official = [m for m in miss if "(particular)" not in m]
        friendlies = [m for m in miss if "(particular)" in m]
        check(f"[{label}] {title}", not official, "em falta: " + "; ".join(official) if official else "")
        if friendlies:
            check(f"[{label}] {title} — particulares", False, "em falta nas fontes: " + "; ".join(friendlies), warn=True)

    # Casos concretos
    def open_match(match_text):
        fid = next((f["id"] for f in fixtures if match_text in (f["h"] + " x " + f["a"])), None)
        if fid:
            page.evaluate(f"openGame('{fid}')")
            page.wait_for_timeout(200)
        return fid

    if open_match("União Serra"):
        txt = page.inner_text("#modalBody")
        low = txt.lower()
        check(f"[{label}] Sporting x União Serra: árbitro Diogo Rosa", "Diogo Rosa" in txt)
        check(f"[{label}] Sporting x União Serra: homem do jogo Irankunda", "homem do jogo" in low and "Irankunda" in txt)
        check(f"[{label}] Sporting x União Serra: estatísticas (posse 72%)", "72%" in txt and "Posse de bola" in txt)
        check(f"[{label}] Sporting x União Serra: onze em lista (22 jogadores)", page.locator("#modalBody .lineup-list-row").count() == 22)
        check(f"[{label}] Sporting x União Serra: nomes curtos (sem nomes completos)", "Willian Largura Garcia Costa" not in txt and "Willian Costa" in txt)
        check(f"[{label}] Sporting x União Serra: substituições com entrada/saída corretas",
              "↓ Nestory Irankunda · ↑ Jesse Derry" in txt and "↓ Luis Guilherme · ↑ Geny Catamo" in txt)
        page.evaluate("closeModal()")
    if open_match("Estrela"):
        n = page.locator("#modalBody .pitch-player").count()
        check(f"[{label}] Estrela x Sporting: tática desenhada (22 jogadores no campo)", n == 22, f"{n} no campo")
        txt = page.inner_text("#modalBody")
        check(f"[{label}] Estrela x Sporting: Suárez entra (não sai) aos 61'", "↓ Fotis Ioannidis · ↑ Luis Suárez" in txt)
        page.evaluate("closeModal()")

    # Fechar janela
    closed = page.evaluate("!document.getElementById('modal').classList.contains('open')")
    check(f"[{label}] a janela de detalhes fecha", closed)

    # Plantel: abrir um jogador
    page.evaluate("go('squad')")
    page.wait_for_timeout(200)
    players = page.locator("#squadList [onclick*=openPlayer]")
    if players.count():
        players.first.click()
        page.wait_for_timeout(300)
        ok = page.evaluate("document.getElementById('modal').classList.contains('open') && document.getElementById('modalBody').innerText.length>30")
        check(f"[{label}] plantel: abrir ficha de jogador ({players.count()} jogadores)", ok)
        page.evaluate("closeModal()")
    else:
        check(f"[{label}] plantel: lista de jogadores", False, "sem jogadores clicáveis")

    # Notícias
    page.evaluate("go('news')")
    page.wait_for_timeout(200)
    news_n = page.evaluate("document.querySelectorAll('#newsList > *').length")
    check(f"[{label}] notícias listadas", news_n > 0, f"{news_n} itens")

    # Classificação
    page.evaluate("go('classification')")
    page.wait_for_timeout(200)
    rows = page.evaluate("document.getElementById('classificationList').innerText")
    check(f"[{label}] classificação com o Sporting", "Sporting" in rows)


    # Modalidades
    page.evaluate("go('modalidades')")
    page.wait_for_timeout(300)
    sports = page.evaluate("[...document.querySelectorAll('#modFilters .news-filter')].map(b=>b.textContent.trim())")
    check(f"[{label}] modalidades: filtros por modalidade", len(sports) >= 4, ", ".join(sports))
    keys = page.evaluate("(D['modalidades.json']?.sports||[]).map(s=>s.key)")
    bad = []
    for k in ["all"] + keys:
        for tab in ("calendar", "results", "news", "table"):
            before = len(js_errors)
            page.evaluate(f"setModSport('{k}');setModTab('{tab}')")
            page.wait_for_timeout(60)
            txt = page.evaluate("document.getElementById('modContent').innerText.trim().length")
            if len(js_errors) > before or txt < 10:
                bad.append(f"{k}/{tab}")
    check(f"[{label}] modalidades: {len(keys)} modalidades × separadores sem erros", not bad, ", ".join(bad))
    page.evaluate("setModSport('all');setModTab('calendar')")
    n_ev = page.locator("#modContent .mod-event").count()
    football = page.evaluate("[...document.querySelectorAll('#modContent .mod-chip')].filter(c=>/futebol(?! de praia)/i.test(c.textContent)).length")
    check(f"[{label}] modalidades: calendário com jogos e sem futebol", n_ev > 0 and football == 0, f"{n_ev} jogos")
    page.evaluate("setModTab('results')")
    check(f"[{label}] modalidades: resultados", page.locator("#modContent .mod-result").count() > 0)
    page.evaluate("setModTab('news')")
    check(f"[{label}] modalidades: notícias", page.locator("#modContent .news-card").count() > 0)
    page.evaluate("go('home')")
    check(f"[{label}] início: bloco Modalidades com próximos jogos", page.locator("#homeModalidades .mod-home-row").count() > 0)
    n_today = page.evaluate("(()=>{const t=modDayKey(new Date().toISOString());const d=modData();return (d.sports||[]).flatMap(s=>(s.fixtures||[]).concat(s.results||[])).filter(x=>modDayKey(x.date)===t).length})()")
    shown = page.locator("#sportingDay .mod-home-row").count()
    check(f"[{label}] início: Dia de Sporting {'visível' if n_today else 'escondido'} ({shown} jogos)", (shown > 0) == (n_today > 0))
    real_errors = [e for e in js_errors if "tailwind" not in e.lower()]
    check(f"[{label}] sem erros de JavaScript", not real_errors, " | ".join(real_errors[:3]))
    ctx.close()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        for path, name in (("/", "site"), ("/app/", "app")):
            run_variant(browser, path, f"{name} · telemóvel", {"width": 390, "height": 844}, True)
            run_variant(browser, path, f"{name} · computador", {"width": 1366, "height": 900}, False)
        browser.close()
    failed = [r for r in results if r[3] == "FAIL"]
    warns = [r for r in results if r[3] == "WARN"]
    print(f"\nRESUMO: {len(results) - len(failed) - len(warns)} passaram, {len(warns)} avisos, {len(failed)} falharam (total {len(results)}).")
    with open(os.environ.get("REPORT", "functional-report.json"), "w", encoding="utf-8") as fh:
        json.dump([{"status": st, "test": n, "detail": d} for ok, n, d, st in results], fh, ensure_ascii=False, indent=1)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
