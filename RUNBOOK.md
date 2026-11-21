# Runbook: when the weekly scan breaks

The scan runs every Monday (GitHub Actions → **Weekly STR scan**). When something goes wrong, GitHub emails the repo owner because the run fails. Partial data is still saved first, and the safety checks make sure nothing is wrongly marked delisted.

Start by opening the failed run and reading the red error line at the bottom of the log. It will match one of these cases.

## 1. "Airbnb page format changed"

**Log says:** `none of the first 20 Airbnb searches returned readable results`, then `nothing scraped`.

**What happened:** Airbnb changed how its search page carries listing data. This happens a few times a year. Nothing was updated this week.

**Fix:**
1. Open a search page in Chrome, for example `https://www.airbnb.com/s/homes?ne_lat=38.9&ne_lng=-106.9&sw_lat=38.8&sw_lng=-107.0&search_by_map=true`.
2. View the page source and search for `data-deferred-state`. The listing data is the JSON inside that `<script>` tag.
3. Compare it with the two places that read it in `pipeline/platforms/airbnb.py`:
   - `STATE_RE`: the script tag's id.
   - `_fetch`: the path to the results, `niobeClientData[0][1].data.presentation.staysSearch.results`.
   - `_parse`: the fields read from each result (`demandStayListing.id`, `location.coordinate`, the name).
4. Update whichever changed. Then test on a few tiles:
   ```bash
   python -c "from pipeline.platforms import airbnb as A; print(len(A.scrape([(38.85,-107.0,38.89,-106.96)])))"
   ```
5. Push, then re-run: **Actions → Weekly STR scan → Run workflow**.

With Claude Code: open it in this repo, paste the failed run's log, and ask it to fix the Airbnb scraper.

## 2. "Scan was partial" (throttled)

**Log says:** `Alert if any source was partial or failed`, with `"ok": false` for `airbnb`.

**What happened:** Airbnb throttled the scan. Throttled pages look normal but contain no results, so the scan found less than 80% of its usual count (the best of the last 4 runs). The data is saved, but **nothing was marked delisted this week**, so the dashboard stays correct.

**Fix:** usually nothing. Next week's scan normally recovers. If it happens two or three weeks in a row, re-run by hand at a different time of day. You can also lower `workers` in `airbnb.scrape()` from 4 to 2 to slow the scan down.

If the listing-pages step (`airbnb_details`) is the partial one, Airbnb blocked the page reads partway through. The pages it missed are tried again next week.

## 3. Listing pages stopped reading

**Log says:** `airbnb details: 15 errors in a row - stopping`.

**What happened:** either Airbnb blocked the page reads, or the listing page format changed.

**Fix:** open any listing (`https://www.airbnb.com/rooms/<id>`), view the source, and check `parse()` in `pipeline/platforms/airbnb_details.py`. It looks for the `overview` items ("3 bedrooms", "2 baths") and `longDescriptionHtml`. Test with:
```bash
python -c "from pipeline.platforms import airbnb_details as D; print(D.fetch_many(['<listing id>']))"
```

## 4. Matching or GIS errors

**Log shows:** a Python traceback in `pipeline/match.py` or `load_gis()`, for example `KeyError: 'ACCOUNTNO'`.

**What happened:** a new county GIS export changed column names or file layout.

**Fix:** rebuild the GIS files with `python scripts/build_gis.py /path/to/GISData`, map any renamed columns in that script, and re-run.

## Handy commands

```bash
python -m pipeline.run                            # full weekly run (scrape, match, listing pages, rebuild)
python -m pipeline.run --no-details               # skip reading individual listing pages
python -m pipeline.run --rematch --date 2026-11-16  # redo one saved scan (undoes what it did first)
python -m pipeline.run --rebuild-history          # replay every saved scan from scratch
python -m pipeline.run --rebuild-only             # regenerate docs/data only
cd docs && python -m http.server 8790             # view the dashboard locally
```

## How the safety rules work

- **Partial scans:** a platform that returns less than 80% of its best count from the last 4 runs is treated as partly blocked, and nothing from it is marked delisted that run.
- **Two missed scans:** a listing missing from one scan stays active and is flagged. It's delisted only after two full scans in a row without it.
- **Listing pages settle it early:** if a missing listing's page still loads, it's still listed, so the flag is cleared. If the page returns 404, it's delisted right away.
- **Retries:** an empty search is retried only if the same map tile found listings for other dates. That's the pattern a throttled stretch leaves behind.
