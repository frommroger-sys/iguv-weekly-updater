#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, re, json, datetime, traceback, argparse
from urllib.parse import urljoin, urlparse
from dateutil.tz import gettz
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# =========================
#   ENV / Defaults
# =========================
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")

TZ               = os.environ.get("TZ", "Europe/Zurich")

# Limits & Timeouts (Safe Defaults)
HTTP_TIMEOUT_S             = int(os.environ.get("HTTP_TIMEOUT_S", "25"))
HTTP_MAX_RETRIES           = int(os.environ.get("HTTP_MAX_RETRIES", "2"))
MAX_LINKS_PER_SOURCE       = int(os.environ.get("MAX_LINKS_PER_SOURCE", "40"))
MAX_TOTAL_CANDIDATES       = int(os.environ.get("MAX_TOTAL_CANDIDATES", "150"))
MAX_ITEMS_PER_SECTION_KI   = int(os.environ.get("MAX_ITEMS_PER_SECTION_KI", "5"))
OPENAI_REQUEST_TIMEOUT_S   = int(os.environ.get("OPENAI_REQUEST_TIMEOUT_S", "60"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/3.1; +https://iguv.ch)"
HDR_HTML = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
HDR_JSON = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
}

# =========================
#   Helpers
# =========================
def iso_now_local(fmt="%Y-%m-%d %H:%M") -> str:
    return datetime.datetime.now(tz=gettz(TZ)).strftime(fmt)

def domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def require_env():
    missing = []
    for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "WP_BASE": WP_BASE,
        "WP_USERNAME": WP_USERNAME,
        "WP_APP_PASSWORD": WP_APP_PASSWORD,
    }.items():
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError("Fehlende ENV Variablen: " + ", ".join(missing))

def http_get(url: str) -> requests.Response:
    last_err = None
    for _ in range(HTTP_MAX_RETRIES):
        try:
            return requests.get(url, headers=HDR_HTML, timeout=HTTP_TIMEOUT_S)
        except Exception as e:
            last_err = e
    raise last_err

# Datumsheuristiken
DATE_PATTERNS = [
    r"(?P<iso>\d{4}-\d{2}-\d{2})",
    r"(?P<dot>\d{2}\.\d{2}\.\d{4})",
    r"/(?P<y>\d{4})/(?P<m>\d{2})/",
]

def parse_date_heuristic(text_or_url: str):
    s = text_or_url or ""
    for pat in DATE_PATTERNS:
        m = re.search(pat, s)
        if not m:
            continue
        if m.groupdict().get("iso"):
            try:
                return datetime.datetime.strptime(m.group("iso"), "%Y-%m-%d").date()
            except Exception:
                pass
        if m.groupdict().get("dot"):
            try:
                return datetime.datetime.strptime(m.group("dot"), "%d.%m.%Y").date()
            except Exception:
                pass
        if m.groupdict().get("y") and m.groupdict().get("m"):
            try:
                y = int(m.group("y")); mo = int(m.group("m"))
                return datetime.date(y, mo, 1)
            except Exception:
                pass
    return None

def is_within_days(d: datetime.date, days: int) -> bool:
    if not d: return False
    today = datetime.date.today()
    return (today - d) <= datetime.timedelta(days=days)

def same_site(href: str, base: str) -> bool:
    try:
        return domain(href) == domain(base)
    except Exception:
        return False

# =========================
#   Crawling / Filtering
# =========================
def extract_candidates(list_url: str, days: int, keywords: list[str]) -> list[dict]:
    """Scannt eine Listen-/Übersichtsseite, filtert nach Keywords + Datumsheuristik und deckelt die Anzahl."""
    print(f" - Quelle abrufen: {list_url}")
    try:
        resp = http_get(list_url)
    except Exception as e:
        print("   WARN: fetch failed:", repr(e))
        return []
    if resp.status_code != 200:
        print("   WARN: HTTP", resp.status_code, "bei", list_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    kws = [k.lower() for k in keywords]
    base = list_url
    out = []

    for i, a in enumerate(soup.find_all("a", href=True)):
        if i >= MAX_LINKS_PER_SOURCE:
            break
        href = urljoin(base, a["href"].strip())
        text = " ".join(a.get_text(separator=" ", strip=True).split())
        if not text:
            continue
        if not same_site(href, base):
            continue

        blob = (text + " " + href).lower()
        if kws and not any(k in blob for k in kws):
            continue

        d = parse_date_heuristic(text) or parse_date_heuristic(href)
        if d and not is_within_days(d, days):
            continue

        out.append({
            "title": text[:240],
            "url": href,
            "date": d.isoformat() if d else None,
            "source": domain(href)
        })

    # Dedup (Titel+URL)
    dedup = {}
    for it in out:
        dedup[(it["title"], it["url"])] = it
    items = list(dedup.values())
    print(f"   -> {len(items)} Kandidaten nach Filter (max {MAX_LINKS_PER_SOURCE} Links gescannt)")
    return items

# =========================
#   OpenAI Summarization
# =========================
def summarize_with_openai(sections_payload: list[dict], max_per_section: int, style: str) -> dict:
    """JSON-Ausgabe via Chat Completions (stabil mit openai>=1.35)."""
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    # Payload deckeln
    capped_sections = []
    total = 0
    for sec in sections_payload:
        cand = sec.get("candidates", [])[:50]
        capped_sections.append({"name": sec.get("name", "Sektion"), "candidates": cand})
        total += len(cand)
        if total >= MAX_TOTAL_CANDIDATES:
            break

    sys_prompt = (
        "Du bist ein präziser Nachrichten-Editor für Finanz-/Regulierungsthemen in der Schweiz. "
        f"Erstelle pro Sektion maximal {max_per_section} Punkte. "
        "Jeder Punkt: title, url, date_iso (YYYY-MM-DD; wenn None → heutiges Datum), summary (1–2 Sätze). "
        "Gib das Ergebnis als JSON-Objekt {\"sections\":[{\"name\":\"...\",\"items\":[...]}]} zurück."
    )

    user_payload = {
        "generated_at": iso_now_local(),
        "style": style,
        "sections": capped_sections,
        "note": f"KI sieht max. {MAX_TOTAL_CANDIDATES} Kandidaten insgesamt; pro Sektion max. 50."
    }

    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
        ],
        temperature=0.2
    )

    txt = completion.choices[0].message.content
    try:
        data = json.loads(txt)
    except Exception:
        print("WARN: KI-Output kein JSON. Rohtext (Anfang):", (txt or "")[:600])
        data = {"sections": []}
    return data

# =========================
#   HTML-Rendering
# =========================
def to_html(digest: dict, days: int) -> str:
    parts = [f'<h2>Wöchentliche Übersicht (aktualisiert: {iso_now_local()})</h2>']
    any_item = False
    for sec in digest.get("sections", []):
        items = sec.get("items", []) or []
        if not items:
            continue
        any_item = True
        parts.append(f'<h3 style="margin-top:1.2em;">{sec.get("name","")}</h3>')
        parts.append("<ul>")
        for it in items:
            date = (it.get("date_iso") or "")[:10]
            title = it.get("title","").strip()
            url = it.get("url","").strip()
            summary = it.get("summary","").strip()
            parts.append(f'<li><strong>{date}</strong> – <a href="{url}" target="_blank" rel="noopener">{title}</a>: {summary}</li>')
        parts.append("</ul>")
    if not any_item:
        parts.append("<p>Keine relevanten Neuigkeiten in den letzten Tagen.</p>")
    parts.append(f'<hr><p style="font-size:12px;color:#666;">Automatisch erstellt (letzte {days} Tage).</p>')
    return "\n".join(parts)

# =========================
#   MU-Plugin Endpoint
# =========================
def post_to_mu_plugin(html_inner: str):
    url = f"{WP_BASE}/wp-json/iguv/v1/weekly"
    print(f"WordPress (MU-Plugin) aktualisieren: {url}")
    r = requests.post(
        url,
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        json={"html": html_inner.strip()},
        timeout=HTTP_TIMEOUT_S,
        headers=HDR_JSON
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Endpoint-Update fehlgeschlagen: {r.status_code} {r.text}")
    print("SUCCESS: Weekly HTML via Endpoint gesetzt.")

# =========================
#   Main
# =========================
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()
    cfg = load_yaml("data_sources.yaml")

    parser = argparse.ArgumentParser(description="IGUV Weekly Updater")
    parser.add_argument("--fast", action="store_true", help="Nur Kernquellen (FINMA, SECO, OFAC) – schneller, weniger Last.")
    args = parser.parse_args()

    # Quellen bestimmen (ggf. FAST-Modus)
    sections_cfg = cfg.get("sections", [])
    if args.fast:
        core_domains = {"finma.ch", "seco.admin.ch", "ofac.treasury.gov"}
        filtered_sections = []
        for sec in sections_cfg:
            kept = []
            for u in sec.get("sources", []):
                try:
                    if any(d in domain(u) for d in core_domains):
                        kept.append(u)
                except Exception:
                    pass
            if kept:
                filtered_sections.append({"name": sec["name"], "sources": kept})
        sections_cfg = filtered_sections
        print(f"FAST-Modus aktiv: {len(sections_cfg)} Sektionen / Kernquellen.")

    days = int(cfg.get("time_window_days", 7))
    keywords = cfg.get("keywords", [])
    style = cfg.get("summary", {}).get("style", "Kompakt, sachlich.")

    # Crawlen
    print("Quellen scannen …")
    sections_payload = []
    total_candidates = 0
    for block in sections_cfg:
        name = block.get("name", "Sektion")
        urls = block.get("sources", [])
        all_items = []
        for u in urls:
            if total_candidates >= MAX_TOTAL_CANDIDATES:
                break
            items = extract_candidates(u, days=days, keywords=keywords)
            room = MAX_TOTAL_CANDIDATES - total_candidates
            if room <= 0:
                break
            items = items[:room]
            total_candidates += len(items)
            all_items.extend(items)
        all_items.sort(key=lambda it: it.get("date") or "", reverse=True)
        sections_payload.append({"name": name, "candidates": all_items})

    print(f"Gesamt-Kandidaten (gekappt): {total_candidates} / Limit {MAX_TOTAL_CANDIDATES}")

    # KI
    print("KI-Zusammenfassung …")
    digest = summarize_with_openai(sections_payload, max_per_section=MAX_ITEMS_PER_SECTION_KI, style=style)
    total_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(f"Relevante Meldungen nach KI: {total_items}")

    # HTML
    print("HTML generieren …")
    html_inner = to_html(digest, days=days)

    # WP via MU-Plugin
    print("WordPress aktualisieren …")
    post_to_mu_plugin(html_inner)

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
