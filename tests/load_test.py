"""Teste de carga leve do Lion's Den.

Simula visitas completas (página + todos os ficheiros de dados, com gzip) com
utilizadores em simultâneo, em patamares crescentes. Tem um teto de tráfego
(MAX_MB) para não esgotar a quota diária gratuita do Firebase Hosting (~360 MB/dia).

Uso:
  BASE_URL=https://lionsdenscp.web.app STAGES=5,15,30 VISITS=4 MAX_MB=60 python tests/load_test.py
"""
import gzip
import json
import os
import re
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "https://lionsdenscp.web.app").rstrip("/")
STAGES = [int(x) for x in os.environ.get("STAGES", "5,15,30").split(",")]
VISITS = int(os.environ.get("VISITS", "4"))  # visitas por utilizador em cada patamar
MAX_BYTES = float(os.environ.get("MAX_MB", "60")) * 1024 * 1024
UA = "LionsDen-LoadTest/1.0"

lock = threading.Lock()
total_bytes = 0


def fetch(url):
    global total_bytes
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Cache-Control": "no-cache"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = b"", e.code
    except Exception:
        body, status = b"", 0
    dt = time.perf_counter() - t0
    with lock:
        total_bytes += len(body)
    return status, dt, len(body)


def data_files():
    status, _, _ = fetch(BASE + "/")
    req = urllib.request.Request(BASE + "/", headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    try:
        html = gzip.decompress(raw).decode("utf-8", "ignore")
    except OSError:
        html = raw.decode("utf-8", "ignore")
    m = re.search(r"const files=\[(.*?)\]", html)
    return re.findall(r"'([^']+\.json)'", m.group(1)) if m else []


def visit(files):
    """Uma visita: página primeiro, depois os dados em paralelo (como o browser)."""
    t0 = time.perf_counter()
    results = [("/", *fetch(BASE + "/"))]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f, res in zip(files, ex.map(lambda f: fetch(f"{BASE}/data/{f}?t={time.time()}"), files)):
            results.append((f, *res))
    return time.perf_counter() - t0, results


def pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))
    return values[k]


def main():
    files = data_files()
    print(f"Alvo: {BASE} · {len(files)} ficheiros de dados por visita · teto {MAX_BYTES/1048576:.0f} MB", flush=True)
    report = []
    for users in STAGES:
        with lock:
            if total_bytes >= MAX_BYTES:
                print("Teto de tráfego atingido; patamares seguintes ignorados.")
                break
        visit_times, req_times, errors, n_req = [], [], 0, 0
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=users) as ex:
            futures = [ex.submit(visit, files) for _ in range(users * VISITS)]
            for fu in futures:
                vt, res = fu.result()
                visit_times.append(vt)
                for _, st, dt, _n in res:
                    n_req += 1
                    req_times.append(dt)
                    if st != 200:
                        errors += 1
        wall = time.perf_counter() - t0
        row = {
            "utilizadores": users, "visitas": len(visit_times), "pedidos": n_req, "erros": errors,
            "visita_p50_s": round(pct(visit_times, 50), 2), "visita_p95_s": round(pct(visit_times, 95), 2),
            "pedido_p50_ms": round(pct(req_times, 50) * 1000), "pedido_p95_ms": round(pct(req_times, 95) * 1000),
            "pedido_max_ms": round(max(req_times) * 1000), "pedidos_por_s": round(n_req / wall, 1),
        }
        report.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(f"Tráfego total: {total_bytes/1048576:.1f} MB")
    with open(os.environ.get("REPORT", "load-report.json"), "w", encoding="utf-8") as fh:
        json.dump({"base": BASE, "mb": round(total_bytes / 1048576, 1), "stages": report}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
