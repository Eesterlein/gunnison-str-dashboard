"""Match scraped listings to county parcels and keep only unincorporated, non-commercial STRs."""
import math
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GIS = ROOT / "data" / "gis"
CRS_FT = 2232           # Colorado Central State Plane, US feet (county GIS native)
SEARCH_FT = 800         # Airbnb offsets pins; look this far for candidate parcels
NEAR_FT = 300
TILE_DEG = 0.04
NOT_STR_WORDS = re.compile(r"\b(?:rv|campsite|camp ?site|tent|glamping|camper|trailer site|tipi|teepee|yurt|bell tent)\b", re.I)


def load_gis():
    parcels = gpd.read_parquet(GIS / "parcels.parquet")
    towns = gpd.read_file(GIS / "towns.geojson").to_crs(CRS_FT)
    county = gpd.read_file(GIS / "county.geojson").to_crs(CRS_FT)
    return parcels, towns, county


def improved_unincorporated(parcels, towns):
    imp = parcels[parcels.IMPSACTUAL.fillna(0) > 0].copy()
    rp = imp.geometry.representative_point()
    return imp[~rp.within(towns.union_all())]


def search_tiles(parcels, towns):
    """Bounding boxes (s, w, n, e) covering every improved unincorporated parcel."""
    pts = improved_unincorporated(parcels, towns).geometry.representative_point().to_crs(4326)
    cells = {(math.floor(p.y / TILE_DEG), math.floor(p.x / TILE_DEG)) for p in pts}
    return [(a * TILE_DEG, b * TILE_DEG, (a + 1) * TILE_DEG, (b + 1) * TILE_DEG) for a, b in sorted(cells)]


def area_name(propertylo):
    if not isinstance(propertylo, str) or "," not in propertylo:
        return None
    return propertylo.rsplit(",", 1)[-1].strip() or None


def match(listings, parcels, towns, county):
    """Return (kept, excluded) DataFrames. kept = unincorporated STRs with parcel match info."""
    df = pd.DataFrame(listings)
    df = df.dropna(subset=["lat", "lng"])
    L = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lng, df.lat), crs=4326).to_crs(CRS_FT)
    town_u = towns.union_all()

    L["exclude_reason"] = None
    L.loc[~L.within(county.union_all()), "exclude_reason"] = "outside county"
    in_town = gpd.sjoin(L[["geometry"]], towns[["Name", "geometry"]], how="left", predicate="within")["Name"]
    in_town = in_town[~in_town.index.duplicated()]
    L.loc[in_town.notna() & L.exclude_reason.isna(), "exclude_reason"] = "inside " + in_town.str.title()
    L.loc[L.name.fillna("").str.contains(NOT_STR_WORDS) & L.exclude_reason.isna(), "exclude_reason"] = "campsite/RV"
    L["ft_to_town"] = L.geometry.distance(town_u).round()

    imp = parcels[parcels.IMPSACTUAL.fillna(0) > 0]
    cand = gpd.sjoin(L[["listing_key", "geometry"]], imp, how="inner", predicate="dwithin", distance=SEARCH_FT)
    cand["dist_ft"] = cand.geometry.distance(imp.geometry.loc[cand.index_right], align=False).round()
    cand = cand.sort_values(["listing_key", "dist_ft"])
    near = cand[cand.dist_ft <= NEAR_FT]
    n_near = near.groupby("listing_key").ParcelNumb.nunique()
    best = cand.groupby("listing_key").head(1).set_index("listing_key")

    cols = ["ACCOUNTNO", "ParcelNumb", "OWNERNAME", "ONELINEOWN", "OWNERSTREE", "OWNERCITY", "OWNERSTATE", "OWNERZIP", "PROPERTYLO", "SUBDIVISIO",
            "TAXDISTRIC", "ACCOUNTTYP", "TaxParcelT", "TOTALACTUA", "dist_ft"]
    L = L.set_index("listing_key").join(best[cols]).join(n_near.rename("parcels_within_300ft"))
    L["parcels_within_300ft"] = L.parcels_within_300ft.fillna(0).astype(int)

    def confidence(r):
        if pd.isna(r.ACCOUNTNO):
            return "Unmatched"
        if r.parcels_within_300ft <= 1 and r.dist_ft <= 150:
            return "High"
        if r.parcels_within_300ft <= 3:
            return "Medium"
        return "Low"
    L["confidence"] = L.apply(confidence, axis=1)

    # Airbnb sometimes drops listings from other places onto one bogus point (e.g. Caribbean villas
    # stacked at 38.7946,-106.5348). Four or more unmatched pins within 500 ft = bad geocode.
    um = L[L.confidence.eq("Unmatched")]
    if len(um):
        stacked = gpd.sjoin(um[["geometry"]], um[["geometry"]], predicate="dwithin", distance=500).groupby(level=0).size()
        bad = stacked[stacked >= 4].index
        L.loc[L.index.isin(bad) & L.exclude_reason.isna(), "exclude_reason"] = "bad Airbnb geocode"
    L["area"] = L.PROPERTYLO.map(area_name)
    commercial = L.ACCOUNTTYP.eq("Commercial") & L.confidence.isin(["High", "Medium"])
    L.loc[commercial & L.exclude_reason.isna(), "exclude_reason"] = "commercial parcel"

    # Up to 6 candidate parcels for staff to review on Low/Medium matches
    top = near.groupby("listing_key").head(6)
    cands = {}
    for k, a, p, ad, o, d in zip(top.listing_key, top.ACCOUNTNO, top.ParcelNumb, top.PROPERTYLO, top.OWNERNAME, top.dist_ft):
        cands.setdefault(k, []).append({"account": a, "parcel": p, "address": ad, "owner": o, "ft": int(d)})
    L["candidates"] = pd.Series(cands, dtype=object)
    L = L.reset_index()
    L["candidates"] = L.candidates.apply(lambda v: v if isinstance(v, list) else [])
    out = pd.DataFrame(L.drop(columns="geometry"))
    return out[out.exclude_reason.isna()].drop(columns="exclude_reason"), out[out.exclude_reason.notna()]
