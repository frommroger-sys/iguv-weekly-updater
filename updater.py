import os
# updater.py – IGUV Weekly Website Updater (korrigierte Version)
import sys, subprocess, re, json
from datetime import datetime, timedelta

# ======= KONFIG =======
WP_BASE         = os.environ.get("WP_BASE", "https://iguv.ch").rstrip("/")
WP_PAGE_ID      = int(os.environ.get("WP_PAGE_ID", "50489"))
WP_USERNAME     = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
WP_CONTAINER_ID = os.environ.get("WP_CONTAINER_ID", "weekly-update-content")
USER_AGENT      = os.environ.get("USER_AGENT", "IGUV-Weekly-Updater/2.0 (+https://iguv.ch)")

# ======= DEPS =======
try:
    import requests
except Exception:
    raise RuntimeError("Das Modul 'requests' muss in dieser Umgebung verfügbar sein.")

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "beautifulsoup4"])
        from bs4 import BeautifulSoup
        HAVE_BS4 = True
    except Exception:
        HAVE_BS4 = False

HEADERS = {"User-Agent": USER_AGENT}
NOW = datetime.now()
SINCE = NOW - timedelta(days=7)

# ======= HELPERS =======
def http_get(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def make_soup(url, timeout=25):
    if not HAVE_BS4:
        return None
    try:
        return BeautifulSoup(http_get(url, timeout).text, "html.parser")
    except Exception:
        return None

def parse_date_guess(text):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return datetime(y, mth, d)
    except:
        return None

def within_last_week(dt):
    return dt and dt >= SINCE

def item(title, url, text=None):
    return {"title": title.strip(), "url": url.strip(), "text": (text or "").strip()}

def dedupe(lst):
    seen, out = set(), []
    for i in lst:
        k = (i["title"], i["url"])
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out

# ======= SOURCES =======
def fetch_finma():
    base = "https://www.finma.ch"
    url = base + "/de/news/"
    sp = make_soup(url)
    out = []
    if sp:
        nodes = sp.select("article, .news__item, .m-news-list__item, li a[href]") or []
        for n in nodes:
            a = n if getattr(n, "name", "") == "a" else n.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href", "")
            if not title or not href:
                continue
            link = href if href.startswith("http") else base + href
            dt = parse_date_guess(n.get_text(" ", strip=True))
            if dt and within_last_week(dt) and any(k in title.lower() for k in ["rundschreiben", "konsult", "sanktion", "verordnung", "publikation"]):
                out.append(item(title, link))
    return dedupe(out)

def fetch_ao_generic(base_url, keywords):
    sp = make_soup(base_url)
    out = []
    if not sp:
        return out
    for a in sp.select("a[href]"):
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if not title or not href:
            continue
        link = href if href.startswith("http") else (base_url.rstrip("/") + "/" + href.lstrip("/"))
        dt = parse_date_guess(a.get_text(" ", strip=True)) or parse_date_guess(link)
        if dt and within_last_week(dt) and any(k in title.lower() for k in keywords):
            out.append(item(title, link))
    return dedupe(out)

def fetch_aoos():
    return fetch_ao_generic("https://www.aoos.ch", ["gebühr", "gebuehr", "tarif", "reglement", "prüf", "pruef", "faq"])

def fetch_osfin():
    return fetch_ao_generic("https://www.osfin.ch", ["gebühr", "gebuehr", "tarif", "reglement", "prüf", "pruef", "faq"])

def fetch_oadfct():
    return fetch_ao_generic("https://www.oad-fct.ch", ["gebühr", "gebuehr", "tarif", "reglement", "prüf", "pruef", "faq"])

def fetch_osif():
    return fetch_ao_generic("https://www.osif.ch", ["gebühr", "gebuehr", "tarif", "reglement", "prüf", "pruef", "faq"])

def fetch_so_fit():
    return fetch_ao_generic("https://www.so-fit.ch", ["gebühr", "gebuehr", "tarif", "reglement", "prüf", "pruef", "faq"])

# (… Rest deines Codes mit SECO, OFAC, EU, Events, State Handling, build_fragment, replace_container etc. bleibt unverändert …)

# ======= MAIN =======
if __name__ == "__main__":
    # Hier direkt der bisherige Code
    print("Updater startet...")
    # fetch data, update WordPress, etc.
