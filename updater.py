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

# Limits & Timeouts
HTTP_TIMEOUT_S             = int(os.environ.get("HTTP_TIMEOUT_S", "25"))
HTTP_MAX_RETRIES           = int(os.environ.get("HTTP_MAX_RETRIES", "2"))
MAX_LINKS_PER_SOURCE       = int(os.environ.get("MAX_LINKS_PER_SOURCE", "60"))
MAX_TOTAL_CANDIDATES       = int(os.environ.get("MAX_TOTAL_CANDIDATES", "200"))
MAX_ITEMS_PER_SECTION_KI   = int(os.environ.get("MAX_ITEMS_PER_SECTION_KI", "5"))
OPENAI_REQUEST_TIMEOUT_S   = int(os.environ.get("OPENAI_REQUEST_TIMEOUT_S", "60"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/3.2; +https://iguv.ch)"
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
    try: return urlparse(url).netloc
    except: return ""

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
        if not v: missing.append(k)
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

# Datums-Heuristik (URL/Titel)
DATE_PATTERNS = [
    r"(?P<iso>\d{4}-\d{2}-\d{2})",
    r"(?P<dot>\d{2}\.\d{2}\.\d{4})",
    r"/(?P<y>\d{4})/(?P<m>\d{2})/",
]

def parse_date_heuristic(text_or_url: str):
    s = text_or_url or ""
    for pat in DATE_PATTERNS:
        m = re.search(pat, s)
        if not m: continue
        if m.groupdict().get("iso"):
            try: return datetime.datetime.strptime(m.group("iso"), "%Y-%m-%d").date()
            except: pass
        if m.groupdict().get("dot"):
            try: return datetime.datetime.strptime(m.group("dot"), "%d.%m.%Y").date()
            except: pass
        if m.groupdict().get("y") and m.groupdict().get("m"):
            try: return datetime.date(int(m.group("y")), int(m.group("m")), 1)
            except: pass
    return None

def is_within_days(d: datetime.date, days: int) -> bool:
    return bool(d) and (datetime.date.today() - d) <= datetime.timedelta(days=days)

# Navigation/irrelevante Linktexte (kleingeschrieben vergleichen)
TEXT_BLACKLIST = {
    "home","start","startseite","über uns","ueber uns","about","kontakt","kontaktieren",
    "jobs","karriere","login","anmelden","impressum","datenschutz","media","medien","news",
    "newsletter","presse","faq","häufige fragen","haeufige fragen","downloads","publikationen",
    "veranstaltungen","events","kalender","sitemap"
}

def normalize(s: str) -> str:
    return " ".join((s or "").strip().split())

# =========================
#   Crawling / Filtering (mit Source-Regeln)
# =========================
def extract_from_source(list_url: str, rule: dict, days: int, global_keywords: list[str]) -> list[dict]:
    """
    rule:
      include_regex: [list]
      exclude_text: [list]
      require_date: true/false
      same_site_only: true/false
      keywords: [list]   # optional, überschreibt global
    """
    print(f" - Quelle abrufen: {list_url}")
    try:
        resp = http_get(list_url)
    except Exception as e:
        print("   WARN: fetch failed:", repr(e)); return []
    if resp.status_code != 200:
        print("   WARN: HTTP", resp.status_code, "bei", list_url); return []

    soup = BeautifulSoup(resp.text, "lxml")
    include_regex = [re.compile(pat, flags=re.I) for pat in rule.get("include_regex", [])]
    exclude_text = [t.lower() for t in rule.get("exclude_text", [])]
    require_date = bool(rule.get("require_date", True))
    same_site_only = rule.get("same_site_only", True)
    kws = [k.lower() for k in (rule.get("keywords") or global_keywords or [])]

    items = []
    base = list_url
    for i, a in enumerate(soup.find_all("a", href=True)):
        if i >= MAX_LINKS_PER_SOURCE: break

        href = urljoin(base, a["href"].strip())
        text = normalize(a.get_text(" ", strip=True))

        if not text or text.lower() in TEXT_BLACKLIST: 
            continue
        if same_site_only and domain(href) != domain(base):
            continue
        if include_regex:
            if not any(rx.search(href) or rx.search(text) for rx in include_regex):
                continue
        if exclude_text and any(ex in text.lower() for ex in exclude_text):
            continue

        # Keywords (wenn gesetzt)
        blob = (text + " " + href).lower()
        if kws and not any(k in blob for k in kws):
            continue

        d = parse_date_heuristic(text) or parse_date_heuristic(href)
        if require_date and not d:
            continue
        if d and not is_within_days(d, days):
            continue

        items.append({
            "title": text[:240],
            "url": href,
            "date": d.isoformat() if d else None,
            "source": domain(href)
        })

    print(f"   -> {len(items)} Kandidaten nach Filter (max {MAX_LINKS_PER_SOURCE} Links gescannt)")
    return items

# =========================
#   OpenAI Summarization
# =========================
def summarize_with_openai(sections_payload: list[dict], max_per_section: int, style: str) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    # Deckeln
    capped, total = [], 0
    for sec in sections_payload:
        cand = sec.get("candidates", [])[:60]
        capped.append({"name": sec.get("name","Sektion"), "candidates": cand})
        total += len(cand)
        if total >= MAX_TOTAL_CANDIDATES: break

    sys_prompt = (
        "Du bist ein präziser Nachrichten-Editor für Finanz-/Regulierungsthemen in der Schweiz. "
        f"Erstelle pro Sektion maximal {max_per_section} Punkte. "
        "Jeder Punkt: title, url, date_iso (YYYY-MM-DD; wenn nicht vorhanden → leer lassen), summary (1–2 Sätze). "
        "Gib das Ergebnis als JSON-Objekt {\"sections\":[{\"name\":\"...\",\"items\":[...]}]} zurück."
    )

    user_payload = {
        "generated_at": iso_now_local(),
        "style": style,
        "sections": capped,
        "note": f"Insgesamt max. {MAX_TOTAL_CANDIDATES} Kandidaten."
    }

    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role":"system","content":sys_prompt},
            {"role":"user","content":json.dumps(user_payload, ensure_ascii=False)}
        ],
        temperature=0.2
    )

    txt = completion.choices[0].message.content
    try:
        return json.loads(txt)
    except Exception:
        print("WARN: KI-Output kein JSON. Rohtext (Anfang):", (txt or "")[:600])
        return {"sections":[]}

# =========================
#   HTML Rendering
# =========================
def to_html(digest: dict, days: int) -> str:
    parts = [f'<h2>Wöchentliche Übersicht (aktualisiert: {iso_now_local()})</h2>']
    any_item = False
    for sec in digest.get("sections", []):
        items = sec.get("items", []) or []
        if not items: continue
        any_item = True
        parts.append(f'<h3 style="margin-top:1.2em;">{sec.get("name","")}</h3>')
        parts.append("<ul>")
        for it in items:
            date = (it.get("date_iso") or "").strip()
            date_html = f"<strong>{date}</strong> – " if date else ""
            title = it.get("title","").strip()
            url = it.get("url","").strip()
            summary = it.get("summary","").strip()
            parts.append(f'<li>{date_html}<a href="{url}" target="_blank" rel="noopener">{title}</a>: {summary}</li>')
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
    parser.add_argument("--fast", action="store_true", help="Nur Kernquellen (FINMA, SECO, OFAC)")
    args = parser.parse_args()

    sections_cfg = cfg.get("sections", [])
    if args.fast:
        core_domains = {"finma.ch","seco.admin.ch","ofac.treasury.gov"}
        fast_sections = []
        for sec in sections_cfg:
            rules = []
            for src in sec.get("sources", []):
                u = src["url"] if isinstance(src, dict) else src
                if any(d in domain(u) for d in core_domains):
                    rules.append(src)
            if rules:
                fast_sections.append({"name":sec["name"], "sources": rules, "keywords": sec.get("keywords")})
        sections_cfg = fast_sections
        print(f"FAST-Modus aktiv: {len(sections_cfg)} Sektionen / Kernquellen.")

    days = int(cfg.get("time_window_days", 7))
    global_keywords = [k.lower() for k in (cfg.get("keywords") or [])]
    style = cfg.get("summary", {}).get("style", "Kompakt, sachlich.")

    # Crawlen
    print("Quellen scannen …")
    sections_payload = []
    total = 0
    for block in sections_cfg:
        name = block.get("name", "Sektion")
        per_section_keywords = [k.lower() for k in (block.get("keywords") or [])] or global_keywords
        cand_all = []
        for src in block.get("sources", []):
            if total >= MAX_TOTAL_CANDIDATES: break
            if isinstance(src, str):
                rule = {"url": src}
            else:
                rule = dict(src)
            url = rule.get("url"); 
            if not url: continue
            items = extract_from_source(url, rule, days, per_section_keywords)
            room = MAX_TOTAL_CANDIDATES - total
            items = items[:room]
            total += len(items)
            cand_all.extend(items)
        cand_all.sort(key=lambda it: it.get("date") or "", reverse=True)
        sections_payload.append({"name": name, "candidates": cand_all})

    print(f"Gesamt-Kandidaten (gekappt): {total} / Limit {MAX_TOTAL_CANDIDATES}")

    # KI
    print("KI-Zusammenfassung …")
    digest = summarize_with_openai(sections_payload, MAX_ITEMS_PER_SECTION_KI, style)
    total_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(f"Relevante Meldungen nach KI: {total_items}")

    # HTML
    print("HTML generieren …")
    html_inner = to_html(digest, days)

    # WP
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
