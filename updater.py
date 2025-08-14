import os, sys, re, json, datetime, traceback, argparse
from urllib.parse import urljoin, urlparse
from dateutil.tz import gettz
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ======= ENV / Defaults =======
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_PAGE_ID       = os.environ.get("WP_PAGE_ID", "")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID  = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
CONTAINER_STRATEGY = os.environ.get("CONTAINER_STRATEGY", "rebuild")  # rebuild | replace
TZ               = os.environ.get("TZ", "Europe/Zurich")

# Limits & Timeouts
HTTP_TIMEOUT_S             = int(os.environ.get("HTTP_TIMEOUT_S", "25"))
HTTP_MAX_RETRIES           = int(os.environ.get("HTTP_MAX_RETRIES", "2"))
MAX_LINKS_PER_SOURCE       = int(os.environ.get("MAX_LINKS_PER_SOURCE", "40"))
MAX_TOTAL_CANDIDATES       = int(os.environ.get("MAX_TOTAL_CANDIDATES", "150"))
MAX_ITEMS_PER_SECTION_KI   = int(os.environ.get("MAX_ITEMS_PER_SECTION_KI", "5"))
OPENAI_REQUEST_TIMEOUT_S   = int(os.environ.get("OPENAI_REQUEST_TIMEOUT_S", "60"))

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/2.4; +https://iguv.ch)"
DEFAULT_HEADERS_JSON = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
}
DEFAULT_HEADERS_HTML = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

# ======= Utils =======
def iso_now_local() -> str:
    return datetime.datetime.now(tz=gettz(TZ)).strftime("%Y-%m-%d %H:%M")

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def require_env():
    missing = []
    for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "WP_BASE": WP_BASE,
        "WP_PAGE_ID": WP_PAGE_ID,
        "WP_USERNAME": WP_USERNAME,
        "WP_APP_PASSWORD": WP_APP_PASSWORD,
    }.items():
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError("Fehlende ENV Variablen: " + ", ".join(missing))

def http_get_html(url: str) -> requests.Response:
    last_err = None
    for _ in range(HTTP_MAX_RETRIES):
        try:
            return requests.get(url, headers=DEFAULT_HEADERS_HTML, timeout=HTTP_TIMEOUT_S)
        except Exception as e:
            last_err = e
    raise last_err

def domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""

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

def extract_candidates(list_url: str, days: int, keywords: list[str]) -> list[dict]:
    print(f" - Quelle abrufen: {list_url}")
    try:
        resp = http_get_html(list_url)
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

    # Dedup
    dedup = {}
    for it in out:
        dedup[(it["title"], it["url"])] = it
    items = list(dedup.values())
    print(f"   -> {len(items)} Kandidaten nach Filter (max {MAX_LINKS_PER_SOURCE} Links gescannt)")
    return items

def summarize_with_openai(sections_payload: list[dict], max_per_section: int, style: str) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)

    # Payload kappen
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

# -------- WordPress I/O --------
def wp_get_page():
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}?context=edit"
    r = requests.get(url, auth=(WP_USERNAME, WP_APP_PASSWORD), timeout=HTTP_TIMEOUT_S, headers=DEFAULT_HEADERS_JSON)
    if r.status_code != 200:
        raise RuntimeError(f"WP GET fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

def rebuild_container_in_content(full_html: str, inner_html: str) -> str:
    start_marker = "<!-- IGUV_WEEKLY_START -->"
    end_marker   = "<!-- IGUV_WEEKLY_END -->"
    full_html = re.sub(re.escape(start_marker) + r".*?" + re.escape(end_marker), "", full_html, flags=re.DOTALL)
    pattern_div = re.compile(rf'<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>.*?</div>', re.IGNORECASE | re.DOTALL)
    full_html = pattern_div.sub("", full_html)
    fresh = f'<!-- IGUV_WEEKLY_START -->\n<div id="{WP_CONTAINER_ID}">{inner_html}</div>\n<!-- IGUV_WEEKLY_END -->'
    return (full_html + "\n" + fresh).strip()

def replace_container_in_content(full_html: str, inner_html: str) -> str:
    start_marker = "<!-- IGUV_WEEKLY_START -->"
    end_marker   = "<!-- IGUV_WEEKLY_END -->"
    if start_marker in full_html and end_marker in full_html:
        pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
        return pattern.sub(start_marker + "\n" + f'<div id="{WP_CONTAINER_ID}">{inner_html}</div>' + "\n" + end_marker, full_html)
    pattern_div = re.compile(rf'(<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>)(.*?)(</div>)', re.IGNORECASE | re.DOTALL)
    if pattern_div.search(full_html):
        return pattern_div.sub(rf'\1{inner_html}\3', full_html)
    return (full_html + "\n" + f'<!-- IGUV_WEEKLY_START -->\n<div id="{WP_CONTAINER_ID}">{inner_html}</div>\n<!-- IGUV_WEEKLY_END -->').strip()

def elementor_replace_html(html_text: str, inner_html: str) -> str:
    start_marker = "<!-- IGUV_WEEKLY_START -->"
    end_marker   = "<!-- IGUV_WEEKLY_END -->"
    if start_marker in html_text and end_marker in html_text:
        pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
        return pattern.sub(start_marker + "\n" + inner_html + "\n" + end_marker, html_text)
    pattern_div = re.compile(rf'(<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>)(.*?)(</div>)', re.IGNORECASE | re.DOTALL)
    if pattern_div.search(html_text):
        return pattern_div.sub(rf'\1{inner_html}\3', html_text)
    return html_text  # unverändert

def elementor_update_html_widget(elementor_data_json: str, inner_html: str, force_if_no_match: bool = True) -> tuple[str, bool, bool]:
    """
    Sucht im _elementor_data nach einem HTML-Widget.
    - Ersetzt Marker/Container innerhalb des Widgets.
    - Wenn kein Treffer und force_if_no_match=True: überschreibt das ERSTE HTML-Widget.
    Rückgabe: (updated_json, changed, forced)
    """
    try:
        data = json.loads(elementor_data_json)
    except Exception:
        return elementor_data_json, False, False

    changed = False
    forced  = False
    first_html_node = None

    def walk(nodes):
        nonlocal changed, forced, first_html_node
        for n in nodes or []:
            if n.get("elType") == "widget" and n.get("widgetType") == "html":
                if first_html_node is None:
                    first_html_node = n
                settings = n.get("settings", {})
                html_val = settings.get("html", "")
                new_val = elementor_replace_html(html_val, inner_html)
                if new_val != html_val:
                    settings["html"] = new_val
                    n["settings"] = settings
                    changed = True
            # Kinder
            for key in ("elements", "innerElements"):
                if isinstance(n.get(key), list):
                    walk(n[key])

    walk(data if isinstance(data, list) else [])

    # Falls kein Treffer: erstes HTML-Widget überschreiben
    if not changed and force_if_no_match and first_html_node is not None:
        print(" - Kein Marker/Container im Elementor-HTML gefunden – überschreibe das erste HTML-Widget (FORCED).")
        settings = first_html_node.get("settings", {}) or {}
        forced_block = f"<!-- IGUV_WEEKLY_START -->\n<div id=\"{WP_CONTAINER_ID}\">{inner_html}</div>\n<!-- IGUV_WEEKLY_END -->"
        settings["html"] = forced_block
        first_html_node["settings"] = settings
        changed = True
        forced = True

    if not changed:
        return elementor_data_json, False, False

    try:
        return json.dumps(data, ensure_ascii=False), True, forced
    except Exception:
        return elementor_data_json, False, False

def wp_update_page(payload: dict):
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}"
    r = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json=payload,
                      timeout=HTTP_TIMEOUT_S, headers=DEFAULT_HEADERS_JSON)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP UPDATE fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

# -------- Main --------
def main():
    parser = argparse.ArgumentParser(description="IGUV Weekly Updater")
    parser.add_argument("--fast", action="store_true", help="Nur Kernquellen verarbeiten (schneller, weniger Last).")
    args = parser.parse_args()

    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()
    cfg = load_yaml("data_sources.yaml")

    # FAST-Modus
    if args.fast:
        core_domains = {"finma.ch", "seco.admin.ch", "ofac.treasury.gov"}
        filtered_sections = []
        for sec in cfg.get("sections", []):
            kept = []
            for u in sec.get("sources", []):
                try:
                    if any(d in domain(u) for d in core_domains):
                        kept.append(u)
                except Exception:
                    pass
            if kept:
                filtered_sections.append({"name": sec["name"], "sources": kept})
        cfg = {**cfg, "sections": filtered_sections}
        print(f"FAST-Modus aktiv: {len(filtered_sections)} Sektionen / Kernquellen.")

    days = int(cfg.get("time_window_days", 7))
    keywords = cfg.get("keywords", [])
    sections_cfg = cfg.get("sections", [])
    style = cfg.get("summary", {}).get("style", "Kompakt, sachlich.")

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

    print("KI-Zusammenfassung …")
    digest = summarize_with_openai(sections_payload, max_per_section=MAX_ITEMS_PER_SECTION_KI, style=style)
    total_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(f"Relevante Meldungen nach KI: {total_items}")

    print("HTML generieren …")
    inner_html = to_html(digest, days=days)

    print("WordPress aktualisieren …")
    page = wp_get_page()
    content_raw = page.get("content", {}).get("raw") or page.get("content", {}).get("rendered", "") or ""
    meta = page.get("meta") or {}

    # Elementor: versuchen zu ersetzen oder forcieren
    did_elementor = False
    forced_html_widget = False
    elementor_data = meta.get("_elementor_data")
    updated_meta = None
    if isinstance(elementor_data, str) and elementor_data.strip():
        new_json, changed, forced = elementor_update_html_widget(elementor_data, inner_html, force_if_no_match=True)
        if changed:
            updated_meta = dict(meta)
            updated_meta["_elementor_data"] = new_json
            did_elementor = True
            forced_html_widget = forced
            if forced_html_widget:
                print(" - Elementor: erstes HTML-Widget überschrieben (FORCED).")
            else:
                print(" - Elementor: HTML-Widget mit Marker/Container ersetzt.")

    # Zusätzlich: klassischen content pflegen (falls Theme den Content rendert)
    if CONTAINER_STRATEGY.lower() == "rebuild":
        new_content = rebuild_container_in_content(content_raw, inner_html)
    else:
        new_content = replace_container_in_content(content_raw, inner_html)

    payload = {"content": new_content}
    if did_elementor and updated_meta is not None:
        payload["meta"] = updated_meta

    updated = wp_update_page(payload)
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
