import os
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BASE = "https://www.in.gov.br"
LEITURAJORNAL = BASE + "/leiturajornal"
ARTIGO_PREFIXES = [
    BASE + "/en/web/dou/-/",
    BASE + "/web/dou/-/",
]

# SIAPE: aceita nº / no / n° (°)
RE_SIAPE = re.compile(r"matr[ií]cula\s+SIAPE\s+n[ºo°]\s*([\d\.]+)", re.IGNORECASE)

# Nome típico do DOU: "NOME, matrícula SIAPE ..."
RE_NOME = re.compile(
    r"\b([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]+?)\s*,\s*matr[ií]cula\s+SIAPE\b",
    re.IGNORECASE,
)

# filtros do seu caso
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

def extract_json_array_from_leiturajornal(html: str):
    # <script type="application/json">{... "jsonArray":[...] ...}</script>
    m = re.search(
        r'<script[^>]+type="application/json"[^>]*>\s*(\{.*?\})\s*</script>',
        html, re.DOTALL
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        arr = data.get("jsonArray", [])
        return arr if isinstance(arr, list) else []
    except Exception:
        return []

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

def item_matches_pf(item: dict) -> bool:
    # sem depender de campo fixo: transforma o item inteiro em texto e procura termos
    blob = json.dumps(item, ensure_ascii=False).lower()
    return any(k in blob for k in NEEDLE_ITEM_ANY)

def fetch_article(session: requests.Session, url_title: str):
    for prefix in ARTIGO_PREFIXES:
        url = prefix + url_title
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

    # IMPORTANTÍSSIMO: NÃO usar org/org_sub aqui — pega a seção inteira do dia.
    jornal_url = f"{LEITURAJORNAL}?secao=dou2&data={date_str}"
    print(f"[INFO] leiturajornal: {jornal_url}")

    html = http_get(session, jornal_url)
    items = extract_json_array_from_leiturajornal(html)
    print(f"[INFO] jsonArray itens: {len(items)}")

    url_titles = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if not it.get("urlTitle"):
            continue
        # pré-filtro por PF/MJSP no item
        if item_matches_pf(it):
            url_titles.append(it["urlTitle"])

    url_titles = dedupe(url_titles)
    print(f"[INFO] candidatos PF/MJSP: {len(url_titles)}")

    results = []
    scanned = 0

    for ut in url_titles:
        url_used, text = fetch_article(session, ut)
        if not url_used or not text:
            continue
        scanned += 1
        found = extract_records_from_article(text, url_used)
        if found:
            results.extend(found)

    payload = {
        "data": date_str,
        "secao": "dou2",
        "total": len(results),
        "resultados": results,
        "scanned_articles": scanned,
        "source": jornal_url,
    }

    out = f"saida_{date_str.replace('-', '')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if results:
        msg = [f"DOU Seção 2 ({date_str}) — Aposentadoria(s) PF (Perito Criminal Federal):"]
        for r in results:
            msg.append(f"- {r.get('nome')} | SIAPE: {r.get('siape')} | {r.get('url')}")
        telegram_send("\n".join(msg))

if __name__ == "__main__":
    main()
