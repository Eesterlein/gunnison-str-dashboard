"""Weekly pipeline: scrape -> match to parcels -> update history -> write dashboard data.

    python -m pipeline.run                      # scrape all automated platforms
    python -m pipeline.run --platforms airbnb
    python -m pipeline.run --rebuild-only       # no scraping; regenerate docs/data from history
"""
import argparse
import gzip
import json
from collections import Counter
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from pipeline import match as M
from pipeline.platforms import airbnb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
HIST = ROOT / "data" / "history"
SITE = ROOT / "docs" / "data"
PLATFORMS = {"airbnb": airbnb}
DROP_GUARD = 0.5  # a platform returning < 50% of last run's count is treated as blocked


def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent, default=str))


def scrape(names, tiles, run_date):
    results, status = [], {}
    prev_counts = (load_json(HIST / "runs.json", []) or [{}])[-1].get("scraped_by_platform", {})
    for name in names:
        try:
            rows = PLATFORMS[name].scrape(tiles, log=log)
        except Exception as e:  # keep going; other platforms still update
            log(f"{name} FAILED: {e}")
            status[name] = {"ok": False, "count": 0, "error": str(e)[:200]}
            continue
        prev = prev_counts.get(PLATFORMS[name].__name__.split(".")[-1].title(), 0)
        ok = not (prev and len(rows) < prev * DROP_GUARD)
        status[name] = {"ok": ok, "count": len(rows)}
        if not ok:
            log(f"{name}: only {len(rows)} vs {prev} last run - not marking anything delisted")
        out = RAW / run_date / f"{name}.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt") as f:
            json.dump(rows, f)
        results += rows
    return results, status


def update_history(kept, status, run_date):
    master = {x["listing_key"]: x for x in load_json(HIST / "listings.json", [])}
    seen = set()
    for rec in kept.to_dict("records"):
        k = rec["listing_key"]
        seen.add(k)
        old = master.get(k, {})
        rec["first_seen"] = old.get("first_seen", run_date)
        rec["last_seen"] = run_date
        rec["active"] = True
        if old.get("staff_confirmed_account"):  # reserved for a future "confirm parcel" module
            rec["staff_confirmed_account"] = old["staff_confirmed_account"]
        master[k] = rec
    trusted = {PLATFORMS[n].__name__.split(".")[-1].title() for n, s in status.items() if s["ok"]}
    for k, rec in master.items():
        if k not in seen and rec.get("active") and rec["platform"] in trusted:
            rec["active"] = False
            rec["delisted_on"] = run_date
    recs = sorted(master.values(), key=lambda r: r["listing_key"])
    save_json(HIST / "listings.json", recs)
    return recs


def kpis(recs, run_date, status, scraped):
    active = [r for r in recs if r["active"]]
    accounts = {r["ACCOUNTNO"] for r in active if r.get("ACCOUNTNO") and r["confidence"] in ("High", "Medium")}
    return {
        "date": run_date,
        "active_listings": len(active),
        "new_listings": sum(r["first_seen"] == run_date for r in active),
        "delisted": sum(r.get("delisted_on") == run_date for r in recs),
        "likely_accounts": len(accounts),
        "unmatched": sum(r["confidence"] == "Unmatched" for r in active),
        "by_confidence": dict(Counter(r["confidence"] for r in active)),
        "by_platform": dict(Counter(r["platform"] for r in active)),
        "scraped_by_platform": dict(Counter(x["platform"] for x in scraped)),
        "platform_status": status,
    }


def build_site(recs, parcels, runs):
    SITE.mkdir(parents=True, exist_ok=True)
    keep_cols = ["listing_key", "platform", "listing_id", "url", "name", "description", "rating", "beds_baths",
                 "lat", "lng", "confidence", "dist_ft", "parcels_within_300ft", "ACCOUNTNO", "ParcelNumb",
                 "OWNERNAME", "ONELINEOWN", "OWNERSTREE", "OWNERCITY", "OWNERSTATE", "OWNERZIP", "PROPERTYLO", "SUBDIVISIO", "TAXDISTRIC", "ACCOUNTTYP", "area",
                 "TOTALACTUA", "ft_to_town", "candidates", "first_seen", "last_seen", "active", "delisted_on",
                 "search_windows"]
    last = runs[-1]["date"] if runs else None
    recent = [r for r in recs if r["active"] or (r.get("delisted_on") and r["delisted_on"] >= _weeks_ago(last, 12))]
    site = [{c: _clean(r.get(c)) for c in keep_cols} for r in recent]
    save_json(SITE / "listings.json", site)

    accts = {r["ACCOUNTNO"]: r["confidence"] for r in recent if r.get("ACCOUNTNO") and r["active"]}
    poly = parcels[parcels.ACCOUNTNO.isin(accts)][["ACCOUNTNO", "ParcelNumb", "PROPERTYLO", "geometry"]].copy()
    poly["geometry"] = poly.simplify(3)
    poly["confidence"] = poly.ACCOUNTNO.map(accts)
    poly.to_crs(4326).to_file(SITE / "parcels.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    gpd.read_file(M.GIS / "towns.geojson").to_file(SITE / "towns.geojson", driver="GeoJSON", COORDINATE_PRECISION=5)
    gpd.read_file(M.GIS / "county.geojson").to_file(SITE / "county.geojson", driver="GeoJSON", COORDINATE_PRECISION=5)
    save_json(SITE / "kpis.json", runs, indent=1)


def _weeks_ago(d, n):
    return (pd.Timestamp(d) - pd.Timedelta(weeks=n)).date().isoformat() if d else "0000"


def _clean(v):
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platforms", default=",".join(PLATFORMS))
    ap.add_argument("--rebuild-only", action="store_true")
    ap.add_argument("--rematch", action="store_true", help="re-match data/raw/<date> instead of scraping")
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args()

    parcels, towns, county = M.load_gis()
    runs = load_json(HIST / "runs.json", [])
    if a.rebuild_only:
        build_site(load_json(HIST / "listings.json", []), parcels, runs)
        return

    if a.rematch:  # re-run matching on a saved raw scrape (e.g. after changing match rules)
        scraped, status = [], {}
        for f in sorted((RAW / a.date).glob("*.json.gz")):
            with gzip.open(f, "rt") as fh:
                rows = json.load(fh)
            scraped += rows
            status[f.name.split(".")[0]] = {"ok": True, "count": len(rows)}
    else:
        tiles = M.search_tiles(parcels, towns)
        log(f"{len(tiles)} search tiles over unincorporated improved parcels")
        scraped, status = scrape(a.platforms.split(","), tiles, a.date)
    if not scraped:
        raise SystemExit("nothing scraped")
    for x in scraped:
        x["listing_key"] = f"{x['platform']}:{x['listing_id']}"
    kept, excluded = M.match(scraped, parcels, towns, county)
    log(f"kept {len(kept)} unincorporated STR listings; excluded {len(excluded)}: "
        f"{dict(Counter(excluded.exclude_reason.str.replace(r'inside .*', 'inside a town', regex=True)))}")
    recs = update_history(kept, status, a.date)
    k = kpis(recs, a.date, status, scraped)
    k["excluded"] = dict(Counter(excluded.exclude_reason.str.replace(r"inside .*", "inside a town", regex=True)))
    runs = [r for r in runs if r["date"] != a.date] + [k]
    save_json(HIST / "runs.json", runs, indent=1)
    build_site(recs, parcels, runs)
    log(json.dumps({x: k[x] for x in ("active_listings", "new_listings", "delisted", "likely_accounts")}))


if __name__ == "__main__":
    main()
