"""
Pulls every Bellhaven community off the public website.

Two ideas drive this file.

1. DISCOVERY IS A UNION, NOT A PAGE COUNT.
   The directory says "34 communities listed" across 3 pages. The homepage
   links a 35th (Findlay) that the directory never shows. So we collect links
   from the directory, the homepage and the About page, then de-duplicate.
   We never hardcode "3 pages" or "35 communities".

2. PARSE BY LABEL, NOT BY POSITION.
   A detail page renders as pairs: the word "Address" and then the address,
   the words "Care Offerings" and then the offerings. We find the element whose
   text is exactly "Address" and read the value sitting next to it. If the site
   adds a "Room Count" row tomorrow, nothing shifts, because we never say
   "the third box on the page".
"""
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import (
    POLITE_DELAY_SECONDS,
    RAW_DIR,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT,
    SITE_BASE,
)
from bellhaven.normalize import location_key, parse_city_state_zip

USER_AGENT = "bellhaven-sync/1.0 (Clipboard Health analyst assessment)"

# The vocabulary the site actually uses. Used as a fallback when several
# offering tags get flattened into one string ("Assisted LivingMemory Support").
KNOWN_OFFERINGS = [
    "Short-Term Rehabilitation & Nursing",
    "Short Term Rehabilitation & Nursing",
    "Independent Living",
    "Assisted Living",
    "Memory Support",
    "Memory Care",
    "Skilled Nursing",
]


def fetch(url, save_as=None):
    """GET a page, with retries, a polite delay and a copy saved to disk."""
    last_error = None
    for attempt in range(REQUEST_RETRIES):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            if save_as:
                (RAW_DIR / save_as).write_text(response.text, encoding="utf-8")
            time.sleep(POLITE_DELAY_SECONDS)
            return response.text
        except requests.RequestException as exc:      # noqa: PERF203
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _label_element(soup, label):
    """
    Find the smallest element whose visible text is exactly `label`.

    "Smallest" matters: on most pages the <body> also technically contains the
    word "Address", but the <div class="label">Address</div> contains nothing
    else. Fewest descendants wins.
    """
    target = label.strip().lower().rstrip(":")
    hits = []
    for tag in soup.find_all(["div", "span", "dt", "th", "h2", "h3", "h4", "p", "strong", "b", "label"]):
        text = tag.get_text(" ", strip=True).lower().rstrip(":")
        if text == target:
            hits.append(tag)
    if not hits:
        return None
    return min(hits, key=lambda t: len(list(t.descendants)))


def value_element_for(soup, label):
    """Return the element holding the VALUE that belongs to `label`."""
    label_tag = _label_element(soup, label)
    if label_tag is None:
        return None

    sibling = label_tag.find_next_sibling()
    if sibling is not None and sibling.get_text(strip=True):
        return sibling

    # No sibling: the label and value share a parent wrapper. Take the parent,
    # make a copy, delete the label from the copy, and what remains is the value.
    parent = label_tag.parent
    if parent is None:
        return None
    clone = _soup(str(parent))
    for tag in clone.find_all(True):
        if tag.get_text(" ", strip=True).lower().rstrip(":") == label.strip().lower():
            tag.decompose()
            break
    return clone


def value_text_for(soup, label):
    """The value for `label` as text, with line breaks preserved."""
    element = value_element_for(soup, label)
    if element is None:
        return ""
    return element.get_text("\n", strip=True)


def split_offerings(soup):
    """
    Care offerings, as a clean list.

    Each offering is its own tag on the page, which is why the flattened text
    reads "Assisted LivingMemory Support" with no separator. So:
      1. read the child tags one by one (the correct way), and
      2. if that yields nothing, fall back to scanning the flattened string for
         offerings we already know about.
    """
    element = value_element_for(soup, "Care Offerings")
    if element is None:
        return []

    children = [
        child.get_text(" ", strip=True)
        for child in element.find_all(True, recursive=True)
        if child.get_text(" ", strip=True) and not child.find(True)
    ]
    cleaned = [c for c in dict.fromkeys(children) if c]
    if cleaned:
        return cleaned

    flat = element.get_text(" ", strip=True)
    found, remainder = [], flat
    for offering in KNOWN_OFFERINGS:
        if offering.lower() in remainder.lower():
            found.append(offering)
            remainder = re.sub(re.escape(offering), " ", remainder, flags=re.I)
    if remainder.strip():
        found.append(remainder.strip())       # surfaced, never silently dropped
    return found


def discover_slugs():
    """Every /communities/<slug> link the site exposes, from every page."""
    slugs, pages_seen = {}, set()

    # 1. the paginated directory, following "Next" until it disappears
    url, guard = f"{SITE_BASE}/communities", 0
    while url and url not in pages_seen and guard < 25:
        pages_seen.add(url)
        guard += 1
        html = fetch(url, save_as=f"directory_{guard}.html")
        soup = _soup(html)
        for anchor in soup.select('a[href*="/communities/"]'):
            href = anchor.get("href", "")
            slug = href.rstrip("/").split("/communities/")[-1]
            if slug and "/" not in slug:
                slugs.setdefault(slug, "directory")
        nxt = None
        for anchor in soup.find_all("a"):
            if "next" in anchor.get_text(" ", strip=True).lower():
                nxt = urljoin(SITE_BASE, anchor.get("href", ""))
        url = nxt

    # 2. any other page that links to a community
    for extra, name in ((f"{SITE_BASE}/", "home"), (f"{SITE_BASE}/about", "about")):
        soup = _soup(fetch(extra, save_as=f"{name}.html"))
        for anchor in soup.select('a[href*="/communities/"]'):
            slug = anchor.get("href", "").rstrip("/").split("/communities/")[-1]
            if slug and "/" not in slug:
                slugs.setdefault(slug, name)

    return slugs


def stated_total(html):
    """The count the site claims, so we can warn when our crawl disagrees."""
    m = re.search(r"(\d+)\s+communities", html, flags=re.I)
    return int(m.group(1)) if m else None


def scrape_location(slug):
    """One community detail page -> one dict."""
    url = f"{SITE_BASE}/communities/{slug}"
    html = fetch(url, save_as=f"community_{slug}.html")
    soup = _soup(html)

    heading = soup.find("h1")
    name = heading.get_text(" ", strip=True) if heading else ""

    address_block = value_text_for(soup, "Address")
    lines = [ln for ln in address_block.split("\n") if ln.strip()]
    street = lines[0] if lines else ""
    city, state, zip_code = parse_city_state_zip(lines[1]) if len(lines) > 1 else ("", "", "")

    record = {
        "slug": slug,
        "url": url,
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "offerings": split_offerings(soup),
        "administrator": value_text_for(soup, "Administrator"),
        "phone": value_text_for(soup, "Phone"),
    }
    record["location_key"] = location_key(street, zip_code)
    record["problems"] = validate(record)
    return record


def validate(record):
    """Never drop a bad record. Flag it, and let a human see it."""
    problems = []
    if not record["name"]:
        problems.append("no name found")
    if not record["street"]:
        problems.append("no street found")
    if not re.fullmatch(r"\d{5}", record["zip"] or ""):
        problems.append(f"zip looks wrong: {record['zip']!r}")
    if not record["offerings"]:
        problems.append("no care offerings found")
    return problems


def scrape_all(verbose=True):
    slugs = discover_slugs()
    records = []
    for i, (slug, source) in enumerate(sorted(slugs.items()), start=1):
        record = scrape_location(slug)
        record["discovered_via"] = source
        records.append(record)
        if verbose:
            flag = "  <-- " + "; ".join(record["problems"]) if record["problems"] else ""
            print(f"  [{i:>2}/{len(slugs)}] {record['name']}{flag}")
    return records


if __name__ == "__main__":
    # Handy during the demo: `python -m bellhaven.scraper` prints what it found.
    import json

    found = scrape_all()
    print(f"\n{len(found)} communities")
    print(json.dumps(found[:2], indent=2))
