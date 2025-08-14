import os, sys, re, json, datetime, traceback
from urllib.parse import urljoin, urlparse
from dateutil.tz import gettz
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ========= ENV Variablen =========
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # schlank & günstig für Summaries
WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_PAGE_ID       = os.environ.get("WP_PAGE_ID", "")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID  = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
TZ               = os.environ.get("TZ", "Europe/Zurich")

USER_AGENT = "IGUV-Weekly-Updater/2.0 (+https://iguv.ch)"

# ========= Hilfsfunktionen =========
def iso_now_local() -> str:
    return datetime.datetime.now(tz=gettz(TZ)).strftime("%Y-%m-%d %H:%M")

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def require_env():
    missing = []
    if not OPENAI_API_KEY: missing.append("OPENAI_API_KEY")
    if not WP_BASE: missing.append("WP_BASE")
    if not WP_PAGE_ID: missing.append("WP_PAGE_ID")
    if not WP_USERNAME: missing.append("WP_USERNAME")
    if not WP_APP_PASSWORD: missing.append("WP_APP_PASSWORD")
    if missing:
        raise RuntimeError("Fehlende ENV Variablen: " + ", ".join(missing))

def http_get(url: str, timeout: int = 40) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    return requests.get(url, headers=headers, timeout=timeout)

def domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""

# Gängige Datums-Muster (YYYY-MM-DD, DD.MM.YYYY, /YYYY/MM/)
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
        if "iso" in m.groupdict() and m.group("iso"):
            try:
                return datetime.datetime.strptime(m.group("iso"), "%Y-%m-%d").date()
            except Exception:
                pass
        if "dot" in m.groupdict() and m.group("dot"):
            try:
                return datetime.datetime.strptime(m.group("dot"), "%d.%m.%Y").date()
            except Exception:
                pass
        if "y" in m.groupdict() and "m" in m.groupdict():
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

def extract_candidates(list_url: str, days: int, keywords: list[str]) -> list[dict]:
    """Holt eine Übersichtsseite, sammelt Links, filtert grob nach Keywords & Datum (Heuristik)."""
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

    # Generischer Link-Scrape
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"].strip())
        text = " ".join(a.get_text(separator=" ", strip=True).split())
        if not text:
            continue
        # nur gleiche Domain (reduziert Rauschen)
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

    # Dedup
    dedup = {}
    for it in out:
        dedup[(it["title"], it["url"])] = it
    items = list(dedup.values())
    print(f"   -> {len(items)} Kandidaten nach Filter")
    return items

def summarize_with_openai(sections_payload: list[dict], max_per_section: int, style: str) -> dict:
    """Nimmt bereits gescrapte Kandidaten, lässt KI eine kompakte JSON-Zusammenfassung bauen."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    sys_prompt = (
        "Du bist ein präziser Nachrichten-Editor für Finanz-/Regulierungsthemen in der Schweiz. "
        f"Erstelle pro Sektion maximal {max_per_section} Punkte. "
        "Jeder Punkt enthält: title, url, date_iso (YYYY-MM-DD; falls None → heutiges Datum), summary (1–2 Sätze; warum relevant). "
        "Gib das Ergebnis als JSON-Objekt {\"sections\":[{\"name\":\"...\",\"items\":[...]}]} zurück."
    )

    user_payload = {
        "generated_at": iso_now_local(),
        "style": style,
        "sections": sections_payload
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

def fetch_wp_page():
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}?context=edit"
    r = requests.get(url, auth=(WP_USERNAME, WP_APP_PASSWORD), timeout=30, headers={"User-Agent": USER_AGENT})
    if r.status_code != 200:
        raise RuntimeError(f"WP GET fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

def replace_container_html(full_html: str, inner_html: str) -> str:
    # Ersetzt den Inhalt innerhalb <div id="WP_CONTAINER_ID">...</div> oder hängt Block an
    pattern = re.compile(rf'(<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>)(.*?)(</div>)',
                         re.IGNORECASE | re.DOTALL)
    if pattern.search(full_html):
        return pattern.sub(rf'\1{inner_html}\3', full_html)
    else:
        block = f'<div id="{WP_CONTAINER_ID}">{inner_html}</div>'
        return full_html + "\n" + block

def update_wp(inner_html: str):
    page = fetch_wp_page()
    current_html = page.get("content", {}).get("raw") or page.get("content", {}).get("rendered", "")
    new_html = replace_container_html(current_html, inner_html)
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}"
    r = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json={"content": new_html},
                      timeout=60, headers={"User-Agent": USER_AGENT})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP UPDATE fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

# ========= Main =========
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()

    cfg = load_yaml("data_sources.yaml")
    days = int(cfg.get("time_window_days", 7))
    keywords = cfg.get("keywords", [])
    sections_cfg = cfg.get("sections", [])
    max_bullets = int(cfg.get("summary", {}).get("max_bullets_per_section", 5))
    style = cfg.get("summary", {}).get("style", "Kompakt, sachlich.")

    print("Quellen scannen …")
    sections_payload = []
    for block in sections_cfg:
        name = block.get("name", "Sektion")
        urls = block.get("sources", [])
        all_items = []
        for u in urls:
            items = extract_candidates(u, days=days, keywords=keywords)
            all_items.extend(items)
        # jüngere zuerst
        all_items.sort(key=lambda it: it.get("date") or "", reverse=True)
        sections_payload.append({"name": name, "candidates": all_items})

    print("KI-Zusammenfassung …")
    digest = summarize_with_openai(sections_payload, max_per_section=max_bullets, style=style)
    total_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(f"Relevante Meldungen nach KI: {total_items}")

    print("HTML generieren …")
    html = to_html(digest, days=days)

    print("WordPress aktualisieren …")
    updated = update_wp(html)
    link = updated.get("link") or updated.get("guid", {}).get("rendered", "")
    mod  = updated.get("modified") or updated.get("modified_gmt", "")
    print(f"SUCCESS: WP aktualisiert. modified={mod} link={link}")

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
