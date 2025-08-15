#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IGUV Weekly Updater – HTML only (mit erzwungener Websuche via Responses API)

Wesentliche Punkte:
- Modell: gpt-5 (env-override OPENAI_MODEL möglich)
- Websuche: tools=[{"type":"web_search"}] + tool_choice="required" (erzwingt Nutzung)
- Strikte Abschnittsregeln: Zeitfenster, min/max Items
- FINMA inkl. Sanktionen/Embargos; SECO, OFAC, EU; CH-Parlament (Curia Vista); Verbände/Medien (bis 60 Tage)
- Events-Parser für /event/ robuster (time/datetime, data-*, dt/dd, deutsche Monatsnamen)
- HTML-Ausgabe ohne <b>-Tags, saubere CSS-Fettschrift
- WordPress-Update via MU-Plugin-Endpoint /wp-json/iguv/v1/weekly (optional X-IGUV-Token)
"""

import os, sys, json, html, re, traceback
from datetime import datetime, date
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup

# ================== ENV ==================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = (os.getenv("OPENAI_MODEL", "") or "gpt-5").strip()
OPENAI_REQUEST_TIMEOUT_S = int(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "600"))
USE_WEBSEARCH = os.getenv("USE_OPENAI_WEBSEARCH", "1") not in ("0","false","False","")

WP_BASE         = (os.getenv("WP_BASE", "") or "").rstrip("/")
WP_USERNAME     = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_API_TOKEN    = os.getenv("WP_API_TOKEN", "")  # optional

EVENTS_COUNT = int(os.getenv("EVENTS_COUNT", "3"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/11.0; +https://iguv.ch)"
REQ_TIMEOUT = 45

# ================== Helpers ==================
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
    months = ["Januar","Februar","Maerz","April","Mai","Juni",
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

_TAG_RX = re.compile(r"<[^>]+>")
WS_RX   = re.compile(r"\s+")

def sanitize_text(s: Optional[str]) -> str:
    if not s: return ""
    # decode HTML entities, strip tags, normalize whitespace
    s = html.unescape(s)
    s = _TAG_RX.sub("", s)
    s = WS_RX.sub(" ", s).strip()
    return s

# ================== Events (lokal aus /event/) ==================
DATE_RX_NUM = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
MONTHS_DE = {
    "januar":1,"februar":2,"maerz":3,"märz":3,"april":4,"mai":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"dezember":12
}
DATE_RX_TEXT = re.compile(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s*(\d{4})")

def _extract_date_any(txt: str) -> Optional[date]:
    txt = (txt or "").strip()
    if not txt: return None
    m = DATE_RX_NUM.search(txt)
    if m:
        try:
            dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(yyyy, mm, dd)
        except Exception:
            pass
    m2 = DATE_RX_TEXT.search(txt)
    if m2:
        try:
            dd = int(m2.group(1))
            mon = m2.group(2).strip().lower()
            yyyy = int(m2.group(3))
            mm = MONTHS_DE.get(mon, None)
            if mm:
                return date(yyyy, mm, dd)
        except Exception:
            pass
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

        # 1) <time datetime="YYYY-MM-DD">Titel</time>
        for t in soup.find_all("time"):
            d_iso = (t.get("datetime") or "").strip()
            d = None
            if d_iso:
                try:
                    d = datetime.fromisoformat(d_iso[:10]).date()
                except Exception:
                    d = None
            if not d:
                d = _extract_date_any(t.get_text(" ", strip=True))
            if not d or d < today:
                continue
            parent = t.find_parent(["article","li","div","section"]) or t
            a = parent.find("a", href=True) or t.find("a", href=True)
            href = a["href"] if a else None
            title = sanitize_text(parent.get_text(" ", strip=True))[:220]
            if href:
                href = requests.compat.urljoin(url, href)
            candidates.append({"date_iso": d.isoformat(), "title": title, "url": href or url})

        # 2) Generisch: jeder Knoten + Fallback auf Elterntext
        for tag in soup.find_all(["article","li","div","span","a","dd","dt"], recursive=True):
            txt = sanitize_text(tag.get_text(" ", strip=True))
            d = _extract_date_any(txt) or _extract_date_any(sanitize_text(tag.parent.get_text(" ", strip=True)) if tag.parent else "")
            if not d or d < today:
                continue
            a = tag if (tag.name == "a" and tag.has_attr("href")) else tag.find("a", href=True)
            href = a["href"] if a else None
            if href:
                href = requests.compat.urljoin(url, href)
            title = txt[:220]
            candidates.append({"date_iso": d.isoformat(), "title": title, "url": href or url})

        # Dedup & sort
        seen = set()
        for e in sorted(candidates, key=lambda it: it["date_iso"]):
            key = (e["date_iso"], e.get("url",""), e.get("title","")[:80])
            if key in seen: 
                continue
            seen.add(key)
            out.append(e)
            if len(out) >= n: break
    except Exception as e:
        print("WARN: fetch_upcoming_iguv_events:", repr(e))
    return out

# ================== OpenAI (Responses API) ==================
def build_messages(events: List[Dict[str,str]]) -> List[Dict[str,str]]:
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
        "rules": {
            "require_web_search": True,
            "time_windows": {
                "finma_days": 7,
                "sanctions_days": 7,
                "media_days": 60,
                "parliament_days": 60
            },
            "limits": {
                "finma_min": 1, "finma_max": 5,
                "ao_min": 1, "ao_max": 5,
                "parl_min": 2, "parl_max": 7,
                "media_min": 1, "media_max": 7,
                "sanctions_min": 1, "sanctions_max": 7
            }
        },
        "must_search_sources": must_sources,
        "format_requirements": {
            "language": "Deutsch",
            "sections_order": [
                "FINMA-Updates",
                "AO-Änderungen (AOOS, OSFIN, OAD FCT, OSIF, SO-FIT)",
                "Sanktionen & Embargos (SECO, OFAC, EU)",
                "Branchenstimmung (Verbände & Medien)",
                "Parlamentarische Agenda",
                "Nächste Events"
            ],
            "each_item_fields": ["title","url","date_iso","issuer","summary"]
        },
        "events_hint": events
    }

    sys = (
        "Du bist Redakteur für Schweizer Vermögensverwalter. NUTZE ZWINGEND die Websuche (tool web_search). "
        "Durchsuche FINMA (News, Rundschreiben, Sanktionen & Embargos), SECO, OFAC, EU (inkl. sanctionsmap.eu), "
        "Verbände/Medien (bis 60 Tage) und CH-Parlament (bis 60 Tage). "
        "Erzwinge: FINMA≥1, AO≥1, Parlament≥2, alle ≤Max laut Regeln. Nur fachlich relevante Inhalte "
        "(Rundschreiben, Aufsichtsmitteilungen, Sanktionslisten/GL/Designations, branchenrelevante Verbands-/Medienberichte, "
        "parlamentarische Vorstösse zu AML/Sanktionen/Finanzmarkt). "
        "Formatiere AUSSCHLIESSLICH als JSON:\n"
        "{\n"
        '  "briefing":[{"title":"...","url":"..."}],\n'
        '  "sections":[\n'
        '    {"name":"FINMA-Updates","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"FINMA","summary":"..."}]},\n'
        '    {"name":"AO-Änderungen (AOOS, OSFIN, OAD FCT, OSIF, SO-FIT)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"AO","summary":"..."}]},\n'
        '    {"name":"Sanktionen & Embargos (SECO, OFAC, EU)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"SECO|OFAC|EU|EU-Rat","summary":"..."}]},\n'
        '    {"name":"Branchenstimmung (Verbände & Medien)","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"economiesuisse|SwissBanking|VSV-ASG|IGUV|Medium","summary":"..."}]},\n'
        '    {"name":"Parlamentarische Agenda","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"Schweizer Parlament","summary":"..."}]},\n'
        '    {"name":"Nächste Events","items":[{"title":"...","url":"...","date_iso":"YYYY-MM-DD","issuer":"IGUV|InPaSu","summary":""}]}\n'
        "  ]\n"
        "}\n"
        "Kein Fliesstext außerhalb des JSON. Füge KEIN HTML in die Felder ein."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(spec, ensure_ascii=False)}
    ]

def call_openai(messages: List[Dict[str,str]]) -> Dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    tools = [{"type": "web_search"}] if USE_WEBSEARCH else None
    print(f"OpenAI: Modell={OPENAI_MODEL} | Tools(web_search)={'AN' if tools else 'AUS'} | Timeout={OPENAI_REQUEST_TIMEOUT_S}s")

    try:
        kwargs = dict(model=OPENAI_MODEL, input=messages)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "required"  # zwingt tool-Nutzung
        resp = client.responses.create(**kwargs)

        # Diagnostik: versuchen, Tool-Nutzung zu erkennen (best-effort)
        try:
            tool_uses = 0
            # je nach SDK-Struktur defensiv parsen
            if hasattr(resp, "output") and isinstance(resp.output, list):
                for block in resp.output:
                    if getattr(block, "type", "") == "tool_call":
                        tool_uses += 1
            print(f"INFO: erkannte Tool-Aufrufe: {tool_uses}")
        except Exception:
            pass

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
        # Sicherheit: alle Strings säubern
        for sec in data.get("sections", []):
            for it in sec.get("items", []) or []:
                for k in ("title","url","date_iso","issuer","summary"):
                    if k in it:
                        it[k] = sanitize_text(it.get(k))
        return data
    except Exception:
        print("WARN: KI-Output kein valider JSON. Rohtext (Anfang):", raw[:600])
        return {"briefing": [], "sections": []}

# ================== HTML Rendering ==================
CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap');
  .iguv-weekly{font-family:'Poppins',system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;line-height:1.7;font-size:16px}
  .iguv-weekly h1{font-size:2.2rem;margin:0 0 .8rem;color:#0f2a5a;font-weight:700;text-align:left}
  .iguv-weekly .meta{color:#666;margin:0 0 1.4rem;text-align:left}
  .iguv-weekly h2{font-size:1.4rem;margin:1.8rem 0 .9rem;color:#0f2a5a}
  .iguv-weekly ul{margin:.7rem 0 1.4rem 1.4rem}
  .iguv-weekly li{margin:.55rem 0}
  .iguv-note{background:#f6f8fb;border-left:4px solid #0f2a5a;padding:1rem;border-radius:.6rem;margin:1rem 0 1.6rem}
  .iguv-disclaimer{font-size:.9rem;color:#555;border-top:1px solid #e6e6e6;padding-top:.9rem;margin-top:1.4rem}
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

    def section(title: str, key: str, empty_msg: str = "Keine neuen, relevanten Meldungen."):
        items = get_items(key)
        parts.append(f"<h2>{html.escape(title)}</h2>")
        if not items:
            parts.append(f"<ul><li>{html.escape(empty_msg)}</li></ul>")
            return
        parts.append("<ul>")
        for it in items[:7]:
            issuer    = html.escape(sanitize_text(it.get("issuer")))
            t         = html.escape(sanitize_text(it.get("title") or "(ohne Titel)"))
            u         = html.escape((it.get("url") or "").strip())
            date_iso  = (it.get("date_iso") or "").strip()
            date_txt  = parse_iso_or_ch(date_iso)
            summary   = html.escape(sanitize_text(it.get("summary")))
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

    section("FINMA-Updates", "FINMA-Updates")
    section("AO-Änderungen (AOOS, OSFIN, OAD FCT, OSIF, SO-FIT)", "AO-Änderungen (AOOS, OSFIN, OAD FCT, OSIF, SO-FIT)")
    section("Sanktionen & Embargos (SECO, OFAC, EU)", "Sanktionen & Embargos (SECO, OFAC, EU)")
    section("Branchenstimmung (Verbände & Medien)", "Branchenstimmung (Verbände & Medien)")
    section("Parlamentarische Agenda", "Parlamentarische Agenda")

    parts.append("<h2>Nächste Events</h2>")
    parts.append("<ul>")
    if events:
        for e in events[:EVENTS_COUNT]:
            title_txt = html.escape(sanitize_text(e.get("title") or "Event"))
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
        headers["X-IGUV-Token"] = WP_API_TOKEN

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
    print(f"Modell: {OPENAI_MODEL} | Timeout: {OPENAI_REQUEST_TIMEOUT_S}s | Websuche: {'AN' if USE_WEBSEARCH else 'AUS'}")
    require_env()

    events = fetch_upcoming_iguv_events(WP_BASE, EVENTS_COUNT)
    print(f"INFO: Events gefunden: {len(events)} (limit={EVENTS_COUNT})")

    print("KI-Zusammenfassung (gpt-5 + erzwungene Websuche) …")
    messages = build_messages(events)
    digest = call_openai(messages)

    # Abschnitts-Diagnostik (Min/Max Sichtkontrolle)
    try:
        for s in (digest.get("sections") or []):
            name = s.get("name","")
            cnt  = len(s.get("items") or [])
            print(f"INFO: Abschnitt '{name}': {cnt} Items")
    except Exception:
        pass

    print("HTML generieren …")
    html_out = render_html(digest, events)

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
