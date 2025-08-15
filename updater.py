#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IGUV Weekly Updater – Prompt-basierte Variante (schlankes HTML, ohne Design)
- Schickt deinen detaillierten Prompt an die OpenAI Responses API (gpt-5)
- Aktiviert Websuche (tools=[{"type":"web_search"}], ohne tool_choice)
- Retries bei transienten Verbindungsfehlern
- Postet das Ergebnis an /wp-json/iguv/v1/weekly (Shortcode zeigt es wie bisher an)
- Füllt 'Next Events' ggf. via einfachem Scraper von https://iguv.ch/event/
"""

import os, sys, re, html, time, traceback
from datetime import datetime, date
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

# ================== ENV ==================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = (os.getenv("OPENAI_MODEL", "") or "gpt-5").strip()
OPENAI_REQUEST_TIMEOUT_S = int(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "900"))
USE_WEBSEARCH  = os.getenv("USE_OPENAI_WEBSEARCH", "1").lower() not in ("0","false","off")

WP_BASE         = (os.getenv("WP_BASE", "") or "").rstrip("/")
WP_USERNAME     = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_API_TOKEN    = os.getenv("WP_API_TOKEN", "")  # optional

EVENTS_COUNT = int(os.getenv("EVENTS_COUNT", "5"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/Prompt-Mode; +https://iguv.ch)"
REQ_TIMEOUT = 45

def require_env():
    missing = []
    for k,v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "WP_BASE": WP_BASE,
        "WP_USERNAME": WP_USERNAME,
        "WP_APP_PASSWORD": WP_APP_PASSWORD,
    }.items():
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError("Fehlende ENV Variablen: " + ", ".join(missing))

# ================== Event-Scraper ==================
DATE_RX_NUM = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
MONTHS_DE = {
    "januar":1,"februar":2,"maerz":3,"märz":3,"april":4,"mai":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"dezember":12
}
DATE_RX_TEXT = re.compile(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s*(\d{4})")

def _extract_date_any(txt: str) -> Optional[date]:
    txt = (txt or "").strip()
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
            dd = int(m2.group(1)); mon = m2.group(2).strip().lower(); yyyy = int(m2.group(3))
            mm = MONTHS_DE.get(mon)
            if mm: return date(yyyy, mm, dd)
        except Exception:
            pass
    return None

def http_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"WARN: GET failed for {url}: {repr(e)}")
        return None

def fetch_upcoming_events(base_url: str, n=5) -> List[Dict[str,str]]:
    url = f"{base_url}/event/"
    out: List[Dict[str,str]] = []
    html_data = http_get(url)
    if not html_data:
        return out
    soup = BeautifulSoup(html_data, "lxml")
    today = date.today()
    candidates = []

    # <time datetime="YYYY-MM-DD">
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
        title = " ".join((parent.get_text(" ", strip=True) or "").split())[:220]
        if href:
            href = requests.compat.urljoin(url, href)
        candidates.append({"date_iso": d.isoformat(), "title": title, "url": href or url})

    # generisch
    for tag in soup.find_all(["article","li","div","span","a","dd","dt"]):
        txt = " ".join((tag.get_text(" ", strip=True) or "").split())
        d = _extract_date_any(txt) or _extract_date_any(" ".join((tag.parent.get_text(" ", strip=True) or "").split()) if tag.parent else "")
        if not d or d < today:
            continue
        a = tag if (tag.name == "a" and tag.has_attr("href")) else tag.find("a", href=True)
        href = a["href"] if a else None
        if href:
            href = requests.compat.urljoin(url, href)
        title = txt[:220]
        candidates.append({"date_iso": d.isoformat(), "title": title, "url": href or url})

    seen = set()
    for e in sorted(candidates, key=lambda it: it["date_iso"]):
        key = (e["date_iso"], e.get("url",""), e.get("title","")[:80])
        if key in seen: 
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= n:
            break
    return out

def ch_date_str(d: date, with_time: Optional[datetime]=None) -> str:
    months = ["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"]
    base = f"{d.day}. {months[d.month-1]} {d.year}"
    if with_time is not None:
        base += with_time.strftime(", %H:%M")
    return base

# ================== OpenAI Call ==================
SYSTEM_TEXT = (
    "Du bist ein präziser, faktenorientierter Redakteur für Schweizer Vermögensverwalter (UVV/EAM). "
    "Wenn aktuelle Informationen benötigt werden, nutze die Websuche. "
    "Arbeite quellenbasiert, knapp, pro Punkt max. 12 Wörter als Titel/Zusammenfassung."
)

PROMPT_TEXT = """Erstelle mir einen gut recherchierten und kompakten Bericht mit relevanten Themen speziell für unabhängige Vermögensverwalter mit folgenden Informationen zusammen.:

Titel: Weekly-Updates

Regeln:
Erstelle einen kompakten, professionellen Wochenbericht für Vermögensverwalter (UVV/EAM) in deutscher Sprache.
Zeige die wichtigsten, relevanten Änderungen der letzten Zeit (siehe Vorgaben pro Abschnitt).
Jede Quelle als anklickbaren Link einfügen.
Jede Meldung als kurze, sinnvolle Zusammenfassung/Titel (max. 12 Wörter).
Bei fehlenden Ergebnissen: „Keine neuen, relevanten Änderungen“.
Keine Informationen die älter sind als 12 Monate.

Untertitel: FINMA-Updates
Regeln:
Quelle: www.finma.ch
Suche nach für Vermögensverwalter relevanten Updates wie Medienmitteilungen, Rundschreiben, Konsultationspapiere und Publikationen.
Zeitraum: nicht älter als 12 Monate.
Mindestens 1, maximal 5 Updates.
Format: • [Datum] – [Titel] (Link)

Untertitel: AO-Änderungen
Regeln:
Berücksichtige: AOOS, OSFIN, FINControl Suisse, OSIF, SO-FIT.
Suche nach Änderungen in Reglementen, Gebühren, Statuten, Verordnungen, Organisationsstruktur oder News.
Nur offizielle Mitteilungen
Mindestens 1 Update pro AO, maximal 3 Updates.
Format: • [Datum] – [Titel] (Link)

Untertitel: Parlamentarische Agenda
Regeln:
Gehe auf https://www.parlament.ch/de/ratsbetrieb/suche-curia-vista
Setze diese Filter: Zeitraum: Letzte 12 Monate
Geschäftstyp: „Vorstösse“ und „Geschäfte“
Suchbegriffe: FINMA, Vermögensverwalter, Aufsichtsorganisation, Finanzmarktaufsicht, FINIG, FIDLEV
Sortierung: „Neueste zuerst“
Zeitraum: nicht älter als 12 Monate.
Mindestens 1, maximal 5 Updates.
Format: • [Datum] – [Titel/Vorstoß] (Link)

Untertitel: Branchenstimmung (Verbände)
Regeln:
Berücksichtige Stellungnahmen von: economiesuisse, SwissBanking, inpasu.ch/news/, Schweizerischer Gewerbeverband.
Direkt auf den Verbandseiten suchen:
- economiesuisse: https://www.economiesuisse.ch/de → Suchfeld → „FINMA“, „Regulierung“, „Finanzplatz“
- SwissBanking: https://www.swissbanking.ch/de/themen
- Gewerbeverband: https://sgv-usam.ch/de/ → Medien
- inpasu.ch/news: Direkter Zugriff, meist schon nach Datum sortiert.
Spezifische Suchstrings in der Websuche zulassen (site-Suche):
- site:economiesuisse.ch FINMA
- site:swissbanking.ch Regulierung
- site:sgv-usam.ch Vermögensverwalter
- site:inpasu.ch/news/ Regulierung
Themen: Regulierung, Wirtschaftslage, Finanzmarktpolitik.
Mindestens 1 Update pro Verband, maximal 4 Updates.
Format: • [Datum] – [Thema] (Link)

Untertitel: Medien-Monitoring Regeln (optimierte Fassung):
Suche nur in folgenden Quellen:
site:finews.ch
site:fuw.ch
site:letemps.ch
Weitere relevante Fachportale: site:assetmanagement.ch, site:hedgework.de, site:citywire.com (DACH-Editionen)
Zeitraum: letzte 12 Monate
Themen: Nur Artikel zu Vermögensverwaltern, FINMA, Aufsichtsorganisationen (AOOS, OSFIN, FINcontrol, OSIF, SO-FIT), Regulierung, Finanzplatz Schweiz.
Ausschluss: Keine Werbung, Produktankündigungen, PR-Texte oder reine Personalwechsel ohne regulatorischen Bezug.
Format: • [Datum] – [Beschreibung] (Link)
 

Untertitel: Embargos & Sanktionen
Regeln:
Quellen:
- SECO
- EU Council / https://www.consilium.europa.eu/de/explainers/
- OFAC / https://ofac.treasury.gov/recent-actions
Inhalte: Neue Sanktionen, gelöschte Sanktionen, Listenänderungen.
Je die letzten Updates von SECO, EU und USA.
Format: • [Datum] – [Beschreibung] (Link)

Untertitel: Next Events
Öffne die Seite https://iguv.ch/event/ und analysiere den dortigen Event-Kalender. 
Lies alle Event-Einträge aus, die in der Zukunft liegen (aktuelles Datum beachten). 
Extrahiere aus jedem Eintrag:
1. Datum
2. Titel der Veranstaltung
3. Ort
4. Den direkten Link zur Event-Detailseite
Gib nur die nächsten 3–5 zukünftigen Termine aus. 
Format: • [Datum] – [Titel] – [Ort] ([Link])

WICHTIG – AUSGABEFORMAT:
Gib die gesamte Antwort als valides, schlichtes HTML ohne Inline-Styles aus:
<h2>Weekly-Updates</h2>
<h3>FINMA-Updates</h3>
<ul><li>[Datum] – [Titel] (<a href="URL" target="_blank" rel="noopener">Link</a>)</li>…</ul>
<h3>AO-Änderungen</h3>
<ul>…</ul>
<h3>Parlamentarische Agenda</h3>
<ul>…</ul>
<h3>Branchenstimmung</h3>
<ul>…</ul>
<h3>Medien-Monitoring</h3>
<ul>…</ul>
<h3>Embargos & Sanktionen</h3>
<ul>…</ul>
<h3>Next Events</h3>
<ul>…</ul>
Keine zusätzlichen Absätze oder Styles.
"""

def ask_openai_html() -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    messages = [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": PROMPT_TEXT},
    ]

    kwargs: Dict[str, Any] = {"model": OPENAI_MODEL, "input": messages}
    if USE_WEBSEARCH:
        kwargs["tools"] = [{"type":"web_search"}]  # kein tool_choice setzen!

    resp = client.responses.create(**kwargs)

    # Best-effort: Tool-Nutzung loggen (falls vorhanden)
    try:
        tool_uses = 0
        if hasattr(resp, "output") and isinstance(resp.output, list):
            for block in resp.output:
                if getattr(block, "type", "") == "tool_call":
                    tool_uses += 1
        print(f"INFO: erkannte Tool-Aufrufe: {tool_uses}", file=sys.stderr)
    except Exception:
        pass

    text = getattr(resp, "output_text", "") or ""
    return text.strip()

# ================== Postprocessing: Events anhängen/ersetzen ==================
def ensure_next_events_section(model_html: str, base_url: str) -> str:
    """Falls das Modell keine <h3>Next Events</h3>-Sektion erzeugt hat,
    oder die Liste leer ist, ergänzen wir sie via Scraper."""
    has_section = re.search(r"<h3>\s*Next Events\s*</h3>", model_html, re.IGNORECASE) is not None
    need_append = (not has_section)

    if not need_append:
        # Prüfen, ob direkt danach eine UL mit Li kommt
        after = re.search(r"(<h3>\s*Next Events\s*</h3>)(?P<tail>.*)", model_html, re.IGNORECASE|re.DOTALL)
        if after:
            tail = after.group("tail")
            # falls keine <li> gefunden → als leer betrachten
            if re.search(r"<li>.*?</li>", tail, re.IGNORECASE|re.DOTALL) is None:
                need_append = True
        else:
            need_append = True

    if not need_append:
        return model_html

    ev = fetch_upcoming_events(base_url, EVENTS_COUNT)
    if not ev:
        # Nichts zu ergänzen
        return model_html

    lines = []
    lines.append("<h3>Next Events</h3>")
    lines.append("<ul>")
    for e in ev:
        dtxt = e.get("date_iso","")[:10]
        # Datum hübsch machen
        try:
            d = datetime.strptime(dtxt, "%Y-%m-%d").date()
            dpretty = ch_date_str(d)
        except Exception:
            dpretty = dtxt
        title = html.escape((e.get("title") or "Event").strip())
        url = html.escape((e.get("url") or "").strip())
        lines.append(f'<li>{dpretty} – {title} (<a href="{url}" target="_blank" rel="noopener">Link</a>)</li>')
    lines.append("</ul>")

    if has_section:
        # vorhandene (leere) Sektion ersetzen
        model_html = re.sub(r"(<h3>\s*Next Events\s*</h3>)(\s*<ul>.*?</ul>)?",
                            "\n".join(lines),
                            model_html, flags=re.IGNORECASE|re.DOTALL)
    else:
        # ans Ende hängen
        model_html = model_html.rstrip() + "\n" + "\n".join(lines)
    return model_html

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
    print("== IGUV Prompt-Weekly startet ==")
    print(f"Modell: {OPENAI_MODEL} | Timeout: {OPENAI_REQUEST_TIMEOUT_S}s | Websuche: {'AN' if USE_WEBSEARCH else 'AUS'}")
    require_env()

    # Retries mit Exponential Backoff
    max_retries = 3
    backoff = 10
    last_err = None
    model_html = ""
    for attempt in range(1, max_retries+1):
        try:
            model_html = ask_openai_html()
            if not model_html:
                raise RuntimeError("Leere Antwort vom Modell.")
            break
        except Exception as e:
            last_err = e
            if attempt == max_retries:
                raise
            print(f"WARN: OpenAI-Versuch {attempt} fehlgeschlagen: {repr(e)} – retry in {backoff}s")
            time.sleep(backoff)
            backoff *= 2

    # Falls Next-Events fehlen/leer → via Scraper ergänzen
    try:
        model_html = ensure_next_events_section(model_html, WP_BASE)
    except Exception as e:
        print("WARN: ensure_next_events_section:", repr(e))

    # Kopfzeile ggf. mit aktuellem Zeitstempel ersetzen
    now = datetime.now()
    now_txt = ch_date_str(now.date(), with_time=now)
    model_html = re.sub(
        r"(<h1>\s*Weekly-Updates\s*–\s*Stand:\s*)(\[.*?\]|\d{1,2}\.\s*[A-Za-zäöüÄÖÜ]+?\s*\d{4},\s*\d{2}:\d{2})(\s*</h1>)",
        rf"\1{now_txt}\3",
        model_html,
        flags=re.IGNORECASE
    )
    if not re.search(r"<h1>.*Weekly-Updates", model_html, re.IGNORECASE):
        # falls das Modell den Kopf nicht gesetzt hat, setzen wir ihn
        model_html = f"<h1>Weekly-Updates – Stand: {now_txt}</h1>\n" + model_html

    print("WordPress aktualisieren …")
    post_to_wp(model_html)

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
