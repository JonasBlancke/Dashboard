#!/usr/bin/env python
"""Rebuild the High-resolution Land Cover demo's vector data (Amsterdam).

Writes b-kode-redesign/services/demo/landcover3d/<id>.json, one per
neighbourhood in demo/landcover/meta.json, with the layers landcover.js reads:

    buildings  real footprints from the urbanscape run's clean-building gpkg
               (prop HEIGHT = roof height, m)
    landcover  the 2 m classification raster, polygonised, then smoothed so the
               pixel staircase is gone (props LC_CODE / LC_NAME / LC_COLOR)
    trees      individual trees: treetops detected in the 2 m canopy height raster
               with Access_to_green's count_trees_chm.py (variable-window local
               maxima), drawn as a trunk + crown per tree

    python pipeline/export_landcover_vector.py
    python pipeline/export_landcover_vector.py --run <urbanscape_YYYYMMDD dir>
    python pipeline/export_landcover_vector.py --trees-tif <canopy height .tif>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from shapely.geometry import Polygon, box, shape, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "b-kode-redesign" / "services" / "demo"
DEFAULT_RUN = (ROOT.parent / "ML-UrbanHeat" / "cities" / "Amsterdam" / "raw_data"
               / "spatial" / "urbanscape_20261019")

# raster code -> (name, colour). Names/colours match the existing demo files and
# legend. Roof (2) is exported as ground-coloured "Paved" — an underlay beneath
# the footprints, so the thin gaps between the raster's roof pixels and the real
# outlines don't show the basemap, while the footprints alone define the edges.
CLASSES = {
    1: ("Paved", "#7d7563"),
    2: ("Paved", "#7d7563"),
    4: ("Deciduous trees", "#2b7520"),
    5: ("Grass", "#85e16e"),
    6: ("Bare soil", "#d49144"),
    7: ("Water", "#112cb6"),
}
MIN_AREA_M2 = 6.0       # drop specks smaller than ~1.5 pixels
SIMPLIFY_M = 0.8        # flatten the 2 m pixel staircase
SMOOTH_M = 0.9          # open/close radius that rounds the remaining corners
BLDG_SIMPLIFY_M = 0.25

# individual trees (canopy height raster, 2 m) + the detection from Access_to_green
TREES_TIF = (ROOT.parent / "Minority_Report" / "Code" / "MinorityReport_NEW" / "Raw_data"
             / "Amsterdam" / "urbanscape" / "final" / "Trees.tif")
TREE_SCRIPT_DIR = ROOT.parent / "github" / "Access_to_green"
TREE_A, TREE_B = 0.25, 1.0          # crown width = a*h + b (m), as in count_trees_chm.py
TRUNK_DIAM_M = 0.7
CANOPY_BASE_FRAC = 0.4              # crown starts at 40 % of the tree height


def disc(x, y, r, n):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return Polygon(np.c_[x + r * np.cos(t), y + r * np.sin(t)])


def detect_treetops(tif: Path, script_dir: Path):
    sys.path.insert(0, str(script_dir))
    from count_trees_chm import detect_trees     # VWS local-maxima treetop detection
    print(f"trees: detecting treetops in {tif.name} ...")
    gdf = detect_trees(str(tif), min_height=2.0, a=TREE_A, b=TREE_B, sigma=0.8)
    print(f"trees: {len(gdf):,} treetops")
    return gdf


def tree_layers(tops: gpd.GeoDataFrame, bounds):
    """Trunk + crown polygons (lng/lat) for the treetops inside `bounds`."""
    clip = gpd.GeoSeries([box(*bounds)], crs=4326).to_crs(tops.crs).iloc[0]
    t = tops[tops.geometry.within(clip)]
    ll = tops.crs
    trunks, crowns = [], []
    for geom, h in zip(t.geometry, t["height"]):
        h = float(h)
        base = round(CANOPY_BASE_FRAC * h, 2)
        r = max(0.9, (TREE_A * h + TREE_B) / 2)
        trunks.append((disc(geom.x, geom.y, TRUNK_DIAM_M / 2, 6), {"CANOPY_BASE": base}))
        crowns.append((disc(geom.x, geom.y, r, 10), {"HEIGHT": round(h, 2), "CANOPY_BASE": base}))
    def fc(items):
        gs = gpd.GeoSeries([g for g, _ in items], crs=ll).to_crs(4326)
        return {"type": "FeatureCollection",
                "features": [to_feature(g, p) for g, (_, p) in zip(gs, items)]}
    return {"trunks": fc(trunks), "canopies": fc(crowns)}


def to_feature(geom, props, crs_to="EPSG:4326", crs_from="EPSG:32631"):
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def landcover(lc_path: Path, bounds, pad_m=30):
    with rasterio.open(lc_path) as d:
        l, b, r, t = transform_bounds("EPSG:4326", d.crs, *bounds)
        win = from_bounds(l - pad_m, b - pad_m, r + pad_m, t + pad_m, d.transform).round_offsets().round_lengths()
        a = d.read(1, window=win)
        tr = d.window_transform(win)
        crs = d.crs
    out = []
    for code, (name, color) in CLASSES.items():
        m = a == code
        if not m.any():
            continue
        polys = [shape(g) for g, v in shapes(m.astype("uint8"), mask=m, transform=tr) if v == 1]
        geom = unary_union(polys)
        geom = geom.simplify(SIMPLIFY_M, preserve_topology=True)
        # morphological open+close: rounds stair-step corners, keeps area
        geom = geom.buffer(SMOOTH_M, join_style=1).buffer(-2 * SMOOTH_M, join_style=1).buffer(SMOOTH_M, join_style=1)
        geom = geom.simplify(SIMPLIFY_M / 2, preserve_topology=True)
        out.append((code, name, color, geom))
    return out, crs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=str(DEFAULT_RUN))
    ap.add_argument("--trees-tif", default=str(TREES_TIF), help="2 m canopy height raster")
    ap.add_argument("--tree-script-dir", default=str(TREE_SCRIPT_DIR))
    a = ap.parse_args()
    run = Path(a.run)
    lc_path = run / "final" / "lc_solweig_w_trees.tif"
    bld_path = run / "raw" / "clean-building.with_heights.gpkg"
    meta = json.loads((DEMO / "landcover" / "meta.json").read_text(encoding="utf-8"))
    meta3d_path = DEMO / "landcover3d" / "meta.json"
    meta3d = json.loads(meta3d_path.read_text(encoding="utf-8"))

    tops = detect_treetops(Path(a.trees_tif), Path(a.tree_script_dir))

    for h in meta["neighbourhoods"]:
        hid, bounds = h["id"], h["bounds"]
        f_old = DEMO / "landcover3d" / f"{hid}.json"
        old = json.loads(f_old.read_text(encoding="utf-8"))
        print(f"[{hid}] {h['label']}")

        # --- land cover, in UTM metres, clipped to the neighbourhood box ---
        classes, crs = landcover(lc_path, bounds)
        clip_ll = box(*bounds)
        clip_utm = gpd.GeoSeries([clip_ll], crs=4326).to_crs(crs).iloc[0]
        lc_feats = []
        for code, name, color, geom in classes:
            g = gpd.GeoSeries([geom.intersection(clip_utm)], crs=crs).to_crs(4326).iloc[0]
            parts = [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))
            for p in parts:
                if p.geom_type != "Polygon" or p.is_empty:
                    continue
                lc_feats.append(to_feature(p, {"LC_CODE": code, "LC_NAME": name, "LC_COLOR": color}))
        print(f"    landcover  {len(lc_feats)} polygons")

        # --- buildings: real footprints ---
        g = gpd.read_file(bld_path, bbox=tuple(bounds))
        g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
        hcol = "HEIGHT_ROOF" if "HEIGHT_ROOF" in g.columns else "height_m"
        g["HEIGHT"] = g[hcol].fillna(g.get("height_m")).fillna(6.0).clip(lower=3.0).round(1)
        u = g.to_crs(crs)
        u["geometry"] = u.geometry.buffer(0).simplify(BLDG_SIMPLIFY_M, preserve_topology=True)
        u = u.to_crs(4326)
        b_feats = []
        for geom, hh in zip(u.geometry, u["HEIGHT"]):
            geom = geom.intersection(clip_ll)
            parts = [geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))
            for p in parts:
                if p.geom_type == "Polygon" and not p.is_empty:
                    b_feats.append(to_feature(p, {"HEIGHT": float(hh)}))
        print(f"    buildings  {len(b_feats)} (was {len(old['buildings']['features'])})")

        def fc(feats):
            return {"type": "FeatureCollection", "features": feats}
        out = {"buildings": fc(b_feats), "landcover": fc(lc_feats), "trees": tree_layers(tops, bounds)}
        print(f"    trees      {len(out['trees']['canopies']['features'])} (was {len(old['trees']['canopies']['features'])})")

        def rnd(o):                       # 7 decimals ~ 1 cm; keeps files small
            if isinstance(o, float):
                return round(o, 7)
            if isinstance(o, list):
                return [rnd(x) for x in o]
            if isinstance(o, dict):
                return {k: rnd(v) for k, v in o.items()}
            return o
        f_old.write_text(json.dumps(rnd(out), separators=(",", ":")), encoding="utf-8")
        for n in meta3d["neighbourhoods"]:
            if n["id"] == hid:
                n["counts"].update(buildings=len(b_feats), landcover=len(lc_feats))
        print(f"    -> {f_old.name}  {f_old.stat().st_size/1e6:.1f} MB")

    meta3d_path.write_text(json.dumps(meta3d, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
