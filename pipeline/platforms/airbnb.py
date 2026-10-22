"""Airbnb scraper: public map-search pages, tiled over unincorporated Gunnison County.

Airbnb server-renders search results (18 per page, up to 15 pages) into a JSON
<script> block. Pins are offset by Airbnb for privacy, so coordinates are approximate.
"""
import base64
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
# 1 House, 2 Guesthouse, 3 Apartment/condo. Excludes 4 Hotel (commercial) and
# 5 Unique stays (tents, RVs, campers, glamping).
PROPERTY_TYPES = [1, 2, 3]
MAX_PAGES = 15
STATE_RE = re.compile(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', re.S)


def date_windows(today=None):
    """Airbnb only returns listings available for the searched dates, so search several."""
    today = today or date.today()
    wins = [None]  # Airbnb default: flexible one-week trip in the next few months
    for months in (3, 6, 9):
        d = today + timedelta(days=30 * months)
        d += timedelta(days=(1 - d.weekday()) % 7)  # next Tuesday
        wins.append((d.isoformat(), (d + timedelta(days=2)).isoformat()))
    return wins


def _fetch(session, url):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=40)
            m = STATE_RE.search(r.text)
            if m:
                res = json.loads(m.group(1))["niobeClientData"][0][1]["data"]["presentation"]["staysSearch"]["results"]
                return res.get("searchResults") or [], (res.get("paginationInfo") or {}).get("pageCursors") or []
        except Exception:
            pass
        time.sleep(8 * (attempt + 1))
    return None, None


def _parse(r):
    L = r.get("demandStayListing") or {}
    if not L:
        return None
    lid = base64.b64decode(L["id"]).decode().split(":")[-1]
    co = (L.get("location") or {}).get("coordinate") or {}
    card = [x.get("body") for x in ((r.get("structuredContent") or {}).get("primaryLine") or [])]
    return {
        "platform": "Airbnb",
        "listing_id": lid,
        "url": f"https://www.airbnb.com/rooms/{lid}",
        "name": (r.get("nameLocalized") or {}).get("localizedStringWithTranslationPreference"),
        "description": r.get("subtitle"),
        "rating": r.get("avgRatingLocalized"),
        "beds_baths": ", ".join(c for c in card if c and "away" not in c),
        "lat": co.get("latitude"),
        "lng": co.get("longitude"),
    }


def _search_tile(session, tile, window):
    """Return (listings, capped) for one bounding box and date window."""
    s, w, n, e = tile
    q = {"ne_lat": n, "ne_lng": e, "sw_lat": s, "sw_lng": w, "search_by_map": "true",
         "search_type": "user_map_move", "refinement_paths[]": "/homes"}
    if window:
        q.update(checkin=window[0], checkout=window[1], date_picker_type="calendar")
    base = "https://www.airbnb.com/s/homes?" + urllib.parse.urlencode(q)
    base += "".join(f"&l2_property_type_ids%5B%5D={t}" for t in PROPERTY_TYPES)
    out, queue, pages = [], [None], 0
    while queue:
        cursor = queue.pop(0)
        url = base + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        res, cursors = _fetch(session, url)
        if res is None:
            break
        out += [x for x in map(_parse, res) if x]
        pages += 1
        if cursor is None:
            queue = cursors[1:]
        time.sleep(1.2)
    return out, pages >= MAX_PAGES


def _split(tile):
    s, w, n, e = tile
    ms, mw = (s + n) / 2, (w + e) / 2
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def scrape(tiles, workers=4, log=print):
    """Scrape every tile in every date window. Returns de-duplicated listings."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US"})
    found, seen_windows = {}, {}
    jobs = [(t, w, 0) for w in date_windows() for t in tiles]
    done = 0
    while jobs:
        nxt = []
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(_search_tile, session, t, w): (t, w, d) for t, w, d in jobs}
            for f in as_completed(futs):
                tile, win, depth = futs[f]
                listings, capped = f.result()
                done += 1
                if capped and depth < 3:
                    nxt += [(t, win, depth + 1) for t in _split(tile)]
                for x in listings:
                    found.setdefault(x["listing_id"], x)
                    seen_windows.setdefault(x["listing_id"], set()).add("flexible" if win is None else win[0])
                if done % 100 == 0:
                    log(f"airbnb: {done} tile-searches, {len(found)} unique listings")
        jobs = nxt
        if jobs:
            log(f"airbnb: splitting {len(jobs)} capped tiles")
    for lid, x in found.items():
        x["search_windows"] = len(seen_windows[lid])
    return list(found.values())
