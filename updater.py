import os, sys, re, json, datetime, traceback
from urllib.parse import urljoin, urlparse
from dateutil.relativedelta import relativedelta
from dateutil.tz import gettz
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ========= ENV (aus GitHub Secrets) =========
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4.1")
WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_PAGE_ID       = os.environ.get("WP_PAGE_ID", "")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID  = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
TZ               = os.environ.get("TZ", "Europe/Zurich")

# ========= Utilities =========
def iso_now_local() -> str:
    return datetime.datetime.now(tz=gettz(TZ)).strftime("%Y-%m-%d %H:%M")

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def domain(url: str) -> str:
    return urlparse(url).netloc

def get(url: str, timeout: int = 30) -> requests.Response:
    headers = {"User-Agent": "IGUV-Weekly-Updater/2.0 (+https://iguv.ch)"}
    return requests.get(url, headers=headers, timeout=timeout)

# Gängige Datums-Muster in Link-Texten/URLs (YYYY-MM-DD, DD.MM.YYYY, /2025/08/ usw.)
DATE_PATTERNS = [
    r"(?P<iso>\d{4}-\d{2}-\d{2})",
    r"(?P<dot>\d{2}\.\d{2}\.\d{4})",
    r"/(?P<ym>\d{4})/(?P<m>\d{2})/",
]

def parse_date_heuristic(text_or_url: str):
    text = text_or_url
    for pat in DATE_PATTERNS:
        m = re.search(pat, text)
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
        if "ym" in m.groupdict() and "m" in m.groupdict():
            try:
                y = int(m.group("ym")); mo = int(m.group("m"))
                # fallback: Tag = 1
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
    """Holt eine Übersichtsseite, sammelt Links, filtert grob nach Keywords & Datum."""
    print(f" - Quelle abrufen: {list_url}")
    try:
        resp = get(list_url, timeout=40)
    except Exception as e:
        print("   WARN: fetch failed:", repr(e))
        return []
    if resp.status_code != 200:
        print("   WARN: HTTP", resp.status_code, "bei", list_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    items = []
    base = list_url
    kws = [k.lower() for k in keywords]

    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        text = " ".join(a.get_text(separator=" ", strip=True).split())
        if not text:
            continue
        # nur Links der gleichen Domain priorisieren (reduziert Rauschen)
        if not same_site(href, base):
            continue
        blob = (text + " " + href).lower()
        if kws and not any(k in blob for k in kws):
            continue

        d = parse_date_heuristic(text) or parse_date_heuristic(href)
        if d and not is_within_days(d, days):
            continue

        items.append({
            "title": text[:200],
            "url": href,
            "date": d.isoformat() if d else None,
            "source": domain(href)
        })

    # Duplikate raus
    dedup = {}
    for it in items:
        dedup[(it["title"], it["url"])] = it
    items = list(dedup.values())

    print(f"   -> {len(items)} Kandidaten nach Filter")
    return items

def summarize_with_openai(sections_payload: list[dict], max_per_section: int, style: str) -> dict:
    """Nimmt bereits gescrapte Kandidaten, lässt KI eine elegante Kurzfassung bauen (JSON)."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    sys_prompt = (
        "Du bist ein präziser Nachrichten-Editor für Finanz-/Regulierungsthemen in der Schweiz. "
        "Du bekommst pro Sektion eine Liste von Kandidaten mit Titel/URL/Datum/Quelle. "
        f"Erstelle pro Sektion maximal {max_per_section} prägnante Punkte. "
        "Jeder Punkt: title, url, date_iso (falls None: heutigen Tag verwenden), summary (1–2 Sätze, warum es relevant ist). "
        "Gib das Ergebnis als JSON-Objekt mit {\"sections\":[{\"name\":\"...\",\"items\":[...]}]} zurück."
    )

    user_payload = {
        "style": style,
        "now_local": iso_now_local(),
        "sections": sections_payload
    }

    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
        ],
        temperature: 0.2
    )
    txt = completion.choices[0].message.content
    try:
        data = json.loads(txt)
    except Exception:
        print("WARN: KI-Output war kein JSON. Rohtext (Anfang):", (txt or "")[:600])
        # Fallback: leere Struktur
        data = {"sections": []}
    return data

def to_html(digest: dict, days: int) -> str:
    parts = [f'<h2>Wöchentliche Übersicht (aktualisiert: {iso_now_local()})</h2>']
    for sec in digest.get("sections", []):
        items = sec.get("items", []) or []
        if not items: continue
        parts.append(f'<h3 style="margin-top:1.2em;">{sec.get("name","")}</h3>')
        parts.append("<ul>")
        for it in items:
            date = (it.get("date_iso") or "")[:10]
            title = it.get("title","").strip()
            url = it.get("url","").strip()
            summary = it.get("summary","").strip()
            parts.append(f'<li><strong>{date}</strong> – <a href="{url}" target="_blank" rel="noopener">{title}</a>: {summary}</li>')
        parts.append("</ul>")
    if len(parts) == 1:
        parts.append("<p>Keine relevanten Neuigkeiten in den letzten Tagen.</p>")
    parts.append(f'<hr><p style="font-size:12px;color:#666;">Automatisch erstellt (letzte {days} Tage).</p>')
    return "\n".join(parts)

def fetch_wp_page():
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}?context=edit"
    r = requests.get(url, auth=(WP_USERNAME, WP_APP_PASSWORD), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"WP GET fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

def replace_container_html(full_html: str, inner_html: str) -> str:
    # Ersetzt den Inhalt innerhalb des Containers; falls Container fehlt, wird ein Block angehängt.
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
    r = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json={"content": new_html}, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP UPDATE fehlgeschlagen: {r.status_code} {r.text}")
