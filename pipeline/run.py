"""Monthly pipeline: scrape -> match to parcels -> update history -> write dashboard data.

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
from pipeline.platforms import airbnb, airbnb_details

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
HIST = ROOT / "data" / "history"
SITE = ROOT / "docs" / "data"
PLATFORMS = {"airbnb": airbnb}
DROP_GUARD = 0.8  # a platform returning < 80% of its recent normal count is treated as partly blocked
MISS_RUNS = 2    # a listing counts as delisted only after this many full scans in a row without it
DETAIL_FIELDS = ("guests", "bedrooms", "beds", "baths", "license_mentions", "description_text", "page", "details_fetched")
DETAIL_MAX_AGE_DAYS = 25  # scans are monthly, so every listing page is re-read each scan (license numbers get added)
DETAIL_MAX_PER_RUN = 500
GUARD_RUNS = 4   # "recent normal" = the highest count among this many previous runs


def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _jsonable(v):
    """Recursively replace NaN/NA/NaT with None; browsers reject NaN in JSON."""
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, bool, int)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    return v


def save_json(path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(obj), indent=indent, default=str, allow_nan=False))


def guard_status(name, count, run_date):
    """Compare a platform's raw count with its best recent run; a big drop means it was partly blocked."""
    label = PLATFORMS[name].__name__.split(".")[-1].title()
    prior = [r for r in load_json(HIST / "runs.json", []) if r["date"] < run_date][-GUARD_RUNS:]
    best = max((r.get("scraped_by_platform", {}).get(label, 0) for r in prior), default=0)
    ok = not (best and count < best * DROP_GUARD)
    if not ok:
        log(f"{name}: only {count} vs {best} in recent runs - likely partly blocked; not marking anything delisted")
    return {"ok": ok, "count": count, **({} if ok else {"note": f"partial scan: {count} of ~{best} results"})}


def scrape(names, tiles, run_date):
    results, status = [], {}
    for name in names:
        try:
            rows = PLATFORMS[name].scrape(tiles, log=log)
        except Exception as e:  # keep going; other platforms still update
            log(f"{name} FAILED: {e}")
            status[name] = {"ok": False, "count": 0, "error": str(e)[:200]}
            continue
        status[name] = guard_status(name, len(rows), run_date)
        out = RAW / run_date / f"{name}.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt") as f:
            json.dump(rows, f)
        results += rows
    return results, status


def update_history(kept, status, run_date, excluded_keys=(), redo=False):
    master = {x["listing_key"]: x for x in load_json(HIST / "listings.json", [])}
    if redo:  # re-running a saved scan: undo what that scan did, then decide again
        for rec in master.values():
            if rec.get("delisted_on") == run_date:
                rec["active"] = True
                rec.pop("delisted_on", None)
            if run_date in rec.get("missed", []):
                rec["missed"].remove(run_date)
                if not rec["missed"]:
                    rec.pop("missed")
    # A listing kept by an earlier run but excluded by a newer rule was never a real STR here:
    # remove it instead of counting it as delisted.
    for k in excluded_keys:
        master.pop(k, None)
    seen = set()
    for rec in kept.to_dict("records"):
        # Drop empty cells: a column only some rows have (e.g. listing-page details) is NaN for the rest,
        # and NaN is truthy, so it would read as "already fetched".
        rec = {c: v for c, v in rec.items() if isinstance(v, (list, dict)) or not pd.isna(v)}
        k = rec["listing_key"]
        seen.add(k)
        old = master.get(k, {})
        rec["first_seen"] = old.get("first_seen", run_date)
        rec["last_seen"] = run_date
        rec["active"] = True
        if old.get("staff_confirmed_account"):  # reserved for a future "confirm parcel" module
            rec["staff_confirmed_account"] = old["staff_confirmed_account"]
        for f in DETAIL_FIELDS:  # listing-page details are refreshed on their own schedule
            if f in old:
                rec[f] = old[f]
        master[k] = rec
    trusted = {PLATFORMS[n].__name__.split(".")[-1].title() for n, s in status.items() if s["ok"]}
    # One missed scan is usually a scraping glitch (throttled tiles), so a listing stays
    # active but flagged until it has been missing from MISS_RUNS full scans in a row.
    for k, rec in master.items():
        if k not in seen and rec.get("active") and rec["platform"] in trusted:
            rec.setdefault("missed", []).append(run_date)
            if len(rec["missed"]) >= MISS_RUNS:
                rec["active"] = False
                rec["delisted_on"] = run_date
    recs = sorted(master.values(), key=lambda r: r["listing_key"])
    save_json(HIST / "listings.json", recs)
    return recs


def apply_details(recs, run_date, fetch=airbnb_details.fetch_many):
    """Read listing pages: details for new/stale listings, and an existence check for listings missing from search."""
    cutoff = _days_ago(run_date, DETAIL_MAX_AGE_DAYS)
    todo = [r for r in recs if r["platform"] == "Airbnb" and r["active"] and
            (r.get("missed") or not r.get("details_fetched") or r["details_fetched"] < cutoff)]
    todo.sort(key=lambda r: (not r.get("missed"), r.get("details_fetched") or ""))  # existence checks first
    todo = todo[:DETAIL_MAX_PER_RUN]
    if not todo:
        return recs, {"ok": True, "count": 0}
    log(f"airbnb details: reading {len(todo)} listing pages")
    got = fetch([r["listing_id"] for r in todo], log=log)
    n = Counter()
    for r in todo:
        d = got.get(r["listing_id"])
        if not d:
            continue
        n[d["page"]] += 1
        if d["page"] == "removed":  # page gone: delisted now, no need to wait for a second missed scan
            r["active"], r["delisted_on"], r["delist_reason"] = False, run_date, "listing page removed"
        elif d["page"] == "ok":
            for f in DETAIL_FIELDS:
                if f in d:
                    r[f] = d[f]
            r["page"], r["details_fetched"] = "ok", d["fetched"]
            if r.get("missed"):  # still listed, just not returned by search this scan
                r["still_listed_on"] = run_date
                r.pop("missed")
    log(f"airbnb details: {dict(n)}")
    save_json(HIST / "listings.json", recs)
    tried = len(got)
    return recs, {"ok": n["error"] <= max(5, tried * 0.2), "count": tried, "pages": dict(n)}


def _days_ago(d, n):
    return (pd.Timestamp(d) - pd.Timedelta(days=n)).date().isoformat()


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
        "missing_this_run": sum(bool(r.get("missed")) for r in active),
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
                 "TOTALACTUA", "ft_to_town", "candidates", "first_seen", "last_seen", "active", "delisted_on", "missed",
                 "search_windows", "guests", "bedrooms", "beds", "baths", "license_mentions", "page", "details_fetched",
                 "delist_reason", "still_listed_on"]
    last = runs[-1]["date"] if runs else None
    recent = [r for r in recs if r["active"] or (r.get("delisted_on") and r["delisted_on"] >= _weeks_ago(last, 26))]
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
    ap.add_argument("--no-details", action="store_true", help="skip reading individual listing pages")
    ap.add_argument("--rebuild-history", action="store_true", help="replay all saved scans from scratch")
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args()

    parcels, towns, county = M.load_gis()
    runs = load_json(HIST / "runs.json", [])
    if a.rebuild_only:
        build_site(load_json(HIST / "listings.json", []), parcels, runs)
        return

    if a.rebuild_history:  # replay every saved scan from scratch, oldest first
        for f in (HIST / "listings.json", HIST / "runs.json"):
            if f.exists():
                f.rename(f.with_suffix(".json.bak"))
        for d in sorted(p.name for p in RAW.iterdir() if p.is_dir()):
            log(f"--- {d}")
            process(d, *load_raw(d), parcels, towns, county, redo=False, details=False)
        return

    if a.rematch:  # re-run matching on a saved raw scrape (e.g. after changing match rules)
        scraped, status = load_raw(a.date)
    else:
        tiles = M.search_tiles(parcels, towns)
        log(f"{len(tiles)} search tiles over unincorporated improved parcels")
        scraped, status = scrape(a.platforms.split(","), tiles, a.date)
    process(a.date, scraped, status, parcels, towns, county, redo=a.rematch, details=not a.rematch and not a.no_details)


def load_raw(run_date):
    scraped, status = [], {}
    for f in sorted((RAW / run_date).glob("*.json.gz")):
        with gzip.open(f, "rt") as fh:
            rows = json.load(fh)
        scraped += rows
        status[f.name.split(".")[0]] = guard_status(f.name.split(".")[0], len(rows), run_date)
    return scraped, status


def process(run_date, scraped, status, parcels, towns, county, redo, details=False):
    if not scraped:
        raise SystemExit("nothing scraped")
    for x in scraped:
        x["listing_key"] = f"{x['platform']}:{x['listing_id']}"
    kept, excluded = M.match(scraped, parcels, towns, county)
    reasons = dict(Counter(excluded.exclude_reason.str.replace(r"inside .*", "inside a town", regex=True)))
    log(f"kept {len(kept)} unincorporated STR listings; excluded {len(excluded)}: {reasons}")
    recs = update_history(kept, status, run_date, set(excluded.listing_key), redo=redo)
    if details:
        recs, status["airbnb_details"] = apply_details(recs, run_date)
    k = kpis(recs, run_date, status, scraped)
    k["excluded"] = reasons
    runs = [r for r in load_json(HIST / "runs.json", []) if r["date"] != run_date] + [k]
    runs.sort(key=lambda r: r["date"])
    save_json(HIST / "runs.json", runs, indent=1)
    build_site(recs, parcels, runs)
    log(json.dumps({x: k[x] for x in ("active_listings", "new_listings", "delisted", "missing_this_run", "likely_accounts")}))

if __name__ == "__main__":
    main()
