"""Convert the county GIS shapefiles into the slim files the pipeline uses.

Run locally whenever a new county GIS export is available:
    python scripts/build_gis.py /path/to/GISData
"""
import sys
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gis"

KEEP = [
    "ParcelNumb", "ACCOUNTNO", "ACCOUNTTYP", "TaxParcelT", "OWNERNAME", "ONELINEOWN",
    "OWNERSTREE", "OWNERCITY", "OWNERSTATE", "OWNERZIP",
    "PROPERTYLO", "SUBDIVISIO", "TAXDISTRIC", "IMPSACTUAL", "TOTALACTUA", "geometry",
]
INCORPORATED = ["GUNNISON", "CRESTED BUTTE", "MT CRESTED BUTTE", "MARBLE", "PITKIN"]


def main(src):
    src = Path(src)
    OUT.mkdir(parents=True, exist_ok=True)

    p = gpd.read_file(src / "Taxparcelassessor.shp")[KEEP]
    p["ParcelNumb"] = p.ParcelNumb.astype(str)
    p = p[p.geometry.notna() & ~p.geometry.is_empty]
    p.to_parquet(OUT / "parcels.parquet", compression="zstd")  # EPSG:2232, US feet

    t = gpd.read_file(src / "Towns.shp")
    t = t[t.Notes.fillna("").str.contains("ncorp") & t.Name.isin(INCORPORATED)][["Name", "geometry"]]
    t.to_crs(4326).to_file(OUT / "towns.geojson", driver="GeoJSON")

    county = gpd.read_file(src / "Taxdistrict.shp")[["geometry"]].dissolve()
    county["geometry"] = county.buffer(200).simplify(100)
    county.to_crs(4326).to_file(OUT / "county.geojson", driver="GeoJSON")

    print(f"parcels: {len(p)}  towns: {list(t.Name)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "Projects/gunnison-assessor-map/GISData")
