#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IGUV Weekly Updater (HTML only, no PDF)
- GPT-5 (Responses API) mit optionalem Web-Search
- Output strikt nach Template: Kurzfassung, FINMA, Sanktionen (SECO/OFAC/EU), Medien-Monitoring, IGUV/InPaSu Events
- CH-Datumsformat
- Post an MU-Plugin: /wp-json/iguv/v1/weekly  (JSON { "html": "<...>" })
"""

import os, sys, json, html, re, traceback
from datetime import datetime, date
from typing import Any, Dict, List
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ====== ENV ======
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-5")
USE_WEBSEARCH  = os.getenv("USE_OPENAI_WEBSEARCH", "1") in ("1","true","TRUE")

# Request-Timeout für OpenAI (GANZ WICHTIG -> in update.yml auf 600 gesetzt)
OPENAI_REQUEST_TIMEOUT_S = int(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "600"))

# WordPress MU-Plugin Endpoint (Application Password Auth)
WP_BASE         = (os.getenv("WP_BASE", "") or "").rstrip("/")
WP_USERNAME     = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

# Events-Einstellungen
EVENTS_COUNT = int(os.getenv("EVENTS_COUNT", "3"))

# HTTP
USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/7.0; +https://iguv.ch)"
HDR_HTML = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
REQ_TIMEOUT = 30

# ====== Hilfen ======
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

def ch_date_str(d: date) -> str:
    months = ["Januar","Februar","März","April","Mai","Juni",
              "Juli","August","September","Oktober","November","Dezember"]
    return f"{d.day}. {months[d.month-1]} {d.year}"

def parse_iso_or_empty(s: str) -> str:
    s = (s or "").strip()
    if not s: return ""
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d").date()
        return ch_date_str(d)
    except Exception:
        # eventuell schon CH-Format
        return s

# ====== Events (nächste 3 von https://iguv.ch/event/) ======
DATE_RX = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
def fetch_upcoming_iguv_events(n=3) -> List[Dict[str, str]]:
    url = f"{WP_BASE}/event/"
    out: List[Dict[str, str]] = []
    try:
        r = requests.get(url, headers=HDR_HTML, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        today = date.today()

        # Heuristik: Links mit Datum im Text
        for a in soup.find_all("a", href=True):
            txt = " ".join((a.get_text(" ", strip=True) or "").split())
            m = DATE_RX.search(txt)
            if not m:
                continue
            dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                d = date(yyyy, mm, dd)
            except Exception:
                continue
            if d < today:
                continue
            out.append({
                "date_iso": d.isoformat(),
                "title": txt[:200],
                "url": requests.compat.urljoin(url, a["href"])
            })
        out.sort(key=lambda it: it["date_iso"])
    except Exception:
        pass
    return out[:n]

# ====== GPT-5 Responses API (mit Websearch) ======
def build_prompt_json(events: List[Dict[str,str]]) -> str:
    """
    Strenger JSON-Auftrag an GPT-5: Liefere validen JSON-Block mit briefing + sections.
    """
    spec = {
        "task": "Weekly-Report für unabhängige Vermögensverwalter (Schweiz) – letzte 7 Tage",
        "must_search_sources": {
            "FINMA": [
                "https://www.finma.ch/de/news/",
                "https://www.finma.ch/de/dokumentation/rundschreiben/"
            ],
            "SECO": [
                "https://www.seco.admin.ch/seco/de/home/Aussenwirtschaftspolitik_Wirtschaftliche_Zusammenarbeit/Wirtschaftsbeziehungen/exportkontrollen-und-sanktionen/sanktionen-embargos.html"
            ],
            "OFAC": [
                "https://ofac.treasury.gov/recent-actions"
            ],
            "EU": [
                "https://www.consilium.europa.eu/en/press/press-releases/"
            ],
            "Media": [
                "https://www.economiesuisse.ch/de/medien",
                "https://www.swissbanking.ch/de/medien",
                "https://www.vsv-asg.ch/de/aktuelles",
                "https://www.finews.ch/",
                "https://www.handelszeitung.ch/",
                "https://www.nzz.ch/themen/wirtschaft"
            ],
            "IGUV": [
                "https://iguv.ch/news/",
                "https://inpasu.ch/news/"
            ]
        },
        "format_requirements": {
            "language": "Deutsch",
            "date_format": "YYYY-MM-DD (ich rendere lokal als CH-Datum)",
            "sections_order": [
                "FINMA-Updates",
                "Sanktionen & Embargos (SECO, OFAC, EU)",
                "Medien-Monitoring",
                "Nächste IGUV/InPaSu Events"
            ],
            "limits": {
                "max_items_per_section": 5,
                "briefing_bullets": 5,
                "briefing_max_words": 18
            },
            "each_item_fields": ["title","url","date_iso","issuer","summary"]
        },
        "events_hint": events  # wird nur als Kontext mitgegeben (wir rendern diese separat/robust)
    }

    instructions = (
        "Du bist ein präziser Redakteur für Schweizer Vermögensverwalter. "
        "Untersuche gezielt die angegebenen Quellen (FINMA, SECO, OFAC, EU-Rat) sowie Medien/IGUV/InPaSu. "
        "Liefere NUR konkrete Änderungen/Fristen/Sanktionslisten/Rundschreiben/Pflichten mit Relevanz. "
        "Gib AUSSCHLIESSLICH einen validen JSON-Block mit folgendem Schema aus:\n\n"
        "{\n"
        '  "briefing": [ {"title":"...","url":"..."} ],\n'
        '  "sections": [\n'
        '    {"name":"FINMA-Updates","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD|","issuer":"FINMA","summary":"..."}]},\n'
        '    {"name":"Sanktionen & Embargos (SECO, OFAC, EU)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD|","issuer":"SECO|OFAC|EU-Rat","summary":"..."}]},\n'
        '    {"name":"Medien-Monitoring","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD|","issuer":"Medium|IGUV|InPaSu|VSV-ASG","summary":"..."}]},\n'
        '    {"name":"Nächste IGUV/InPaSu Events","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD|","issuer":"IGUV|InPaSu","summary":""}]}\n'
        "  ]\n"
        "}\n\n"
        "KEINE Erklärtexte außerhalb des JSON. Verwende echte, aktuelle Links. "
        "Falls in einer Sektion nichts Relevantes gefunden wurde, gib für diese Sektion ein leeres items-Array zurück."
    )

    user = {
        "role": "user",
        "content": json.dumps(spec, ensure_ascii=False)
    }
    system = {
        "role": "system",
        "content": instructions
    }
    # Responses API akzeptiert 'input' als Liste von Messages
    return json.dumps([system, user], ensure_ascii=False)

def call_gpt5_with_websearch(messages_json_ary_str: str) -> Dict[str, Any]:
    """
    Ruft die Responses API mit optionalem Web-Search-Tool auf und gibt geparstes JSON zurück.
    """
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)
    tools = [{"type": "web_search"}] if USE_WEBSEARCH else []

    # messages_json_ary_str := JSON-Array von {role, content}
    messages = json.loads(messages_json_ary_str)

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=messages,           # Liste von Rollen-Nachrichten
            tools=tools               # aktiviert Websearch (falls accountseitig verfügbar)
            # KEINE temperature setzen (gpt-5 akzeptiert nur default)
        )
        raw = getattr(resp, "output_text", "") or ""
    except Exception as e:
        print("Websearch nicht genutzt (Fallback):", repr(e))
        return {"briefing": [], "sections": []}

    raw = (raw or "").strip()
    # Der Assistent SOLL ausschließlich JSON liefern; robust parsen:
    try:
        # Falls der Assistent doch Text um den JSON legt, den inneren JSON-Block extrahieren
        start = raw.find("{")
        end   = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start:end+1]
        data = json.loads(raw)
        # Minimal validieren
        if "briefing" not in data: data["briefing"] = []
        if "sections" not in data: data["sections"] = []
        return data
    except Exception as e:
        print("WARN: KI-Output kein valider JSON. Rohtext (Anfang):", raw[:500])
        return {"briefing": [], "sections": []}

# ====== HTML Rendering ======
CSS = """
<style>
.iguv-weekly{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;line-height:1.55;font-size:16px}
.iguv-weekly h1{font-size:2.2rem;margin:.2rem 0;color:#0f2a5a;font-weight:700}
.iguv-weekly .meta{color:#666;margin-bottom:.8rem}
.iguv-weekly h2{font-size:1.35rem;margin:1.3rem 0 .5rem;color:#0f2a5a}
.iguv-weekly ul{margin:.5rem 0 1rem 1.2rem}
.iguv-weekly li{margin:.35rem 0}
.iguv-note{background:#f6f8fb;border-left:4px solid #0f2a5a;padding:.9rem;border-radius:.6rem;margin:.9rem 0 1.1rem}
.iguv-disclaimer{font-size:.9rem;color:#555;border-top:1px solid #e6e6e6;padding-top:.6rem;margin-top:1rem}
.issuer{color:#555}
</style>
"""

def to_html(digest: Dict[str, Any], events: List[Dict[str,str]]) -> str:
    """
    Rendert strukturiertes HTML analog PDF-Vorlage.
    - Briefing (4–5 Punkte)
    - FINMA-Updates
    - Sanktionen & Embargos (SECO, OFAC, EU)
    - Medien-Monitoring
    - Nächste IGUV/InPaSu Events (max 3, robust aus events-Argument)
    """
    dt = datetime.now()
    parts = [
        CSS,
        '<div class="iguv-weekly">',
        '<h1>Weekly-Updates</h1>',
        f'<div class="meta">Stand: {html.escape(ch_date_str(dt.date()))}</div>'
    ]

    # Kurzfassung
    briefing = digest.get("briefing") or []
    if briefing:
        parts.append('<div class="iguv-note"><strong>Kurzfassung (4–5 Punkte):</strong><ul>')
        for b in briefing[:5]:
            if isinstance(b, dict):
                t = html.escape((b.get("title") or "").strip())
                u = html.escape((b.get("url") or "").strip())
                if t and u:
                    parts.append(f'<li><a href="{u}" target="_blank" rel="noopener">{t}</a></li>')
                elif t:
                    parts.append(f'<li>{t}</li>')
                elif u:
                    parts.append(f'<li><a href="{u}" target="_blank" rel="noopener">{u}</a></li>')
            else:
                # Falls mal nur String kommt
                t = html.escape(str(b).strip())
                if t:
                    parts.append(f"<li>{t}</li>")
        parts.append("</ul></div>")

    # Helper zum Rendern normaler Sektionen
    def render_section(title: str, items: List[Dict[str,Any]]):
        parts.append(f"<h2>{html.escape(title)}</h2>")
        if not items:
            parts.append("<ul><li>Keine neuen, relevanten Meldungen in den letzten 7 Tagen.</li></ul>")
            return
        parts.append("<ul>")
        for it in items[:5]:
            if isinstance(it, dict):
                title_txt = html.escape((it.get("title") or "").strip())
                url_txt   = html.escape((it.get("url") or "").strip())
                issuer    = html.escape((it.get("issuer") or "").strip())
                date_iso  = (it.get("date_iso") or "").strip()
                date_txt  = parse_iso_or_empty(date_iso)
                summary   = html.escape((it.get("summary") or "").strip())
            else:
                # robust fallback
                title_txt = html.escape(str(it).strip())
                url_txt = issuer = date_txt = summary = ""

            head = ""
            if date_txt:
                head += f"<strong>{date_txt}</strong> – "
            head += title_txt if title_txt else "(ohne Titel)"
            if issuer:
                head += f' <span class="issuer">({issuer})</span>'

            line = f"<li>{head}"
            if url_txt:
                line += f' (<a href="{url_txt}" target="_blank" rel="noopener">Quelle</a>)'
            if summary:
                line += f"<br>{summary}"
            line += "</li>"
            parts.append(line)
        parts.append("</ul>")

    # Sektionen in gewünschter Reihenfolge
    sec_map = { (s.get("name") or "").strip(): s.get("items") or [] for s in (digest.get("sections") or []) }

    render_section("FINMA-Updates", sec_map.get("FINMA-Updates", []))
    render_section("Sanktionen & Embargos (SECO, OFAC, EU)", sec_map.get("Sanktionen & Embargos (SECO, OFAC, EU)", []))
    render_section("Medien-Monitoring", sec_map.get("Medien-Monitoring", []))

    # Events (verwende robust die lokal ermittelten)
    parts.append("<h2>Nächste IGUV/InPaSu Events</h2>")
    parts.append("<ul>")
    if events:
        for e in events[:EVENTS_COUNT]:
            title_txt = html.escape((e.get("title") or "").strip())
            url_txt   = html.escape((e.get("url") or "").strip())
            d_txt = parse_iso_or_empty(e.get("date_iso") or "")
            head = ""
            if d_txt:
                head += f"<strong>{d_txt}</strong> – "
            head += title_txt if title_txt else "(Event)"
            if url_txt:
                head += f' (<a href="{url_txt}" target="_blank" rel="noopener">Link</a>)'
            parts.append(f"<li>{head}</li>")
    else:
        parts.append("<li>Derzeit keine kommenden Termine veröffentlicht.</li>")
    parts.append("</ul>")

    parts.append('<div class="iguv-disclaimer">Massgebend sind die verlinkten Originalquellen. Zeitraum: letzte 7 Tage. IGUV-Events: nächste Termine.</div>')
    parts.append("</div>")
    return "\n".join(parts)

# ====== MU-Plugin Endpoint ======
def post_to_mu_plugin(html_inner: str):
    url = f"{WP_BASE}/wp-json/iguv/v1/weekly"
    print(f"WordPress (MU-Plugin) aktualisieren: {url}")
    r = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD),
                      json={"html": html_inner.strip()}, timeout=REQ_TIMEOUT,
                      headers={"User-Agent": USER_AGENT, "Accept":"application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Endpoint-Update fehlgeschlagen: {r.status_code} {r.text}")
    print("SUCCESS: Weekly HTML via Endpoint gesetzt.")

# ====== MAIN ======
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()

    # 1) Events vorab lokal sammeln (robust)
    events = fetch_upcoming_iguv_events(EVENTS_COUNT)

    # 2) GPT-5 Responses API mit Web-Search
    print("KI-Zusammenfassung (GPT-5 + Websearch) …")
    messages_json_ary = build_prompt_json(events)
    digest = call_gpt5_with_websearch(messages_json_ary)

    # 3) HTML rendern
    print("HTML generieren …")
    html_content = to_html(digest, events)

    # 4) In WordPress via MU-Plugin schreiben
    print("WordPress aktualisieren …")
    post_to_mu_plugin(html_content)

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
