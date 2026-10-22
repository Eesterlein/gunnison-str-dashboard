# Gunnison STR Monitor

A free, self-updating dashboard of short-term rental listings (stays of 30 days or less) in **unincorporated Gunnison County, Colorado**. Each listing is matched to a county assessor account and parcel.

**Live demo:** https://eesterlein.github.io/gunnison-str-dashboard/

Demo sign-in: username `demo`, password `gunnison-str-2026`. The sign-in is a placeholder. All data shown is public assessor and listing information, and the data files can be reached directly. Replace it with real authentication before production use.

## What it does

Every Monday a GitHub Action:

1. Searches Airbnb's public map results across ~300 map tiles. The tiles cover every improved parcel outside the five incorporated towns (Gunnison, Crested Butte, Mt. Crested Butte, Marble, Pitkin). Each tile is searched in 4 date windows, so listings booked on some dates still show up.
2. Keeps only houses, condos and guesthouses. Hotels, campsites, RV sites, tents and glamping are excluded, as are listings inside the towns.
3. Matches each listing pin to the nearest improved parcel in the county GIS and rates the match:
   - **High:** only one improved parcel within 300 ft, and the pin is within 150 ft of it
   - **Medium:** 2–3 parcels within 300 ft
   - **Low:** a dense area (condos, townhomes); the dashboard lists up to 6 candidate parcels
   - **Unmatched:** no improved parcel within 800 ft
4. Updates the history: first-seen and last-seen dates for each listing, plus new and delisted listings. It then recomputes the KPIs and commits the data. GitHub Pages serves the result.

Only stays of 30 days or less are captured. Every result comes from searches for stays of 2 nights to 1 week, so listings with a 30+ night minimum never appear.

## Dashboard

- **KPIs:** active listings, likely STR accounts, new this week, delisted this week, unmatched, plus a weekly trend
- **Breakdowns:** by match confidence, by area (the county's location areas) and by platform. Click a bar to filter.
- **Map:** matched parcel outlines and listing pins on Topo or Satellite basemaps. Town limits are shaded and excluded.
- **Search** by owner, address, account, parcel or listing name.
- **Detail panel:** owner, mailing address, property address, account, parcel, area, subdivision, tax district and listing info. It links to the [county property record](https://property.spatialest.com/co/gunnison) and to the listing.
- **CSV download** of whatever is currently filtered. Optionally limit it to the current map view.

## Repository layout

```
pipeline/run.py              weekly entry point (scrape -> match -> history -> site data)
pipeline/match.py            parcel matching, unincorporated / campsite / commercial filters
pipeline/platforms/airbnb.py Airbnb scraper
scripts/build_gis.py         converts the county shapefiles into data/gis/
data/gis/                    parcels (Feb 2026 county export), town limits, county outline
data/raw/YYYY-MM-DD/         raw scrape per run (gzipped)
data/history/                listings.json (master with first/last seen), runs.json (KPIs per run)
docs/                        the dashboard (GitHub Pages)
```

## Running it

```bash
pip install -r requirements.txt
python -m pipeline.run                    # full weekly run
python -m pipeline.run --rebuild-only     # regenerate docs/data without scraping
python scripts/build_gis.py /path/to/GISData   # after a new county GIS export
```

To serve the dashboard locally: `cd docs && python -m http.server 8790`.

Trigger a run by hand from **Actions → Weekly STR scan → Run workflow**.

## Safety checks

- If a platform returns less than half of last week's count (likely blocked), nothing from that platform is marked delisted that week.
- If a platform fails, the run continues with the others. The dashboard's Sources box shows each platform's status.

## Roadmap

- **Vrbo** (Phase 2). Vrbo blocks automated servers, so this needs a weekly job on a staff computer using a real browser.
- More platforms: Booking.com vacation homes and property-manager sites.
- Staff "confirm parcel" for Low and Medium matches.
- Registration upload and matching, and letter generation.
- Real authentication.

## Notes

- Listing platforms' terms of service restrict automated access. Get IT and legal sign-off before production use.
- Airbnb deliberately offsets map pins, so parcel matches are leads to verify, not determinations.
