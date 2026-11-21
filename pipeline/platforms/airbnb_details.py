"""Airbnb listing pages: bedrooms, baths, guests, description, license numbers, and whether the page still exists.

Search results no longer include bedroom/bath counts, and hosts type license numbers into the description
("Crested Butte South POA STR | #24-83B"), so both come from the listing's own page. A 404 means the
listing was removed. A page that still loads means the listing still exists, even if it dropped out of
search (fully booked, or the scan was throttled).
"""
import html
import json
import re
import time
from datetime import date

import requests

from pipeline.platforms.airbnb import STATE_RE, UA

PAGE = "https://www.airbnb.com/rooms/{}"
COUNT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(guests?|bedrooms?|beds?|baths?|private baths?|shared baths?|half-baths?)\s*$", re.I)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
STUDIO_RE = re.compile(r"^\s*studio\s*$", re.I)
# A license-like keyword, then a token that contains at least one digit: "STR License #24-83B", "Permit: STR21-00163".
LICENSE_RE = re.compile(
    r"(?P<label>(?:str\s+)?(?:licen[sc]e|permit|registration|reg\.?|lic\.?)(?:\s*(?:number|no\.?|num|#))?|str)"
    r"\s*(?:is|:|#|-|\|)?\s*#?\s*(?P<num>(?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]{2,24})",
    re.I,
)
NOT_LICENSE = re.compile(r"^(?:\d{1,2}(?:am|pm)?|\d{3}-\d{3}-\d{4})$", re.I)  # times, phone numbers


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def license_mentions(text):
    """License-like numbers in listing text, with a little context for staff to read."""
    out, seen = [], set()
    for m in LICENSE_RE.finditer(text or ""):
        num = m.group("num").strip("-/").upper()
        if NOT_LICENSE.match(num) or num in seen:
            continue
        seen.add(num)
        a, b = max(0, m.start() - 40), min(len(text), m.end() + 20)
        out.append({"number": num, "context": " ".join(text[a:b].split())})
    return out[:5]


def parse(page_html):
    """Return details from a listing page, or None if the page has no listing data (format changed)."""
    m = STATE_RE.search(page_html)
    if not m:
        return None
    state = json.loads(m.group(1))
    counts, description, title, coord, ptype = {}, "", None, None, None
    for k, v in _walk(state):
        if k == "overview" and isinstance(v, dict) and isinstance(v.get("items"), list):
            for item in v["items"]:
                item = item if isinstance(item, str) else (item or {}).get("title", "")
                c = COUNT_RE.match(item or "")
                if c:
                    word = c.group(2).lower()
                    key = "guests" if word.startswith("guest") else "bedrooms" if word.startswith("bedroom") else \
                        "beds" if word.startswith("bed") else "baths"
                    counts.setdefault(key, float(c.group(1)))
                elif STUDIO_RE.match(item or ""):
                    counts.setdefault("bedrooms", 0.0)
        elif k == "longDescriptionHtml" and isinstance(v, dict) and not description:
            description = _text(v.get("localizedStringWithTranslationPreference") or v.get("localizedString") or v.get("source"))
        elif k == "coordinate" and isinstance(v, dict) and coord is None and v.get("latitude") is not None:
            coord = (v["latitude"], v["longitude"])
        elif k == "propertyType" and isinstance(v, str) and ptype is None and " " in v:
            ptype = v  # "Entire townhouse"
    t = TITLE_RE.search(page_html)
    if t:
        title = re.sub(r"\s+-\s+Airbnb\s*$", "", html.unescape(t.group(1)).strip())
        title = re.sub(r"\s+-\s+[^-]+ for Rent in .*$", "", title)  # "<name> - Townhouses for Rent in Crested Butte, ..."
    if not counts and not description:
        return None
    return {
        "guests": counts.get("guests"),
        "bedrooms": counts.get("bedrooms"),
        "beds": counts.get("beds"),
        "baths": counts.get("baths"),
        "page_title": title,
        "property_type": ptype,
        "page_lat": coord[0] if coord else None,
        "page_lng": coord[1] if coord else None,
        "license_mentions": license_mentions(description),
        "description_text": description[:4000],
    }


def fetch(session, listing_id):
    """{"page": "ok" | "removed" | "error", ...details}."""
    for attempt in range(3):
        try:
            r = session.get(PAGE.format(listing_id), timeout=40, allow_redirects=True)
            if r.status_code in (404, 410) or "404 Page Not Found" in r.text[:2000]:
                return {"page": "removed"}
            if r.status_code == 200:
                d = parse(r.text)
                if d:
                    return {"page": "ok", **d}
        except Exception:
            pass
        time.sleep(6 * (attempt + 1))
    return {"page": "error"}


def fetch_many(ids, log=print, delay=1.5, max_errors_in_a_row=15):
    """Fetch listing pages one at a time. Stops early if Airbnb starts blocking (many errors in a row)."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US"})
    out, errors_in_a_row, today = {}, 0, date.today().isoformat()
    for i, lid in enumerate(ids, 1):
        d = fetch(session, lid)
        d["fetched"] = today
        out[lid] = d
        errors_in_a_row = errors_in_a_row + 1 if d["page"] == "error" else 0
        if errors_in_a_row >= max_errors_in_a_row:
            log(f"airbnb details: {max_errors_in_a_row} errors in a row - stopping (blocked, or page format changed)")
            break
        if i % 50 == 0:
            log(f"airbnb details: {i}/{len(ids)} listing pages")
        time.sleep(delay)
    return out
