#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, re, json, datetime, traceback, argparse, html
from urllib.parse import urljoin, urlparse
from dateutil.tz import gettz
import yaml
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ===== ENV =====
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
MODEL            = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

WP_BASE          = os.environ.get("WP_BASE", "").rstrip("/")
WP_USERNAME      = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD  = os.environ.get("WP_APP_PASSWORD", "")

TZ               = os.environ.get("TZ", "Europe/Zurich")

HTTP_TIMEOUT_S   = int(os.environ.get("HTTP_TIMEOUT_S", "25"))
HTTP_MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", "2"))
MAX_LINKS_PER_SOURCE     = int(os.environ.get("MAX_LINKS_PER_SOURCE", "60"))
MAX_TOTAL_CANDIDATES     = int(os.environ.get("MAX_TOTAL_CANDIDATES", "200"))
MAX_ITEMS_PER_SECTION_KI = int(os.environ.get("MAX_ITEMS_PER_SECTION_KI", "5"))
OPENAI_REQUEST_TIMEOUT_S = int(os.environ.get("OPENAI_REQUEST_TIMEOUT_S", "60"))
EXPAND_DETAILS = os.environ.get("EXPAND_DETAILS", "1") in ("1","true","TRUE")

USER_AGENT = "Mozilla/5.0 (compatible; IGUV-Weekly-Updater/4.1; +https://iguv.ch)"
HDR_HTML = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8", "Cache-Control": "no-cache"}
HDR_JSON = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}

# ===== Helpers =====
def now_local():
    return datetime.datetime.now(tz=gettz(TZ))
def ch_date(d): return d.strftime("%d.%m.%Y")
def domain(u): 
    try: return urlparse(u).netloc
    except: return ""
def load_yaml(p): 
    with open(p,"r",encoding="utf-8") as f: return yaml.safe_load(f)
def require_env():
    miss=[k for k,v in {"OPENAI_API_KEY":OPENAI_API_KEY,"WP_BASE":WP_BASE,"WP_USERNAME":WP_USERNAME,"WP_APP_PASSWORD":WP_APP_PASSWORD}.items() if not v]
    if miss: raise RuntimeError("Fehlende ENV Variablen: "+", ".join(miss))
def http_get(u):
    last=None
    for _ in range(HTTP_MAX_RETRIES):
        try: return requests.get(u, headers=HDR_HTML, timeout=HTTP_TIMEOUT_S)
        except Exception as e: last=e
    raise last
DATE_PATTERNS=[r"(?P<iso>\d{4}-\d{2}-\d{2})", r"(?P<dot>\d{2}\.\d{2}\.\d{4})", r"/(?P<y>\d{4})/(?P<m>\d{2})/"]
def parse_date_heuristic(s):
    s=s or ""
    for pat in DATE_PATTERNS:
        m=re.search(pat,s); 
        if not m: continue
        if m.groupdict().get("iso"):
            try: return datetime.datetime.strptime(m.group("iso"),"%Y-%m-%d").date()
            except: pass
        if m.groupdict().get("dot"):
            try: return datetime.datetime.strptime(m.group("dot"),"%d.%m.%Y").date()
            except: pass
        if m.groupdict().get("y") and m.groupdict().get("m"):
            try: return datetime.date(int(m.group("y")), int(m.group("m")), 1)
            except: pass
    return None
def within_days(d,days): return bool(d) and (datetime.date.today()-d)<=datetime.timedelta(days=days)
def norm(s): return " ".join((s or "").split())

TEXT_BLACKLIST={"home","start","startseite","über uns","ueber uns","about","kontakt","kontaktieren","jobs","karriere","login","anmelden",
                "impressum","datenschutz","newsletter","faq","häufige fragen","haeufige fragen","downloads","publikationen","veranstaltungen",
                "events","kalender","sitemap","kontaktformular","media","medien","news"}  # Navigation

# ===== Deep snippet for better summaries =====
def extract_snippet(u,max_chars=320):
    try:
        r=http_get(u)
        if r.status_code!=200: return ""
        soup=BeautifulSoup(r.text,"lxml")
        ctx=soup.find("article") or soup
        paras=[norm(p.get_text(" ",strip=True)) for p in ctx.find_all("p")]
        paras=[p for p in paras if len(p)>60][:2]
        return " ".join(paras)[:max_chars]
    except: return ""

# ===== Crawl with per-source rules =====
def extract_from_source(list_url, rule, days, global_kws):
    print(f" - Quelle abrufen: {list_url}")
    try: resp=http_get(list_url)
    except Exception as e: print("   WARN:",repr(e)); return []
    if resp.status_code!=200: print("   WARN: HTTP",resp.status_code,"bei",list_url); return []

    soup=BeautifulSoup(resp.text,"lxml")
    include=[re.compile(p,flags=re.I) for p in rule.get("include_regex",[])]
    exclude_text=[t.lower() for t in rule.get("exclude_text",[])]
    require_date=bool(rule.get("require_date",True))
    same_site=rule.get("same_site_only",True)
    kws=[k.lower() for k in (rule.get("keywords") or global_kws or [])]

    items=[]; base=list_url
    for i,a in enumerate(soup.find_all("a",href=True)):
        if i>=MAX_LINKS_PER_SOURCE: break
        href=urljoin(base,a["href"].strip())
        text=norm(a.get_text(" ",strip=True))
        if not text or text.lower() in TEXT_BLACKLIST: continue
        if same_site and domain(href)!=domain(base): continue
        if include and not any(rx.search(href) or rx.search(text) for rx in include): continue
        if exclude_text and any(ex in text.lower() for ex in exclude_text): continue

        blob=(text+" "+href).lower()
        if kws and not any(k in blob for k in kws): continue

        d=parse_date_heuristic(text) or parse_date_heuristic(href)
        if require_date and not d: continue
        if d and not within_days(d,days): continue

        snippet=extract_snippet(href) if EXPAND_DETAILS else ""
        items.append({"title":text[:240], "url":href, "date": d.isoformat() if d else None,
                      "source":domain(href), "snippet":snippet})

    print(f"   -> {len(items)} Kandidaten nach Filter (max {MAX_LINKS_PER_SOURCE} Links)")
    return items

# ===== OpenAI (Briefing + Items) =====
def summarize_with_openai(sections_payload, max_per_section, style):
    client=OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_REQUEST_TIMEOUT_S)
    capped=[]; total=0
    for sec in sections_payload:
        cand=sec.get("candidates",[])[:60]
        capped.append({"name":sec.get("name","Sektion"),"candidates":cand})
        total+=len(cand)
        if total>=MAX_TOTAL_CANDIDATES: break

    sys_prompt=(
        "Du erstellst für Schweizer Vermögensverwalter ein Weekly mit Kurzfassung und Sektionen.\n"
        "- Nenne nur wirklich relevante Änderungen (Sanktionen, Fristen, Rundschreiben, AO-Reglemente/Gebühren/Prüfungen).\n"
        "- Vermeide generische Beschreibungen, fokussiere konkrete News.\n"
        "- Nutze 'snippet' als Kontext.\n"
        "- Kurzfassung: 4–5 Bullets, je max. 20 Wörter, mit Link.\n"
        "Gib ein JSON: {\"briefing\":[{\"title\":\"..\",\"url\":\"..\"}],\n"
        "\"sections\":[{\"name\":\"..\",\"items\":[{\"title\":\"..\",\"url\":\"..\",\"date_iso\":\"YYYY-MM-DD|\" ,\"summary\":\"..\"}]}]}\n"
    )
    user_payload={"generated_at":now_local().strftime("%Y-%m-%d %H:%M"),"style":style,"sections":capped}

    out=client.chat.completions.create(
        model=MODEL,
        response_format={"type":"json_object"},
        messages=[{"role":"system","content":sys_prompt},
                  {"role":"user","content":json.dumps(user_payload,ensure_ascii=False)}],
        temperature=0.15
    )
    txt=out.choices[0].message.content
    try: return json.loads(txt)
    except: print("WARN: KI-JSON fehlgeschlagen:",(txt or "")[:500]); return {"briefing":[],"sections":[]}

# ===== HTML (mit CSS, CH-Datum, sauberer Gliederung) =====
CSS = """
<style>
.iguv-weekly{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;line-height:1.55;font-size:16px}
.iguv-weekly h1{font-size:2.0rem;margin:.2rem 0;color:#0f2a5a;font-weight:600}
.iguv-weekly .meta{color:#666;margin-bottom:.8rem}
.iguv-weekly h2{font-size:1.25rem;margin:1.2rem 0 .4rem;color:#0f2a5a}
.iguv-weekly ul{margin:.4rem 0 1rem 1.2rem}
.iguv-weekly li{margin:.3rem 0}
.iguv-note{background:#f6f8fb;border-left:4px solid #0f2a5a;padding:.8rem;border-radius:.5rem;margin:.8rem 0 1rem}
.iguv-disclaimer{font-size:.9rem;color:#555;border-top:1px solid #e6e6e6;padding-top:.6rem;margin-top:1rem}
</style>
"""

def to_html(digest, days:int)->str:
    dt=now_local()
    parts=[CSS,'<div class="iguv-weekly">','<h1>Weekly-Updates</h1>',
           f'<div class="meta">Stand: {html.escape(dt.strftime("%d.%m.%Y %H:%M"))}</div>']

    # Kurzfassung
    briefing=digest.get("briefing") or []
    if briefing:
        parts.append('<div class="iguv-note"><strong>Kurzfassung (4–5 Punkte):</strong><ul>')
        for b in briefing[:5]:
            t=html.escape(b.get("title","").strip()); u=html.escape(b.get("url","").strip())
            parts.append(f'<li><a href="{u}" target="_blank" rel="noopener">{t}</a></li>')
        parts.append('</ul></div>')

    # Sektionen
    any_item=False
    for sec in digest.get("sections", []):
        items=sec.get("items",[]) or []
        if not items: continue
        any_item=True
        parts.append(f'<h2>{html.escape(sec.get("name",""))}</h2>')
        parts.append("<ul>")
        for it in items:
            date_iso=(it.get("date_iso") or "").strip()
            d_txt=""
            if date_iso:
                try:
                    d=datetime.datetime.strptime(date_iso[:10],"%Y-%m-%d").date()
                    d_txt=f"<strong>{ch_date(d)}</strong> – "
                except: pass
            t=html.escape(it.get("title","").strip())
            u=html.escape(it.get("url","").strip())
            s=html.escape(it.get("summary","").strip())
            parts.append(f'<li>{d_txt}<a href="{u}" target="_blank" rel="noopener">{t}</a>. {s}</li>')
        parts.append("</ul>")

    if not any_item:
        parts.append('<p>Keine relevanten Neuigkeiten in den letzten Tagen.</p>')

    parts.append(f'<div class="iguv-disclaimer">Massgebend sind die verlinkten Originalquellen. Zeitraum: letzte {days} Tage.</div>')
    parts.append("</div>")
    return "\n".join(parts)

# ===== MU-Plugin Endpoint =====
def post_to_mu_plugin(html_inner:str):
    url=f"{WP_BASE}/wp-json/iguv/v1/weekly"
    print(f"WordPress (MU-Plugin) aktualisieren: {url}")
    r=requests.post(url, auth=(WP_USERNAME,WP_APP_PASSWORD),
                    json={"html":html_inner.strip()}, timeout=HTTP_TIMEOUT_S, headers=HDR_JSON)
    if r.status_code not in (200,201):
        raise RuntimeError(f"Endpoint-Update fehlgeschlagen: {r.status_code} {r.text}")
    print("SUCCESS: Weekly HTML via Endpoint gesetzt.")

# ===== Main =====
def main():
    print("== IGUV/INPASU Weekly Updater startet ==")
    require_env()
    cfg=load_yaml("data_sources.yaml")

    parser=argparse.ArgumentParser()
    parser.add_argument("--fast",action="store_true",help="Nur FINMA/SECO/OFAC")
    args=parser.parse_args()

    sections_cfg=cfg.get("sections",[])
    if args.fast:
        core={"finma.ch","seco.admin.ch","ofac.treasury.gov"}
        fast=[]
        for sec in sections_cfg:
            rules=[]
            for src in sec.get("sources",[]):
                u=src["url"] if isinstance(src,dict) else src
                if any(d in domain(u) for d in core):
                    rules.append(src)
            if rules: fast.append({"name":sec["name"],"sources":rules,"keywords":sec.get("keywords")})
        sections_cfg=fast
        print(f"FAST-Modus aktiv: {len(sections_cfg)} Sektionen")

    days=int(cfg.get("time_window_days",7))
    global_kws=[k.lower() for k in (cfg.get("keywords") or [])]
    style=cfg.get("summary",{}).get("style","Sachlich, prägnant.")

    print("Quellen scannen …")
    sections_payload=[]; total=0
    for block in sections_cfg:
        name=block.get("name","Sektion")
        per_kws=[k.lower() for k in (block.get("keywords") or [])] or global_kws
        cand=[]
        for src in block.get("sources",[]):
            if total>=MAX_TOTAL_CANDIDATES: break
            rule=dict(url=src) if isinstance(src,str) else dict(src)
            url=rule.get("url"); 
            if not url: continue
            items=extract_from_source(url, rule, days, per_kws)
            room=MAX_TOTAL_CANDIDATES-total
            items=items[:room]; total+=len(items); cand.extend(items)
        cand.sort(key=lambda it: it.get("date") or "", reverse=True)
        sections_payload.append({"name":name,"candidates":cand})

    print(f"Gesamt-Kandidaten: {total} / Limit {MAX_TOTAL_CANDIDATES}")

    print("KI-Zusammenfassung …")
    digest=summarize_with_openai(sections_payload, MAX_ITEMS_PER_SECTION_KI, style)
    total_items=sum(len(s.get("items",[])) for s in digest.get("sections",[]))
    print(f"Relevante Meldungen nach KI: {total_items}")

    print("HTML generieren …")
    html_inner=to_html(digest, days)

    print("WordPress aktualisieren …")
    post_to_mu_plugin(html_inner)
    print("== Fertig ==")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("ERROR:",repr(e)); traceback.print_exc(); sys.exit(2)
