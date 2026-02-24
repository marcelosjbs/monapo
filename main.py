import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.request

BASE = "https://www.in.gov.br"
LEITURAJORNAL = BASE + "/leiturajornal"
ARTIGO_PREFIX = BASE + "/en/web/dou/-/"

RE_SIAPE = re.compile(r"matr[ií]cula\s+SIAPE\s+n[ºo]\s*([\d\.]+)", re.IGNORECASE)
RE_NOME = re.compile(
    r"\b(?:a|ao)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]+?),\s+ocupante\s+do\s+cargo\s+efetivo\s+de\s+Perito\s+Criminal\s+Federal\b",
    re.IGNORECASE,
)

def br_today_str():
    tz = ZoneInfo("America/Sao_Paulo")
    return datetime.now(tz=tz).strftime("%d-%m-%Y")

def http_get(url: str, timeout=30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "dou-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

def extract_json_array_from_leiturajornal(html: str):
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

def main():
    date_str = br_today_str()
    jornal_url = f"{LEITURAJORNAL}?secao=dou2&data={date_str}"
    html = http_get(jornal_url)
    items = extract_json_array_from_leiturajornal(html)

    url_titles = []
    for it in items:
        if isinstance(it, dict) and it.get("urlTitle"):
            url_titles.append(it["urlTitle"])

    # dedupe
    seen = set()
    url_titles = [x for x in url_titles if not (x in seen or seen.add(x))]

    results = []
    for ut in url_titles:
        article_url = ARTIGO_PREFIX + ut
        try:
            art_html = http_get(article_url)
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

if __name__ == "__main__":
    main()