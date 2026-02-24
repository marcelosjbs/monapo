import os
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ======= CONFIG =======
BASE = "https://www.in.gov.br"
# Use exatamente o filtro do seu link:
def build_filtered_url(date_str: str) -> str:
    org = "Ministério da Justiça e Segurança Pública"
    org_sub = "Polícia Federal"
    return (
        f"{BASE}/leiturajornal?secao=dou2&data={date_str}"
        f"&org={quote(org)}&org_sub={quote(org_sub)}"
    )

# links que queremos coletar (matérias)
RE_ART_LINK = re.compile(r"^https?://www\.in\.gov\.br/(?:en/)?web/dou/-/.+", re.IGNORECASE)

# Conteúdo alvo dentro da matéria:
RE_SIAPE = re.compile(r"matr[ií]cula\s+SIAPE\s+n[ºo°]\s*([\d\.]+)", re.IGNORECASE)
RE_NOME = re.compile(r"\b([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]+?)\s*,\s*matr[ií]cula\s+SIAPE\b", re.IGNORECASE)

# ======= HELPERS =======
def br_today_str() -> str:
    tz = ZoneInfo("America/Sao_Paulo")
    return datetime.now(tz=tz).strftime("%d-%m-%Y")

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

def clean_text(html: str) -> str:
    # limpeza leve (já vem texto bem direto no artigo)
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

def extract_records_from_article_text(text: str, url: str):
    tl = text.lower()
    if "perito criminal federal" not in tl:
        return []
    if "aposent" not in tl and "aposentar" not in tl and "aposentad" not in tl:
        return []

    siapes = [s.replace(".", "") for s in RE_SIAPE.findall(text)]
    nomes = [n.strip().upper() for n in RE_NOME.findall(text)]
    siapes = dedupe(siapes)
    nomes = dedupe(nomes)

    if nomes and siapes:
        if len(nomes) == len(siapes):
            return [{"nome": n, "siape": s, "url": url} for n, s in zip(nomes, siapes)]
        return [{"nome": nomes[0], "siape": siapes[0], "url": url}]
    if nomes or siapes:
        return [{"nome": nomes[0] if nomes else None, "siape": siapes[0] if siapes else None, "url": url}]
    return [{"nome": None, "siape": None, "url": url}]

def fetch_article_html(url: str) -> str:
    # requests “normal” funciona para o artigo
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=40)
    r.raise_for_status()
    return r.text

# ======= PLAYWRIGHT SCRAPER =======
def collect_links_via_js_pagination(filtered_url: str, max_pages: int = 50):
    links = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"[INFO] Abrindo: {filtered_url}")
        page.goto(filtered_url, wait_until="networkidle", timeout=120_000)

        # A paginação pode demorar; tenta esperar algum conteúdo
        page.wait_for_timeout(1500)

        for idx in range(1, max_pages + 1):
            # coleta links da página atual
            anchors = page.query_selector_all("a")
            for a in anchors:
                href = a.get_attribute("href") or ""
                if href.startswith("/"):
                    href = BASE + href
                if RE_ART_LINK.match(href):
                    links.append(href)

            print(f"[INFO] Página UI {idx}: links acumulados {len(set(links))}")

            # tenta clicar no “Próximo »”
            # O texto pode ser “Próximo »”, “Próximo >>” etc.
            next_btn = page.get_by_text("Próximo", exact=False)

            try:
                # se não existir, para
                if next_btn.count() == 0:
                    print("[INFO] Não achei botão Próximo. Parando.")
                    break

                # alguns layouts deixam "Próximo" sempre presente mas desabilitado
                # checa atributo/class no elemento mais provável
                el = next_btn.first
                # se estiver desabilitado, para
                disabled = (el.get_attribute("aria-disabled") == "true") or ("disabled" in (el.get_attribute("class") or "").lower())
                if disabled:
                    print("[INFO] Próximo desabilitado. Parando.")
                    break

                # clica e espera rede estabilizar
                el.click()
                page.wait_for_load_state("networkidle", timeout=120_000)
                page.wait_for_timeout(900)
            except PWTimeoutError:
                # se travar ao trocar página, tenta seguir com o que já coletou
                print("[WARN] Timeout ao avançar página. Parando paginação.")
                break
            except Exception as e:
                print(f"[WARN] Erro ao avançar página: {e}. Parando.")
                break

        browser.close()

    return dedupe(links)

def main():
    date_str = os.getenv("DOU_DATE") or br_today_str()
    url = build_filtered_url(date_str)

    links = collect_links_via_js_pagination(url)
    print(f"[INFO] Total de matérias (links) coletadas: {len(links)}")

    results = []
    scanned = 0
    for link in links:
        try:
            html = fetch_article_html(link)
            text = clean_text(html)
            found = extract_records_from_article_text(text, link)
            if found:
                results.extend(found)
            scanned += 1
        except Exception:
            continue

    payload = {
        "data": date_str,
        "secao": "dou2",
        "org": "Ministério da Justiça e Segurança Pública",
        "org_sub": "Polícia Federal",
        "links_coletados": len(links),
        "artigos_lidos": scanned,
        "total": len(results),
        "resultados": results,
        "source": url,
    }

    out = f"saida_{date_str.replace('-', '')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if results:
        linhas = [f"DOU Seção 2 ({date_str}) — PF — aposentadoria(s) Perito Criminal Federal:"]
        for r in results:
            nome = r.get("nome") or "(nome não extraído)"
            siape = r.get("siape") or "(SIAPE não extraído)"
            linhas.append(f"- {nome} | SIAPE: {siape} | {r.get('url')}")
        telegram_send("\n".join(linhas))

if __name__ == "__main__":
    main()
