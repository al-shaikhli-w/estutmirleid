#!/usr/bin/env python3
"""
Collects Vienna cooperative-apartment (Genossenschaftswohnung) listings and
writes them to data/listings.json for the dashboard.

Two sources are combined:

1. mygewo.at  — an aggregator that already indexes offers from all Vienna
   GBV / gemeinnützige providers. Parsed with a precise regex (reliable).

2. Each individual GBV member cooperative in Vienna (data/cooperatives.json).
   Their websites all differ, so this is a *best-effort* crawl: we follow the
   most likely "Wohnungssuche / Angebote / freie Wohnungen" links and extract
   anything that looks like an apartment (price + m² + Zimmer) with broad
   heuristics. Some sites use JS-rendered listings and won't yield results.

Run manually:  python3 main.py
Run on a schedule: see .github/workflows/scrape.yml
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "listings.json"
LEGACY_DATA_FILE = BASE_DIR / "data" / "listings.json"
COOP_FILE = BASE_DIR / "data" / "cooperatives.json"

MYGEWO_URL = "https://mygewo.at/genossenschaftswohnungen/wien"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApartmentWatch/1.0; +https://github.com/)"
}
REQUEST_TIMEOUT = 25

# ---- mygewo.at (precise) ---------------------------------------------------
# Matches: "Miete: €624 • 36,77 m² • 1 Zimmer • Kapital: -"
MYGEWO_RE = re.compile(
    r"Miete:\s*€\s*([\d.,]+)\s*•\s*([\d.,]+)\s*m²\s*•\s*(\d+)\s*Zimmer\s*•\s*Kapital:\s*(€?[\d.,]*|-)",
    re.UNICODE,
)

# ---- generic per-site crawl (heuristic) ------------------------------------
# Keywords in a link that suggest it leads to available apartments.
LISTING_LINK_KEYWORDS = (
    "wohnungssuche", "wohnungsangebot", "wohnungsangebote", "freie-wohnung",
    "freie-wohnungen", "sofort-verfuegbar", "sofort", "angebote", "angebot",
    "mietwohnung", "immobilien", "objekte", "aktuelle-wohnungen",
    "wohnen", "suche", "verfuegbar",
)
# A "listing-ish" text chunk contains a size in m² and a room count.
SIZE_RE = re.compile(r"([\d]{1,3}(?:[.,]\d{1,2})?)\s*m²", re.UNICODE)
ROOMS_RE = re.compile(r"(\d+)\s*(?:Zimmer|Zi\.?)", re.UNICODE | re.IGNORECASE)
RENT_RE = re.compile(r"(?:€|EUR)\s*([\d.]{2,}(?:,\d{2})?)", re.UNICODE)
# Vienna postal codes are 1010–1239 (1xx0 pattern).
WIEN_PLZ_RE = re.compile(r"\b1[0-2]\d0\b")
# "Kaufoption" = the tenant may later purchase the flat (Miete mit Kaufoption /
# Mietkauf). Detected either from a detail page's "Vertragstyp" field or from a
# broad keyword match in the listing text.
KAUFOPTION_RE = re.compile(
    r"kaufoption|miete\s*mit\s*kauf|mietkauf|miet-?kauf|"
    r"option\s*auf\s*(?:kauf|eigentum)|kaufm[öo]glichkeit",
    re.UNICODE | re.IGNORECASE,
)
VERTRAGSTYP_RE = re.compile(r"Vertragstyp\s+(.{0,40})", re.UNICODE | re.IGNORECASE)
# Skip non-residential offers (shops, offices, parking, storage, plots …).
NON_RESIDENTIAL_RE = re.compile(
    r"gesch[äa]ftslokal|gewerbe|gastro|lokal|b[üu]ro|ordination|geschäft|"
    r"garage|stellplatz|parkplatz|lager|magazin|grundst[üu]ck|"
    r"betriebsobjekt|praxis|atelier|verkaufsfl[äa]che",
    re.UNICODE | re.IGNORECASE,
)
MAX_PAGES_PER_SITE = 4


def num(raw):
    """German number string -> normalized decimal string, or None."""
    if not raw:
        return None
    raw = raw.strip().replace("€", "").replace("EUR", "").strip()
    if raw in ("", "-"):
        return None
    return raw.replace(".", "").replace(",", ".")


def fetch(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.SSLError:
        # Some cooperative sites have broken/mismatched certs; retry without
        # verification (best-effort on public data).
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
    resp.raise_for_status()
    return resp.text


def make_id(source, url, extra=""):
    slug = re.sub(r"[^a-z0-9]+", "-", (url + extra).lower()).strip("-")
    return f"{source}-{slug}"[:180]


def detect_kaufoption(detail_url):
    """Fetch a mygewo detail page and read its "Vertragstyp" field.

    Returns True (Miete mit Kaufoption), False (plain Miete), or None if the
    page can't be read / has no Vertragstyp.
    """
    try:
        html = fetch(detail_url)
    except requests.RequestException:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    m = VERTRAGSTYP_RE.search(text)
    if m:
        return bool(KAUFOPTION_RE.search(m.group(1)))
    # No explicit Vertragstyp field: fall back to a page-wide keyword scan.
    if KAUFOPTION_RE.search(text):
        return True
    return None


# --------------------------------------------------------------------------- #
# Source 1: mygewo.at aggregator
# --------------------------------------------------------------------------- #
def scrape_mygewo():
    listings = []
    try:
        html = fetch(MYGEWO_URL)
    except requests.RequestException as e:
        print(f"  ! mygewo.at failed: {e}", file=sys.stderr)
        return listings

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href*="/genossenschaftswohnungen/angebot/"]'):
        text = " ".join(a.get_text(" ", strip=True).split())
        m = MYGEWO_RE.search(text)
        if not m:
            continue
        miete, groesse, zimmer, kapital = m.groups()
        tail = text[m.end():].strip(" •")
        src = re.search(r"gefunden auf ([\w\.\-]+)", text)
        href = a.get("href", "")
        full_url = href if href.startswith("http") else f"https://mygewo.at{href}"
        listings.append({
            "id": make_id("mygewo", href),
            "rent_eur": num(miete),
            "size_sqm": num(groesse),
            "rooms": int(zimmer),
            "deposit_eur": num(kapital),
            "address": tail or None,
            "kaufoption": None,  # filled in below from the detail page
            "provider": None,
            "source_site": src.group(1) if src else "mygewo.at",
            "detail_url": full_url,
        })
    # Enrich each offer with its Vertragstyp (Kaufoption ja/nein). One extra
    # request per listing — the aggregator only holds a few dozen at a time.
    for l in listings:
        l["kaufoption"] = detect_kaufoption(l["detail_url"])
        time.sleep(0.3)  # be polite
    print(f"  mygewo.at: {len(listings)} listings")
    return listings


# --------------------------------------------------------------------------- #
# Source 2: per-cooperative best-effort crawl
# --------------------------------------------------------------------------- #
def find_listing_pages(base_url, html):
    """Return candidate URLs on this site that likely hold apartment offers."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text(" ", strip=True) + " " + href).lower()
        if any(k in label for k in LISTING_LINK_KEYWORDS):
            full = urljoin(base_url, href)
            if urlparse(full).netloc == base_host and full not in found:
                found[full] = True
        if len(found) >= MAX_PAGES_PER_SITE:
            break
    return list(found.keys())


def extract_listings_from_page(coop, url, html):
    """Broad heuristic extraction: any element mentioning m² + Zimmer."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    results = []
    seen = set()
    # Look at reasonably small blocks so we don't merge unrelated listings.
    for el in soup.find_all(["li", "article", "tr", "div"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not (30 <= len(text) <= 300):
            continue
        size = SIZE_RE.search(text)
        rooms = ROOMS_RE.search(text)
        if not (size and rooms):
            continue
        # Keep Vienna offers only (best-fit for this dashboard).
        if not WIEN_PLZ_RE.search(text):
            continue
        # Drop commercial / non-residential offers.
        if NON_RESIDENTIAL_RE.search(text):
            continue
        # Collapse near-duplicates from nested elements: key on the core facts.
        key = f"{size.group(1)}|{rooms.group(1)}|{WIEN_PLZ_RE.search(text).group(0)}"
        if key in seen:
            continue
        seen.add(key)
        rent = RENT_RE.search(text)
        results.append({
            "id": make_id("coop", url, key),
            "rent_eur": num(rent.group(1)) if rent else None,
            "size_sqm": num(size.group(1)),
            "rooms": int(rooms.group(1)),
            "deposit_eur": None,
            # Best-effort: True if the offer text mentions a purchase option,
            # otherwise unknown (None) — we can't be sure from a snippet.
            "kaufoption": True if KAUFOPTION_RE.search(text) else None,
            "address": text[:200],
            "provider": coop["name"],
            "source_site": urlparse(coop["website"]).netloc,
            "detail_url": url,
        })
    return results


def scrape_cooperative(coop):
    site = coop.get("website", "").strip()
    if not site:
        return []
    listings = []
    try:
        home = fetch(site)
    except requests.RequestException as e:
        print(f"  ! {coop['name'][:40]}: {e}", file=sys.stderr)
        return []

    pages = [site] + find_listing_pages(site, home)
    # de-dup, keep home first
    pages = list(dict.fromkeys(pages))[: MAX_PAGES_PER_SITE + 1]

    for page in pages:
        try:
            html = home if page == site else fetch(page)
        except requests.RequestException:
            continue
        listings.extend(extract_listings_from_page(coop, page, html))
        time.sleep(0.5)  # be polite

    if listings:
        print(f"  {coop['name'][:45]}: {len(listings)} candidate(s)")
    return listings


def dedupe(listings):
    out, seen = [], set()
    for l in listings:
        if l["id"] in seen:
            continue
        seen.add(l["id"])
        out.append(l)
    return out


def load_existing():
    # Prefer the current root output file, but keep compatibility with older
    # runs that wrote to data/listings.json.
    for candidate in (DATA_FILE, LEGACY_DATA_FILE):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return {"listings": [], "history": []}


def main():
    cooperatives = json.loads(COOP_FILE.read_text(encoding="utf-8"))

    print("Scraping mygewo.at aggregator…")
    all_listings = scrape_mygewo()

    print(f"Crawling {len(cooperatives)} cooperative websites…")
    for coop in cooperatives:
        all_listings.extend(scrape_cooperative(coop))

    all_listings = dedupe(all_listings)

    existing = load_existing()
    existing_ids = {l["id"] for l in existing.get("listings", [])}
    new_ids = {l["id"] for l in all_listings}
    added = new_ids - existing_ids
    removed = existing_ids - new_ids

    now = datetime.now(timezone.utc).isoformat()
    result = {
        "last_updated": now,
        "listings": all_listings,
        "cooperatives": cooperatives,
        "history": (existing.get("history", []) + [{
            "timestamp": now,
            "total": len(all_listings),
            "added": len(added),
            "removed": len(removed),
            # IDs that appeared this run — lets the dashboard flag "NEU" offers.
            "new_ids": sorted(added),
        }])[-200:],
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone: {len(all_listings)} listings "
          f"({len(added)} new, {len(removed)} gone) -> {DATA_FILE}")


if __name__ == "__main__":
    main()
