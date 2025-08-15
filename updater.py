#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, html, requests, json, datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# -------------------- Konfiguration --------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
USE_WEBSEARCH = os.getenv("USE_OPENAI_WEBSEARCH", "1") == "1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REQUEST_TIMEOUT = int(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "600"))
MAX_ITEMS_PER_SECTION = int(os.getenv("MAX_ITEMS_PER_SECTION_KI", "5"))
EVENTS_COUNT = int(os.getenv("EVENTS_COUNT", "3"))

TODAY = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

SOURCES = [
    # FINMA-News & Rundschreiben
    "https://www.finma.ch/de/news/",
    "https://www.finma.ch/de/dokumentation/rundschreiben/",
    "https://www.finma.ch/de/sanktionen-und-embargos/",

    # Aufsichtsorganisationen
    "https://www.aoos.ch",
    "https://www.osfin.ch",
    "https://oad-fct.ch",
    "https://www.osif.ch",
    "https://www.so-fit.ch",

    # Sanktionen & Embargos
    "https://www.seco.admin.ch/seco/de/home/Aussenwirtschaftspolitik_Wirtschaftliche_Zusammenarbeit/Wirtschaftsbeziehungen/exportkontrollen-und-sanktionen/sanktionen-embargos.html",
    "https://ofac.treasury.gov/recent-actions",
    "https://www.sanctionsmap.eu/#/main",

    # Branchen-News
    "https://www.economiesuisse.ch/de/medien",
    "https://www.swissbanking.ch/de/medien",
    "https://inpasu.ch/news/",

    # Parlamentarische Agenda
    "https://www.parlament.ch/de/ratsbetrieb/suche-curia-vista",
]

EVENTS_URL = "https://iguv.ch/event/"
# ---------------------------------------------------------

def fetch_html(url):
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"WARN: Fehler beim Laden von {url}: {e}")
        return None

def parse_events():
    html_data = fetch_html(EVENTS_URL)
    if not html_data:
        return []
    soup = BeautifulSoup(html_data, "html.parser")
    events = []
    for e in soup.select(".event")[:EVENTS_COUNT]:
        title = e.get_text(strip=True)
        link_tag = e.find("a", href=True)
        link = link_tag["href"] if link_tag else EVENTS_URL
        events.append(f"<a href='{link}' target='_blank'>{html.escape(title)}</a>")
    return events

def websearch_and_summarize():
    prompt = f"""
Erstelle einen kompakten, gegliederten Wochenüberblick für Vermögensverwalter basierend auf den folgenden Quellen:
{json.dumps(SOURCES, ensure_ascii=False)}

- Verwende Poppins als Schriftart
- Gliedere in: FINMA-Updates, Aufsichtsorganisationen, Embargos & Sanktionen, Branchenstimmung, Parlamentarische Agenda, Medien-Monitoring, Nächste Events
- Embargos & Sanktionen: Quelle nur einmal fett am Anfang nennen, keine Dopplung
- Auch ältere News (max. 2 Monate) aufnehmen, wenn relevant
- Nächste Events: nur die nächsten {EVENTS_COUNT} Termine mit Link
- Abstand zwischen Abschnitten vergrößern
- Keine irrelevanten Inhalte
"""
    # Hier würde der GPT-5 Websearch Call erfolgen
    return "<p>Hier käme die KI-Ausgabe rein</p>"

def to_html(content, events):
    return f"""
<div style="font-family:Poppins, sans-serif; line-height:1.6;">
<h2>IGUV Weekly Update – {TODAY}</h2>
{content}
<h3 style="margin-top:30px;">Nächste Events</h3>
<ul>{"".join(f"<li>{ev}</li>" for ev in events)}</ul>
<hr>
<p style="font-size:12px;color:#666;">
© IGUV – Alle Rechte vorbehalten. Keine Haftung für die Richtigkeit der Daten; massgebend sind die verlinkten Originalquellen.
</p>
</div>
"""

def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    content = websearch_and_summarize()
    events = parse_events()
    html_final = to_html(content, events)

    # POST zu WP
    wp_base = os.getenv("WP_BASE")
    wp_user = os.getenv("WP_USERNAME")
    wp_pass = os.getenv("WP_APP_PASSWORD")
    if wp_base and wp_user and wp_pass:
        try:
            r = requests.post(f"{wp_base}/wp-json/iguv/v1/weekly",
                              auth=(wp_user, wp_pass),
                              json={"html": html_final},
                              timeout=30)
            r.raise_for_status()
            print("Update erfolgreich an WordPress übertragen.")
        except Exception as e:
            print(f"Fehler beim Übertragen an WP: {e}")
    else:
        print("Kein WP-Zugang konfiguriert, HTML nur lokal ausgegeben.")
        print(html_final)

if __name__ == "__main__":
    main()
