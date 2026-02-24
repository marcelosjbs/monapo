import os
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests

BASE = "https://www.in.gov.br"
LEITURAJORNAL = BASE + "/leiturajornal"

ARTIGO_PREFIXES = [
    BASE + "/en/web/dou/-/",
    BASE + "/web/dou/-/",
]

# Aceita nº / no / n° (°)
RE_SIAPE = re.compile(r"matr[ií]cula\s+SIAPE\s+n[ºo°]\s*([\d\.]+)", re.IGNORECASE)
RE_NOME = re.compile(
    r"\b([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]+?)\s*,\s*matr[ií]cula\s+SIAPE\b",
    re.IGNORECASE,
)

# Captura urlTitle dentro do jsonArray (quando existe)
RE_JSON_SCRIPT = re.compile(
    r'<script[^>]+type="application/json"[^>]*>\s*(\{.*?\})\s*</script>',
    re.DOTALL
)

# Fallback: captura links diretos de artigo no HTML
RE_LINK_ARTIGO = re.compile(r'href="(/(?:en/)?web/dou/-/[^"]+)"', re.IGNORECASE)

# Detecta paginação
RE_HAS_NEXT = re.compile(r'Próximo\s*&gt;&gt;|Próximo\s*»|Próximo\s*>>', re.IGNORECASE)

# Filtros “PF/MJSP” para reduzir varredura
NEEDLE_ITEM_ANY = [
    "polícia federal",
    "dgp/pf",
    "portaria dgp/pf",
    "ministério da justiça",
    "segurança pública",
    "mjsp",
]

def br_today_str() -> str:
    tz = ZoneInfo("America/Sao_Paulo")
    return datetime.now(tz=tz).strftime("%d-%m-%Y")

def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Referer": BASE + "/",
        "Upgrade-Insecure-Requests": "1",
    })
    return s

def http_get(session: requests.Session, url: str, timeout=40) -> str:
    last_err = None
    for attempt in range(7):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code in (403, 429, 502, 503):
                time.sleep(2.0 + attempt * 1.3)
                last_err = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(1.2 + attempt * 1.0)
    raise last_err

def clean_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()

def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def extract_json_array(html: str):
    m = RE_JSON_SCRIPT.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        arr = data.get("jsonArray", [])
        return arr if isinstance(arr, list) else []
    except Exception:
        return []

def item_matches_pf(item: dict) -> bool:
    blob = json.dumps(item, ensure_ascii=False).lower()
    return any(k in blob for k in NEEDLE_ITEM_ANY)

def build_leiturajornal_url(date_str: str, page: int, org: str | None, org_sub: str | None) -> str:
    params = {"secao": "dou2", "data": date_str}
    # Se você quiser manter filtro por órgão/subórgão, deixe setado.
    # Mas se ele esconder itens, desative (org=None/org_sub=None).
    if org:
        params["org"] = org
    if org_sub:
        params["org_sub"] = org_sub
    # parâmetro de paginação (na prática costuma ser "pagina")
    params["pagina"] = str(page)
    return f"{LEITURAJORNAL}?{urlencode(params)}"

def fetch_all_targets(session: requests.Session, date_str: str, org: str | None, org_sub: str | None, max_pages: int = 60):
    """
    Busca todas as páginas do leiturajornal (paginado) e retorna:
    - urlTitles (quando houver jsonArray)
    - ou links /web/dou/-/... (fallback)
    """
    all_targets = []
    mode_used = None

    for page in range(1, max_pages + 1):
        url = build_leiturajornal_url(date_str, page, org, org_sub)
        print(f"[INFO] Página {page}: {url}")

        html = http_get(session, url)

        items = extract_json_array(html)
        if items:
            mode_used = mode_used or "jsonArray"
            url_titles = []
            for it in items:
                if isinstance(it, dict) and it.get("urlTitle") and item_matches_pf(it):
                    url_titles.append(it["urlTitle"])
            url_titles = dedupe(url_titles)
            all_targets.extend(url_titles)
        else:
            # fallback por links do HTML (sem metadata do item)
            mode_used = mode_used or "html_links"
            links = RE_LINK_ARTIGO.findall(html)
            links = dedupe(links)
            all_targets.extend(links)

        # condição de parada: se não tiver “Próximo” e já passamos da 1ª página
        has_next = bool(RE_HAS_NEXT.search(html))
        if not has_next:
            print(f"[INFO] Sem 'Próximo' na página {page}. Parando.")
            break

        # pausa pequena para não tomar rate-limit
        time.sleep(0.4)

    return dedupe(all_targets), (mode_used or "unknown")

def fetch_article(session: requests.Session, target: str):
    """
    target pode ser:
    - urlTitle (sem barras)
    - path "/en/web/dou/-/..." ou "/web/dou/-/..."
    - URL completa
    """
    if target.startswith("http"):
        html = http_get(session, target)
        return target, clean_text(html)

    if target.startswith("/"):
        url = BASE + target
        html = http_get(session, url)
        return url, clean_text(html)

    for prefix in ARTIGO_PREFIXES:
        url = prefix + target
        try:
            html = http_get(session, url)
            return url, clean_text(html)
        except Exception:
            continue
    return None, None

def extract_records_from_article(text: str, url: str):
    tl = text.lower()
    if "perito criminal federal" not in tl:
        return []
    if "aposent" not in tl and "aposentar" not in tl and "aposentad" not in tl:
        return []

    siapes = dedupe([s.replace(".", "") for s in RE_SIAPE.findall(text)])
    nomes = dedupe([n.strip().upper() for n in RE_NOME.findall(text)])

    if nomes and siapes:
        if len(nomes) == len(siapes):
            return [{"nome": n, "siape": s, "url": url} for n, s in zip(nomes, siapes)]
        return [{"nome": nomes[0], "siape": siapes[0], "url": url}]

    if nomes or siapes:
        return [{"nome": nomes[0] if nomes else None, "siape": siapes[0] if siapes else None, "url": url}]

    return [{"nome": None, "siape": None, "url": url}]

def telegram_send(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] Telegram não configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    r = requests.post(api, json=payload, timeout=20)
    r.raise_for_status()

def main():
    date_str = os.getenv("DOU_DATE") or br_today_str()
    session = get_session()

    # Recomendo NÃO usar org/org_sub aqui (às vezes omite páginas/itens).
    # Se você quiser insistir, defina DOU_ORG e DOU_ORG_SUB.
    org = os.getenv("DOU_ORG")  # ex: "Ministério da Justiça e Segurança Pública"
    org_sub = os.getenv("DOU_ORG_SUB")  # ex: "Polícia Federal"

    targets, mode = fetch_all_targets(session, date_str, org, org_sub)
    print(f"[INFO] Targets encontrados ({mode}): {len(targets)}")

    results = []
    scanned = 0
    for t in targets:
        url_used, text = fetch_article(session, t)
        if not url_used or not text:
            continue
        scanned += 1
        found = extract_records_from_article(text, url_used)
        if found:
            results.extend(found)

    payload = {
        "data": date_str,
        "secao": "dou2",
        "mode": mode,
        "targets": len(targets),
        "scanned_articles": scanned,
        "total": len(results),
        "resultados": results,
    }

    out = f"saida_{date_str.replace('-', '')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if results:
        linhas = [f"DOU Seção 2 ({date_str}) — Aposentadoria(s) Perito Criminal Federal:"]
        for r in results:
            nome = r.get("nome") or "(nome não extraído)"
            siape = r.get("siape") or "(SIAPE não extraído)"
            linhas.append(f"- {nome} | SIAPE: {siape} | {r.get('url')}")
        telegram_send("\n".join(linhas))

if __name__ == "__main__":
    main()
