#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IGUV Weekly Updater – HTML only

- Modell: websearch-gpt-5 (integrierte Websuche; im Dashboard sichtbar)
- Struktur wie besprochen (Kurzfassung, FINMA, Sanktionen SECO/OFAC/EU, Branchenstimmung, Parlament, Nächste Events)
- FINMA wird nicht auf fixe Länder eingeschränkt; GPT soll alle relevanten Sanktions-/Embargothemen identifizieren
- InPaSu wird NICHT im Titel genannt; Posts können in Branchenstimmung einfliessen
- Economiesuisse/SwissBanking + Parlament bis 60 Tage zurück, wenn relevant
- Poppins, mehr vertikale Abstände, Fusszeile mit Copyright
- Event-Überschrift: „Nächste Events“
- WP POST an MU-Plugin (/wp-json/iguv/v1/weekly), optional mit X-IGUV-Token (WP_API_TOKEN)

Benötigte ENV (in GitHub Actions gesetzt):
  OPENAI_API_KEY
  OPENAI_MODEL=websearch-gpt-5
  OPENAI_REQUEST_TIMEOUT_S=600
  WP_BASE, WP_USERNAME, WP_APP_PASSWORD
  (optional) WP_API_TOKEN  -> muss in WP-Plugin übereinstimmen, falls genutzt
"""

import os, sys, json, html, re, traceback
from datetime import datetime, date
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup

# ================== ENV ==================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "websearch-gpt-5")
OPENAI_REQUEST_TIMEOUT_S = int(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "600"))
USE_WEBSEARCH = True  # bei websearch-gpt-5 ohnehin integriert

WP_BASE         = (os.getenv("WP_BASE", "") or "").rstrip("/")
WP_USERNAME     = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_API_TOKEN    = os.getenv("WP_API_TOKEN", "")  # optionaler zusätzlicher Header-Token

EVENTS_COUNT = int(os.getenv("EVENTS_COUNT", "3"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/9.0; +https://iguv.ch)"
REQ_TIMEOUT = 30

# ================== Hilfsfunktionen ==================
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

def ch_date_str(d: date, with_time: Optional[datetime] = None) -> str:
    months = ["Januar","Februar","März","April","Mai","Juni",
              "Juli","August","September","Oktober","November","Dezember"]
    base = f"{d.day}. {months[d.month-1]} {d.year}"
    if with_time is not None:
        base += with_time.strftime(", %H:%M")
    return base

def parse_iso_or_ch(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d").date()
        return ch_date_str(d)
    except Exception:
        return s

def http_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"WARN: GET failed for {url}: {repr(e)}")
        return None

# ================== Events (lokal aus /event/) ==================
DATE_RX = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

def _extract_date_from_text(txt: str) -> Optional[date]:
    m = DATE_RX.search(txt or "")
    if not m: return None
    try:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(yyyy, mm, dd)
    except Exception:
        return None

def fetch_upcoming_iguv_events(base_url: str, n=3) -> List[Dict[str, str]]:
    url = f"{base_url}/event/"
    out: List[Dict[str, str]] = []
    try:
        html_data = http_get(url)
        if not html_data:
            return out
        soup = BeautifulSoup(html_data, "lxml")
        today = date.today()

        candidates = []
        # Breite Auswahl von Containern, weil Elementor-Templates variieren können
        for tag in soup.find_all(["article","li","div","span","a"], recursive=True):
            txt = " ".join((tag.get_text(" ", strip=True) or "").split())
            if not txt:
                continue
            d = _extract_date_from_text(txt)
            if not d and tag.parent:
                d = _extract_date_from_text(" ".join(tag.parent.get_text(" ", strip=True).split()))
            if not d or d < today:
                continue

            href = None
            if tag.name == "a" and tag.has_attr("href"):
                href = tag["href"]
            else:
                a = tag.find("a", href=True)
                if a: href = a["href"]

            title = txt[:220]
            if href:
                href = requests.compat.urljoin(url, href)
                candidates.append({"date_iso": d.isoformat(), "title": title, "url": href})

        # sortieren & deduplizieren
        seen = set()
        for e in sorted(candidates, key=lambda it: it["date_iso"]):
            key = (e["date_iso"], e["url"])
            if key in seen: continue
            seen.add(key)
            out.append(e)
            if len(out) >= n: break
    except Exception as e:
        print("WARN: fetch_upcoming_iguv_events:", repr(e))
    return out

# ================== OpenAI (Responses API) ==================
def build_messages(events: List[Dict[str,str]]) -> List[Dict[str,str]]:
    """
    System+User Nachrichten für websearch-gpt-5.
    Wichtig: keine Temperature setzen (gpt-5 only default=1 unterstützt).
    """
    must_sources = {
        "FINMA": [
            "https://www.finma.ch/de/news/",
            "https://www.finma.ch/de/dokumentation/rundschreiben/",
            "https://www.finma.ch/de/sanktionen-und-embargos/"
        ],
        "SECO": [
            "https://www.seco.admin.ch/seco/de/home/Aussenwirtschaftspolitik_Wirtschaftliche_Zusammenarbeit/Wirtschaftsbeziehungen/exportkontrollen-und-sanktionen/sanktionen-embargos.html"
        ],
        "OFAC": [
            "https://ofac.treasury.gov/recent-actions"
        ],
        "EU": [
            "https://www.consilium.europa.eu/en/press/press-releases/",
            "https://www.sanctionsmap.eu/#/main"
        ],
        "Media_Associations": [
            "https://www.economiesuisse.ch/de/medien",
            "https://www.swissbanking.ch/de/medien",
            "https://www.vsv-asg.ch/de/aktuelles",
            "https://iguv.ch/news/",
            "https://inpasu.ch/news/",
            "https://www.finews.ch/",
            "https://www.handelszeitung.ch/",
            "https://www.nzz.ch/themen/wirtschaft"
        ],
        "Parliament_CH": [
            "https://www.parlament.ch/de/ratsbetrieb/suche-curia-vista"
        ]
    }
    spec = {
        "task": "Erstelle einen wöchentlichen, belegten Überblick für Schweizer Vermögensverwalter.",
        "time_windows": {
            "default_days": 7,
            "media_days": 60,
            "parliament_days": 60
        },
        "must_search_sources": must_sources,
        "format_requirements": {
            "language": "Deutsch",
            "sections_order": [
                "FINMA-Updates",
                "Sanktionen & Embargos (SECO, OFAC, EU)",
                "Branchenstimmung (Verbände & Medien)",
                "Parlamentarische Agenda",
                "Nächste Events"
            ],
            "limits": {
                "max_items_per_section": 5,
                "briefing_bullets": 5,
                "briefing_max_words": 18
            },
            "each_item_fields": ["title","url","date_iso","issuer","summary"]
        },
        "events_hint": events
    }

    sys = (
        "Du bist Redakteur für Schweizer Vermögensverwalter. "
        "Durchsuche FINMA (News, Rundschreiben, Sanktionen & Embargos), SECO, OFAC, EU (inkl. sanctionsmap.eu) "
        "sowie Verbände/Medien (bis 60 Tage) und CH-Parlament (bis 60 Tage). "
        "Wähle nur fachlich relevante Inhalte (Rundschreiben, Aufsichtsmitteilungen, Sanktionslisten/General Licenses/Designations, "
        "branchenrelevante Verbands-/Medienberichte, parlamentarische Vorstösse mit Bezug zu AML/Sanktionen/Finanzmarkt). "
        "Formatiere AUSSCHLIESSLICH als JSON:\n"
        "{\n"
        '  "briefing":[{"title":"...","url":"..."}],\n'
        '  "sections":[\n'
        '    {"name":"FINMA-Updates","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"FINMA","summary":"..."}]},\n'
        '    {"name":"Sanktionen & Embargos (SECO, OFAC, EU)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"SECO|OFAC|EU|EU-Rat","summary":"..."}]},\n'
        '    {"name":"Branchenstimmung (Verbände & Medien)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"economiesuisse|SwissBanking|VSV-ASG|IGUV|Medium","summary":"..."}]},\n'
        '    {"name":"Parlamentarische Agenda","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"Schweizer Parlament","summary":"..."}]},\n'
        '    {"name":"Nächste Events","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"IGUV|InPaSu","summary":""}]}\n'
        "  ]\n"
        "}\n"
        "Kein Fliesstext außerhalb des JSON. Quelle (issuer) nur vorne fett im HTML ausgeben."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(spec, ensure_ascii=False)}
    ]

def call_openai(messages: List[Dict[str,str]]) -> Dict[str, Any]:
    """
    Nutzt websearch-gpt-5. Keine Temperature übergeben (gpt-5 akzeptiert nur Default).
    """
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    model = OPENAI_MODEL.strip()
    print(f"OpenAI: Modell={model} (Websuche integriert), Timeout={OPENAI_REQUEST_TIMEOUT_S}s")

    try:
        resp = client.responses.create(
            model=model,
            input=messages
        )
        raw = (getattr(resp, "output_text", "") or "").strip()
    except Exception as e:
        print("OpenAI-Call fehlgeschlagen:", repr(e))
        return {"briefing": [], "sections": []}

    try:
        start = raw.find("{"); end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start:end+1]
        data = json.loads(raw)
        if "briefing" not in data: data["briefing"] = []
        if "sections" not in data: data["sections"] = []
        return data
    except Exception:
        print("WARN: KI-Output kein valider JSON. Rohtext (Anfang):", raw[:600])
        return {"briefing": [], "sections": []}

# ================== HTML Rendering ==================
CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap');
  .iguv-weekly{font-family:'Poppins',system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;line-height:1.6;font-size:16px}
  .iguv-weekly h1{font-size:2.2rem;margin:0 0 .6rem;color:#0f2a5a;font-weight:700}
  .iguv-weekly .meta{color:#666;margin:0 0 1.2rem}
  .iguv-weekly h2{font-size:1.35rem;margin:1.6rem 0 .7rem;color:#0f2a5a}
  .iguv-weekly ul{margin:.6rem 0 1.2rem 1.3rem}
  .iguv-weekly li{margin:.5rem 0}
  .iguv-note{background:#f6f8fb;border-left:4px solid #0f2a5a;padding:1rem;border-radius:.6rem;margin:1rem 0 1.4rem}
  .iguv-disclaimer{font-size:.9rem;color:#555;border-top:1px solid #e6e6e6;padding-top:.7rem;margin-top:1.2rem}
  .issuer{color:#0f2a5a;font-weight:600}
</style>
"""

def render_html(digest: Dict[str, Any], events: List[Dict[str,str]]) -> str:
    now = datetime.now()
    parts = [
        CSS,
        '<div class="iguv-weekly">',
        '<h1>Weekly-Updates</h1>',
        f'<div class="meta">Stand: {html.escape(ch_date_str(now.date(), with_time=now))}</div>'
    ]

    # Kurzfassung
    briefing = digest.get("briefing") or []
    if briefing:
        parts.append('<div class="iguv-note"><strong>Kurzfassung (4–5 Punkte):</strong><ul>')
        for b in briefing[:5]:
            if isinstance(b, dict):
                t = html.escape((b.get("title") or "").strip())
                u = html.escape((b.get("url") or "").strip())
                parts.append(f'<li><a href="{u}" target="_blank" rel="noopener">{t or u}</a></li>')
            else:
                parts.append(f"<li>{html.escape(str(b))}</li>")
        parts.append("</ul></div>")

    def get_items(name: str) -> List[Dict[str,Any]]:
        for s in digest.get("sections", []):
            if (s.get("name") or "").strip().lower() == name.strip().lower():
                return s.get("items") or []
        return []

    def section(title: str, key: str):
        items = get_items(key)
        parts.append(f"<h2>{html.escape(title)}</h2>")
        if not items:
            parts.append("<ul><li>Keine neuen, relevanten Meldungen.</li></ul>")
            return
        parts.append("<ul>")
        for it in items[:5]:
            if isinstance(it, dict):
                issuer    = html.escape((it.get("issuer") or "").strip())
                t         = html.escape((it.get("title") or "").strip() or "(ohne Titel)")
                u         = html.escape((it.get("url") or "").strip())
                date_iso  = (it.get("date_iso") or "").strip()
                date_txt  = parse_iso_or_ch(date_iso)
                summary   = html.escape((it.get("summary") or "").strip())
            else:
                issuer = ""; t = html.escape(str(it)); u = ""; date_txt = ""; summary = ""

            head = ""
            if issuer:   head += f'<span class="issuer">{issuer}</span> — '
            if date_txt: head += f"<strong>{date_txt}</strong> – "
            head += t
            line = f"<li>{head}"
            if u:
                line += f' (<a href="{u}" target="_blank" rel="noopener">Quelle</a>)'
            if summary:
                line += f"<br>{summary}"
            line += "</li>"
            parts.append(line)
        parts.append("</ul>")

    # Reihenfolge
    section("FINMA-Updates", "FINMA-Updates")
    section("Sanktionen & Embargos (SECO, OFAC, EU)", "Sanktionen & Embargos (SECO, OFAC, EU)")
    section("Branchenstimmung (Verbände & Medien)", "Branchenstimmung (Verbände & Medien)")
    section("Parlamentarische Agenda", "Parlamentarische Agenda")

    # Events
    parts.append("<h2>Nächste Events</h2>")
    parts.append("<ul>")
    if events:
        for e in events[:EVENTS_COUNT]:
            title_txt = html.escape((e.get("title") or "").strip() or "Event")
            url_txt   = html.escape((e.get("url") or "").strip())
            d_txt     = parse_iso_or_ch(e.get("date_iso") or "")
            head = ""
            if d_txt:
                head += f"<strong>{d_txt}</strong> – "
            head += title_txt
            if url_txt:
                head += f' (<a href="{url_txt}" target="_blank" rel="noopener">Link</a>)'
            parts.append(f"<li>{head}</li>")
    else:
        parts.append("<li>Derzeit keine kommenden Termine veröffentlicht.</li>")
    parts.append("</ul>")

    parts.append('<div class="iguv-disclaimer">© IGUV – Alle Rechte vorbehalten. Keine Haftung für die Richtigkeit der Daten; massgebend sind die verlinkten Originalquellen. Zeitfenster: FINMA/SECO/OFAC/EU 7 Tage; Verbände/Parlament bis 60 Tage; Events: nächste Termine.</div>')
    parts.append("</div>")
    return "\n".join(parts)

# ================== WP Endpoint ==================
def post_to_wp(html_inner: str):
    url = f"{WP_BASE}/wp-json/iguv/v1/weekly"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if WP_API_TOKEN:
        headers["X-IGUV-Token"] = WP_API_TOKEN  # optionaler Token-Fallback

    r = requests.post(
        url,
        auth=(WP_USERNAME, WP_APP_PASSWORD) if WP_USERNAME and WP_APP_PASSWORD else None,
        json={"html": html_inner.strip()},
        headers=headers,
        timeout=REQ_TIMEOUT
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP Update fehlgeschlagen: {r.status_code} {r.text}")
    print("SUCCESS: Weekly HTML via Endpoint gesetzt.")

# ================== MAIN ==================
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    print(f"Modell: {OPENAI_MODEL} | Timeout: {OPENAI_REQUEST_TIMEOUT_S}s")
    require_env()

    # Events lokal holen
    events = fetch_upcoming_iguv_events(WP_BASE, EVENTS_COUNT)

    # GPT-5 (Websearch) aufrufen
    print("KI-Zusammenfassung (websearch-gpt-5) …")
    messages = build_messages(events)
    digest = call_openai(messages)

    # HTML rendern
    print("HTML generieren …")
    html_out = render_html(digest, events)

    # Nach WordPress schreiben
    print("WordPress aktualisieren …")
    post_to_wp(html_out)

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
