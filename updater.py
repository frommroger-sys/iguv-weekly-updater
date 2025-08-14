import os, sys, json, re, datetime, traceback
from dateutil.relativedelta import relativedelta
from dateutil.tz import gettz
import yaml
import requests
from openai import OpenAI

# ========= ENV Variablen =========
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4o-mini-search-preview")
WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_PAGE_ID       = os.environ.get("WP_PAGE_ID", "")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID  = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
TZ               = os.environ.get("TZ", "Europe/Zurich")

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

def render_html(content_html: str, days: int) -> str:
    return (
        f'<div id="{WP_CONTAINER_ID}">'
        f'<h2>Wöchentliche Übersicht (aktualisiert: {iso_now_local()})</h2>'
        f'{content_html}'
        f'<hr><p style="font-size:12px;color:#666;">Automatisch erstellt (letzte {days} Tage).</p>'
        f'</div>'
    )

def to_html_list(digest: dict) -> str:
    out = []
    for sec in digest.get("sections", []):
        if not sec.get("items"):
            continue
        out.append(f'<h3 style="margin-top:1.2em;">{sec["name"]}</h3>')
        out.append("<ul>")
        for it in sec["items"]:
            date = (it.get("date_iso") or "")[:10]
            title = (it.get("title") or "").strip()
            url = (it.get("url") or "").strip()
            summary = (it.get("summary") or "").strip()
            out.append(f'<li><strong>{date}</strong> – <a href="{url}" target="_blank" rel="noopener">{title}</a>: {summary}</li>')
        out.append("</ul>")
    if not out:
        return "<p>Keine relevanten Neuigkeiten in den letzten Tagen.</p>"
    return "\n".join(out)

def fetch_wp_page():
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}?context=edit"
    r = requests.get(url, auth=(WP_USERNAME, WP_APP_PASSWORD), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"WP GET fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

def replace_container_html(full_html: str, new_container_html: str) -> str:
    pattern = re.compile(
        rf'(<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>)(.*?)(</div>)',
        re.DOTALL | re.IGNORECASE
    )
    if pattern.search(full_html):
        return pattern.sub(rf'\1{new_container_html}\3', full_html)
    else:
        return full_html + "\n" + new_container_html

def update_wp_page(new_container_html: str) -> dict:
    page = fetch_wp_page()
    current_html = page.get("content", {}).get("raw") or page.get("content", {}).get("rendered", "")
    updated_html = replace_container_html(current_html, new_container_html)
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}"
    payload = {"content": updated_html}
    r = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP UPDATE fehlgeschlagen: {r.status_code} {r.text}")
    return r.json()

# ========= KI-Funktion mit Websearch =========
def build_json_schema():
    return {
        "name": "WeeklyDigest",
        "schema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "url": {"type": "string"},
                                        "date_iso": {"type": "string"},
                                        "summary": {"type": "string"}
                                    },
                                    "required": ["title", "url", "date_iso", "summary"]
                                }
                            }
                        },
                        "required": ["name", "items"]
                    }
                }
            },
            "required": ["sections"],
            "additionalProperties": False
        }
    }

def query_openai_digest(cfg: dict) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)
    days = cfg.get("time_window_days", 7)
    keywords = ", ".join(cfg.get("keywords", []))
    instruction = (
        f"Erstelle eine Übersicht der letzten {days} Tage aus den angegebenen Quellen. "
        f"Nur Meldungen mit mindestens einem dieser Schlüsselwörter: {keywords}. "
        f"Maximal {cfg.get('summary', {}).get('max_bullets_per_section', 5)} Punkte pro Sektion. "
        "Jeder Punkt: Titel, URL, ISO-Datum, 1–2 Sätze Kurzfazit."
    )

    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        response_format={"type": "json_schema", "json_schema": build_json_schema()},
        input=[{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "input_text", "text": json.dumps({
                    "sections": cfg.get("sections", [])
                }, ensure_ascii=False)}
            ]
        }]
    )

    return json.loads(response.output_text)

# ========= Main =========
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()

    cfg = load_yaml("data_sources.yaml")
    print("Konfiguration geladen:", len(cfg.get("sections", [])), "Sektionen")

    digest = query_openai_digest(cfg)
    total_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(f"Relevante Meldungen: {total_items}")

    html_content = to_html_list(digest)
    full_html = render_html(html_content, cfg.get("time_window_days", 7))

    updated = update_wp_page(full_html)
    print("SUCCESS: WP aktualisiert –", updated.get("link"))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(1)
