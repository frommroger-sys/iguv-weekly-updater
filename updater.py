import os, sys, json, re, datetime, traceback
from dateutil.tz import gettz
from dateutil.relativedelta import relativedelta
from dateutil.parser import isoparse
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ====== ENV (per GitHub Secrets gesetzt) ======
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4.1")  # Empfehlung
WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_PAGE_ID       = os.environ.get("WP_PAGE_ID", "")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID  = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
TZ               = os.environ.get("TZ", "Europe/Zurich")

def log_env_sanity():
    print("ENV-Sanity:")
    print(" - OPENAI_API_KEY set?:", bool(OPENAI_API_KEY))
    print(" - MODEL:", MODEL)
    print(" - WP_BASE:", bool(WP_BASE))
    print(" - WP_PAGE_ID:", bool(WP_PAGE_ID))
    print(" - WP_USERNAME:", bool(WP_USERNAME))
    print(" - WP_APP_PASSWORD set?:", bool(WP_APP_PASSWORD))
    print(" - WP_CONTAINER_ID:", WP_CONTAINER_ID)

def require_env():
    missing = []
    if not OPENAI_API_KEY: missing.append("OPENAI_API_KEY")
    if not WP_BASE: missing.append("WP_BASE")
    if not WP_PAGE_ID: missing.append("WP_PAGE_ID")
    if not WP_USERNAME: missing.append("WP_USERNAME")
    if not WP_APP_PASSWORD: missing.append("WP_APP_PASSWORD")
    if missing:
        raise RuntimeError("Fehlende ENV Variablen: " + ", ".join(missing))

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def iso_now_local() -> str:
    tz = gettz(TZ)
    return datetime.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")

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
                                        "summary": {"type": "string"},
                                        "tags": {"type": "array", "items": {"type": "string"}}
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

def render_html(template_path: str, content_html: str, days: int) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    return (tpl
            .replace("{{WP_CONTAINER_ID}}", WP_CONTAINER_ID)
            .replace("{{TIMESTAMP}}", iso_now_local())
            .replace("{{CONTENT}}", content_html)
            .replace("{{DAYS}}", str(days)))

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
    resp = requests.get(url, auth=(WP_USERNAME, WP_APP_PASSWORD), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"WP GET fehlgeschlagen: {resp.status_code} {resp.text}")
    return resp.json()

def replace_container_html(full_html: str, new_container_html: str) -> str:
    pattern = re.compile(rf'(<div[^>]+id=["\']{re.escape(WP_CONTAINER_ID)}["\'][^>]*>)(.*?)(</div>)',
                         re.DOTALL | re.IGNORECASE)
    if pattern.search(full_html):
        return pattern.sub(rf'\1{new_container_html}\3', full_html)
    else:
        # Container fehlt -> vollständigen Block anhängen
        block = f'<div id="{WP_CONTAINER_ID}">{new_container_html}</div>'
        return full_html + "\n" + block

def update_wp_page(new_container_inner_html: str) -> dict:
    page = fetch_wp_page()
    current_html = page.get("content", {}).get("raw") or page.get("content", {}).get("rendered", "")
    updated_html = replace_container_html(current_html, new_container_inner_html)
    url = f"{WP_BASE}/wp-json/wp/v2/pages/{WP_PAGE_ID}"
    payload = {"content": updated_html}
    resp = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json=payload, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"WP UPDATE fehlgeschlagen: {resp.status_code} {resp.text}")
    return resp.json()

def build_user_instruction(cfg: dict) -> str:
    days = cfg.get("time_window_days", 7)
    kw = ", ".join(cfg.get("keywords", [])) or "(keine)"
    return "\n".join([
        f"Erstelle eine wöchentliche Übersicht der letzten {days} Tage.",
        f"Maximal {cfg.get('summary', {}).get('max_bullets_per_section', 5)} Punkte pro Sektion.",
        "Filterregeln:",
        f"- Nur Meldungen innerhalb der letzten {days} Tage.",
        f"- Schlüsselwörter (mindestens eines): {kw}.",
        "- Keine Duplikate.",
        '- Jede Meldung: Titel, ISO-Datum, URL, 1–2 Sätze Kurzfazit ("warum relevant").',
        f'Stil: {cfg.get("summary", {}).get("style", "Kompakt und sachlich.")}',
    ])

def query_openai_digest(cfg: dict) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)
    days = cfg.get("time_window_days", 7)
    cutoff_iso = (datetime.datetime.now(datetime.timezone.utc) - relativedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    instruction = build_user_instruction(cfg)
    json_schema = build_json_schema()

    print(" - KI-Abfrage (Responses API) ...")
    response = client.responses.create(
        model=MODEL,
        reasoning={"effort": "medium"},
        tools=[{"type": "web_search"}],
        response_format={"type": "json_schema", "json_schema": json_schema},
        input=[{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "input_text", "text": json.dumps({
                    "cutoff_iso_utc": cutoff_iso,
                    "sections": cfg.get("sections", []),
                    "keywords": cfg.get("keywords", [])
                }, ensure_ascii=False)}
            ]
        }]
    )

    raw = response.output_text
    if not raw:
        raise RuntimeError("Leere Antwort der KI (output_text ist leer).")
    try:
        data = json.loads(raw)
    except Exception:
        print("Rohantwort (erster Teil):", raw[:600], "...")
        raise

    # Failsafe-Filter nach Datum & Keywords
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    cutoff = isoparse(cutoff_iso)
    filtered_sections = []
    for sec in data.get("sections", []):
        items = []
        for it in sec.get("items", []):
            try:
                d = isoparse(it.get("date_iso"))
            except Exception:
                continue
            if d < cutoff:
                continue
            if keywords:
                blob = " ".join([it.get("title",""), it.get("summary",""), " ".join(it.get("tags",[]))]).lower()
                if not any(k in blob for k in keywords):
                    continue
            items.append(it)
        if items:
            filtered_sections.append({"name": sec["name"], "items": items})

    return {"sections": filtered_sections}

def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    log_env_sanity()
    require_env()

    print("Konfiguration laden ...")
    cfg = load_yaml("data_sources.yaml")
    print(" - Quellenblöcke:", len(cfg.get("sections", [])))

    print("KI & Websuche ausführen ...")
    digest = query_openai_digest(cfg)
    total = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    print(" - Relevante Meldungen:", total)

    print("HTML rendern ...")
    content_html = to_html_list(digest)
    full_html = render_html("html_template.html", content_html, cfg.get("time_window_days", 7))

    print("WordPress aktualisieren ...")
    updated = update_wp_page(full_html)
    mod = updated.get("modified") or updated.get("modified_gmt")
    link = updated.get("link") or updated.get("guid", {}).get("rendered")
    print(f"SUCCESS: WP aktualisiert. modified={mod} link={link}")

    print("== Fertig ==")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        sys.exit(2)
