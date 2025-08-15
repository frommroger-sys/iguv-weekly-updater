#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, re, json, datetime, traceback, argparse, html
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

# Limits & Toggles
HTTP_TIMEOUT_S             = int(os.environ.get("HTTP_TIMEOUT_S", "25"))
HTTP_MAX_RETRIES           = int(os.environ.get("HTTP_MAX_RETRIES", "2"))
MAX_LINKS_PER_SOURCE       = int(os.environ.get("MAX_LINKS_PER_SOURCE", "60"))
MAX_TOTAL_CANDIDATES       = int(os.environ.get("MAX_TOTAL_CANDIDATES", "200"))
MAX_ITEMS_PER_SECTION_KI   = int(os.environ.get("MAX_ITEMS_PER_SECTION_KI", "5"))
OPENAI_REQUEST_TIMEOUT_S   = int(os.environ.get("OPENAI_REQUEST_TIMEOUT_S", "60"))
EXPAND_DETAILS             = os.environ.get("EXPAND_DETAILS", "1") in ("1","true","TRUE")

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/4.0; +https://iguv.ch)"
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
def now_local():
    return datetime.datetime.now(tz=gettz(TZ))

def iso_now_local(fmt="%Y-%m-%d %H:%M") -> str:
    return now_local().strftime(fmt)

def ch_date(d: datetime.date|None) -> str:
    if not d: return ""
    return d.strftime("%d.%m.%Y")  # Schweizer Format

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

def normalize(s: str) -> str:
    return " ".join((s or "").strip().split())

# Navigation/irrelevante Linktexte (kleingeschrieben vergleichen)
TEXT_BLACKLIST = {
    "home","start","startseite","über uns","ueber uns","about","kontakt","kontaktieren",
    "jobs","karriere","login","anmelden","impressum","datenschutz","media","medien","news",
    "newsletter","presse","faq","häufige fragen","haeufige fragen","downloads","publikationen",
    "veranstaltungen","events","kalender","sitemap","kontaktformular"
}

# =========================
#   Deep snippet (optional)
# =========================
def extract_snippet(url: str, max_chars: int = 280) -> str:
    """
    Holt von der Zielseite die ersten sinnvollen 1–2 Absätze als Anriss.
    Liefert leeren String, falls nicht möglich. Zielt auf präzisere KI-Kurzfassung.
    """
    try:
        r = http_get(url)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, "lxml")
        # Kandidaten: <article>, sonst die ersten <p> mit genug Text
        ctx = soup.find("article") or soup
        paras = [normalize(p.get_text(" ", strip=True)) for p in ctx.find_all("p")]
        paras = [p for p in paras if len(p) > 60][:2]
        text = " ".join(paras)[:max_chars]
        return text
    except Exception:
        return ""

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
        if include_regex and not any(rx.search(href) or rx.search(text) for rx in include_regex):
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

        snippet = extract_snippet(href) if EXPAND_DETAILS else ""
        items.append({
            "title": text[:240],
            "url": href,
            "date": d.isoformat() if d else None,
            "source": domain(href),
            "snippet": snippet
        })

    print(f"   -> {len(items)} Kandidaten nach Filter (max {MAX_LINKS_PER_SOURCE} Links gescannt)")
    return items

# =========================
#   OpenAI Summarization (Kurzfassung + Items)
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
        "Du bist Redaktor für Vermögensverwalter in der Schweiz. "
        "Ziel: komprimierte, fachlich richtige Weekly-Übersicht mit klarer Kurzfassung.\n\n"
        "Richtlinien:\n"
        "- Fokus auf FINMA-/SECO-/OFAC-/EU-Primärquellen; AO (AOOS, OSFIN, OAD-FCT, OSIF, SO-FIT) zu Gebühren/Reglement/Prüfung/FAQ.\n"
        "- Keine Navigations- oder Übersichtsseiten zusammenfassen.\n"
        "- Schreibe sachlich, prägnant, ohne Marketing.\n"
        "- Kurzfassung: 4–5 bullets, nur wirklich Relevantes mit möglicher Auswirkung auf Vermögensverwalter (Pflichten, Fristen, Sanktionen, Rundschreiben).\n"
        "- Verwende die mitgelieferten 'snippet'-Texte als Kontext; wenn leer, fasse Titel/URL anhand Domainwissen minimal ein.\n"
        "- Datumsformat in der Ausgabe NICHT setzen (das macht der Renderer).\n"
        "Gib ein JSON-Objekt zurück: {\n"
        '  "briefing":[{"title":"...","url":"..."}],\n'
        '  "sections":[{"name":"...","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD or empty","summary":"..."}]}]\n'
        "}\n"
    )

    user_payload = {
        "generated_at": iso_now_local(),
        "style": style,
        "sections": capped,
    }

    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role":"system","content":sys_prompt},
            {"role":"user","content":json.dumps(user_payload, ensure_ascii=False)}
        ],
        temperature=0.15
    )

    txt = completion.choices[0].message.content
    try:
        return json.loads(txt)
    except Exception:
        print("WARN: KI-Output kein JSON. Rohtext (Anfang):", (txt or "")[:600])
        return {"briefing": [], "sections":[]}

# =========================
#   HTML Rendering (mit CH-Datum & Layout)
# =========================
CSS = """
<style>
.iguv-weekly{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;line-height:1.55;font-size:16px}
.iguv-weekly h1{font-size:2.2rem;margin:0 0 .2rem 0;color:#0f2a5a;font-weight:600}
.iguv-weekly .meta{color:#666;margin-bottom:1rem}
.iguv-weekly h2{font-size:1.25rem;margin:1.2rem 0 .4rem;color:#0f2a5a}
.iguv-weekly h3{font-size:1.1rem;margin:1rem 0 .25rem}
.iguv-weekly ul{margin:.4rem 0 1rem 1.2rem}
.iguv-weekly li{margin:.3rem 0}
.iguv-weekly a{color:#0b5bd3;text-decoration:underline}
.iguv-note{background:#f6f8fb;border-left:4px solid #0f2a5a;padding:.8rem;border-radius:.5rem;margin:.8rem 0 1rem}
.iguv-disclaimer{font-size:.9rem;color:#555;border-top:1px solid #e6e6e6;padding-top:.6rem;margin-top:1rem}
</style>
"""

def to_html(digest: dict, days: int) -> str:
    dt = now_local()
    date_line = dt.strftime("%d.%m.%Y %H:%M")  # CH-Format

    parts = [CSS, '<div class="iguv-weekly">']
    parts.append("<h1>Weekly-Updates</h1>")
    parts.append(f'<div class="meta">Stand: {html.escape(date_line)}</div>')

    # Kurzfassung
    briefing = digest.get("briefing") or []
    if briefing:
        parts.append('<div class="iguv-note"><strong>Kurzfassung (4–5 Punkte):</strong><ul>')
        for b in briefing[:5]:
            title = html.escape(b.get("title","").strip())
            url = html.escape(b.get("url","").strip())
            parts.append(f'<li><a href="{url}" target="_blank" rel="noopener">{title}</a></li>')
        parts.append('</ul></div>')

    # Sektionen
    any_item = False
    for sec in digest.get("sections", []):
        items = sec.get("items", []) or []
        if not items: continue
        any_item = True
        parts.append(f'<h2>{html.escape(sec.get("name",""))}</h2>')
        parts.append("<ul>")
        for it in items:
            date_iso = (it.get("date_iso") or "").strip()
            d_txt = ""
            if date_iso:
                try:
                    d = datetime.datetime.strptime(date_iso[:10], "%Y-%m-%d").date()
                    d_txt = f"<strong>{ch_date(d)}</strong> – "
                except Exception:
                    d_txt = ""
            title = html.escape(it.get("title","").strip())
            url = html.escape(it.get("url","").strip())
            summary = html.escape(it.get("summary","").strip())
            parts.append(f'<li>{d_txt}<a href="{url}" target="_blank" rel="noopener">{title}</a>. {summary}</li>')
        parts.append("</ul>")

    if not any_item:
        parts.append('<p>Keine relevanten Neuigkeiten in den letzten Tagen.</p>')

    parts.append(f'<div class="iguv-disclaimer">Massgebend sind ausschliesslich die verlinkten Originalquellen. '
                 f'Dieses Weekly zeigt nur Änderungen der letzten {days} Tage.</div>')
    parts.append("</div>")
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
        core = {"finma.ch","seco.admin.ch","ofac.treasury.gov"}
        fast = []
        for sec in sections_cfg:
            chosen = []
            for src in sec.get("sources", []):
                u = src["url"] if isinstance(src, dict) else src
                if any(d in domain(u) for d in core):
                    chosen.append(src)
            if chosen:
                fast.append({"name":sec["name"], "sources": chosen, "keywords": sec.get("keywords")})
        sections_cfg = fast
        print(f"FAST-Modus aktiv: {len(sections_cfg)} Sektionen / Kernquellen.")

    days = int(cfg.get("time_window_days", 7))
    global_keywords = [k.lower() for k in (cfg.get("keywords") or [])]
    style = cfg.get("summary", {}).get("style", "Sachlich, prägnant.")

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
            rule = dict(url=src) if isinstance(src, str) else dict(src)
            url = rule.get("url")
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
