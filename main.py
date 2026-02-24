import os
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


BASE = "https://www.in.gov.br"              # <-- use www
LEITURAJORNAL = BASE + "/leiturajornal"
ARTIGO_PREFIX = BASE + "/en/web/dou/-/"     # funciona hoje, mantemos

RE_SIAPE = re.compile(r"matr[ií]cula\s+SIAPE\s+n[ºo]\s*([\d\.]+)", re.IGNORECASE)
RE_NOME = re.compile(
    r"\b(?:a|ao)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]+?),\s+ocupante\s+do\s+cargo\s+efetivo\s+de\s+Perito\s+Criminal\s+Federal\b",
    re.IGNORECASE,
)


def br_today_str():
    tz = ZoneInfo("America/Sao_Paulo")
    return datetime.now(tz=tz).strftime("%d-%m-%Y")


def get_session() -> requests.Session:
    s = requests.Session()

    # Headers “de navegador” (o que geralmente resolve o 403 no Actions)
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


def http_get(session: requests.Session, url: str, timeout=30) -> str:
    last_err = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 403:
                # pequena pausa + tenta de novo (às vezes o WAF libera no retry)
                time.sleep(1.5 + attempt * 1.0)
                last_err = RuntimeError("403 Forbidden")
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(1.0 + attempt * 0.8)
    raise last_err


def extract_json_array_from_leiturajornal(html: str):
    # Encontra <script type="application/json">{... "jsonArray":[...] ...}</script>
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
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_records(text: str, url: str):
    tl = text.lower()
    if "perito criminal federal" not in tl:
        return []
    if "aposent" not in tl:
        return []

    siapes = RE_SIAPE.findall(text)
    nomes = RE_NOME.findall(text)

    records = []
    if nomes and siapes and len(nomes) == len(siapes):
        for nome, siape in zip(nomes, siapes):
            records.append({"nome": nome.strip().upper(), "siape": siape.replace(".", ""), "url": url})
    else:
        nome = nomes[0].strip().upper() if nomes else None
        siape = siapes[0].replace(".", "") if siapes else None
        if nome or siape:
            records.append({"nome": nome, "siape": siape, "url": url})

    return records


def telegram_send(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram não configurado (faltam TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def main():
    date_str = br_today_str()
    session = get_session()

    jornal_url = f"{LEITURAJORNAL}?secao=dou2&data={date_str}"
    html = http_get(session, jornal_url)
    items = extract_json_array_from_leiturajornal(html)

    url_titles = []
    for it in items:
        if isinstance(it, dict) and it.get("urlTitle"):
            url_titles.append(it["urlTitle"])

    # remove duplicados preservando ordem
    seen = set()
    url_titles = [x for x in url_titles if not (x in seen or seen.add(x))]

    results = []
    for ut in url_titles:
        article_url = ARTIGO_PREFIX + ut
        try:
            art_html = http_get(session, article_url)
            text = clean_text(art_html)
            results.extend(extract_records(text, article_url))
        except Exception:
            continue

    payload = {
        "data": date_str,
        "secao": "dou2",
        "filtro": ["aposent*", "Perito Criminal Federal"],
        "total": len(results),
        "resultados": results,
    }

    out = f"saida_{date_str.replace('-', '')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # Telegram: só avisa quando achar algo (você pode mudar para avisar sempre)
    if results:
        linhas = [f"DOU Seção 2 ({date_str}) — aposentadoria(s) Perito Criminal Federal:"]
        for r in results:
            linhas.append(f"- {r.get('nome')} | SIAPE: {r.get('siape')} | {r.get('url')}")
        telegram_send("\n".join(linhas))


if __name__ == "__main__":
    main()
