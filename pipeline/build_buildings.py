#!/usr/bin/env python
"""Building footprints -> static Mapbox Vector Tiles for the Live Forecast map.

The context overlay used to be a ctx_buildings.png rendered from a ~11 m grid,
which turns to blocks when zoomed in. This writes the real footprint polygons
(ML-UrbanHeat's clean-building.with_heights*.gpkg) as a plain z/x/y .pbf
pyramid, so MapLibre draws them as vectors that stay sharp at any zoom.

    data/buildings/<city>/<z>/<x>/<y>.pbf     layer "buildings", prop h = roof height (m)
    data/buildings/<city>/tiles.json          { minzoom, maxzoom, bounds, count }

Tiles stop at MAXZOOM; MapLibre overzooms them beyond that (still vector).
Buildings are static, so run this once per city (not in the daily CI);
data/ is git-ignored and the tiles ride on the site-assets Release.

    python pipeline/build_buildings.py ghent
    python pipeline/build_buildings.py ghent patras --ml-urbanheat ../ML-UrbanHeat
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import mercantile
import yaml
from mapbox_vector_tile import encode
from shapely import STRtree
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
MINZOOM, MAXZOOM = 13, 15
EXTENT = 4096
BUFFER = 64 / EXTENT            # tile-edge buffer (fraction of a tile) so polygons don't seam
MIN_AREA_M2 = {13: 12.0, 14: 5.0, 15: 0.0}   # drop specks at the coarse zooms
SRC_NAMES = ["clean-building.with_heights_slim.gpkg", "clean-building.with_heights.gpkg"]


def find_source(ml: Path, name: str) -> Path:
    spatial = ml / "cities" / name / "raw_data" / "spatial"
    for f in SRC_NAMES:
        if (spatial / f).is_file():
            return spatial / f
    raise SystemExit(f"no building footprints in {spatial} (looked for {SRC_NAMES})")


def city_bounds(city: str, ml_name: str, ml: Path):
    """lng/lat bbox to cut: the forecast extent if built, else the AOI file."""
    meta = ROOT / "data" / "forecast" / city / "latest" / "meta.json"
    if meta.is_file():
        return json.loads(meta.read_text(encoding="utf-8"))["bbox_wgs84"]
    for f in (ml / "cities" / ml_name / "raw_data" / "AOI.geojson",):
        if f.is_file():
            return list(gpd.read_file(f).total_bounds)
    return None


def build(city: str, ml_name: str, ml: Path, out_root: Path) -> None:
    src = find_source(ml, ml_name)
    bb = city_bounds(city, ml_name, ml)
    pad = 0.002
    bbox = (bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad) if bb else None
    print(f"[{city}] reading {src.name}  bbox={bbox}")
    g = gpd.read_file(src, bbox=bbox)
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    hcol = next((c for c in g.columns if c.upper().startswith("HEIGHT")), None)
    g = g.to_crs(3857)
    heights = (g[hcol].fillna(0) if hcol else 0 * g.geometry.area)
    geoms = g.geometry.values
    areas = g.geometry.area.values
    print(f"[{city}] {len(g):,} footprints")
    tree = STRtree(geoms)

    out = out_root / city
    n_tiles = 0
    west, south, east, north = (bb if bb else tuple(g.to_crs(4326).total_bounds))
    for z in range(MINZOOM, MAXZOOM + 1):
        res = 40075016.686 / (2 ** z) / EXTENT          # metres per tile unit
        min_a = MIN_AREA_M2[z]
        for t in mercantile.tiles(west, south, east, north, z):
            xmin, ymin, xmax, ymax = mercantile.xy_bounds(t)
            pad_m = (xmax - xmin) * BUFFER
            clip = box(xmin - pad_m, ymin - pad_m, xmax + pad_m, ymax + pad_m)
            feats = []
            for i in tree.query(clip):
                if areas[i] < min_a:
                    continue
                geom = geoms[i].intersection(clip)
                if geom.is_empty:
                    continue
                geom = geom.simplify(res, preserve_topology=True)
                if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
                    continue
                feats.append({"geometry": geom,
                              "properties": {"h": round(float(heights.iloc[i]), 1)}})
            if not feats:
                continue
            pbf = encode(
                [{"name": "buildings", "features": feats}],
                default_options={"quantize_bounds": (xmin, ymin, xmax, ymax),
                                 "extents": EXTENT, "y_coord_down": False})
            p = out / str(z) / str(t.x) / f"{t.y}.pbf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(pbf)
            n_tiles += 1
        print(f"[{city}] z{z} done ({n_tiles} tiles so far)")

    (out / "tiles.json").write_text(json.dumps({
        "minzoom": MINZOOM, "maxzoom": MAXZOOM,
        "bounds": [west, south, east, north], "count": int(len(g)),
        "source": src.name}), encoding="utf-8")
    size = sum(f.stat().st_size for f in out.rglob("*.pbf")) / 1e6
    print(f"[{city}] wrote {n_tiles} tiles, {size:.1f} MB -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cities", nargs="+", help="city ids from cities.forecast.yaml")
    ap.add_argument("--ml-urbanheat", default=str(ROOT.parent / "ML-UrbanHeat"))
    ap.add_argument("--out", default=str(ROOT / "data" / "buildings"))
    a = ap.parse_args()
    reg = yaml.safe_load((ROOT / "cities.forecast.yaml").read_text(encoding="utf-8"))["cities"]
    for c in a.cities:
        if c not in reg:
            sys.exit(f"unknown city '{c}' (see cities.forecast.yaml)")
        build(c, reg[c]["ml_urbanheat_city_name"], Path(a.ml_urbanheat), Path(a.out))


if __name__ == "__main__":
    main()
