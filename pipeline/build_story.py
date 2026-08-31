"""
Build the Heat Stress scroll-story assets.

One kind of asset per neighbourhood (hex) — REAL DATA for interactive display:

  hex.geojson       the AOI hexagon           (WGS84)
  streets.geojson   OSM streets, hex bbox     (WGS84)
  utci_<scen>.png   2 m UTCI, windowed-read from the source GeoTIFF,
                    coloured on a CONTINUOUS ramp over the hex's own value
                    range and CLIPPED TO THE HEXAGON (everything outside the
                    polygon is transparent — no square frame), native res

The frontend renders this as a small MapLibre instance (image overlay + two
vector line layers) so the neighbourhood stays pannable / zoomable.

Reads directly from the HeatScan drive (windowed / bbox-filtered — no full
file copies).  Override the root with $HEATSCAN_CITIES.
"""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from pathlib import Path

from affine import Affine

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from rasterio.windows import from_bounds as window_from_bounds

warnings.filterwarnings("ignore", message="Pandas requires version")

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "data" / "story" / "heatstress"
HS = Path(os.environ.get(
    "HEATSCAN_CITIES",
    r"G:\Gedeelde drives\Worldbank\Projects\HeatScan\data\cities",
))

HEX_BUFFER_M = 60          # pad the hex bbox so streets/raster reach the edge
WEBM = "EPSG:3857"

# Continuous UTCI ramp (ColorBrewer YlOrRd, 9-class) — the detail raster is
# coloured by *actual* UTCI value stretched over the hex's own 2–98 % range.
UTCI_RAMP = [
    (0.000, (255, 255, 204)), (0.125, (255, 237, 160)), (0.250, (254, 217, 118)),
    (0.375, (254, 178, 76)),  (0.500, (253, 141, 60)),  (0.625, (252, 78, 42)),
    (0.750, (227, 26, 28)),   (0.875, (189, 0, 38)),    (1.000, (128, 0, 38)),
]

SCENARIOS = [
    {"id": "present", "label": "Present", "epoch": "SOLWEIG · present-day hot day",
     "solweig": "hot_present", "typical": "typical_present"},
    {"id": "2050", "label": "2050", "epoch": "SOLWEIG · 2050 hot day · SSP5-8.5",
     "solweig": "hot_2050", "typical": "typical_2050"},
    {"id": "2090", "label": "2090", "epoch": "SOLWEIG · far-future hot day · SSP5-8.5",
     "solweig": "hot_2100", "typical": "typical_2100"},
]

CITIES = {
    "Nouakchott": {
        "heatscanId": 222,
        "label": "Nouakchott, Mauritania",
        "center": [-15.9659, 18.0874],
        "streets": "urbanscape/raw/streets-osm.geojson",
        "aoiHex": "areas_of_interest/AOI_hex.geojson",
        "hoods": [
            {"dir": "fish_market", "name": "Fish Market",
             "blurb": "Dense low-rise fishing quarter on the Atlantic shore."},
            {"dir": "cinquieme_gardens", "name": "Cinquième Gardens",
             "blurb": "Inland residential district with pockets of greenery."},
            {"dir": "el_mina", "name": "El Mina",
             "blurb": "Sprawling informal settlement south of the centre."},
        ],
    },
    "Belem": {
        "heatscanId": 4593,
        "label": "Belém, Brazil",
        "center": [-48.4399, -1.3901],
        "streets": "urbanscape/raw/streets-osm.geojson",
        "aoiHex": "areas_of_interest/AOI_hex.geojson",
        "hoods": [
            {"dir": "guama", "name": "Guamá",
             "blurb": "River-edge neighbourhood, mix of stilt housing and infill."},
            {"dir": "jurunas", "name": "Jurunas",
             "blurb": "Historic dense district between the centre and the river."},
            {"dir": "terra_firme", "name": "Terra Firme",
             "blurb": "Low-lying, flood-prone, rapidly densifying periphery."},
        ],
    },
}


# ---------------------------------------------------------------- colour
def _ramp_lut(n=256) -> np.ndarray:
    """UTCI_RAMP -> (n,3) uint8 lookup table, linear between the stops."""
    xs = np.array([s[0] for s in UTCI_RAMP])
    cols = np.array([s[1] for s in UTCI_RAMP], dtype="float64")
    u = np.linspace(0.0, 1.0, n)
    lut = np.stack([np.interp(u, xs, cols[:, c]) for c in range(3)], axis=1)
    return np.clip(np.round(lut), 0, 255).astype(np.uint8)


_LUT = _ramp_lut()


def colorize_utci(arr: np.ndarray, lo: float, hi: float,
                  inside: np.ndarray | None = None) -> np.ndarray:
    """float UTCI -> RGBA uint8 on the continuous ramp stretched to [lo, hi].
    NaN (and anything outside `inside`, when given) is fully transparent."""
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    m = np.isfinite(arr)
    if inside is not None:
        m &= inside
    if not m.any():
        return rgba
    span = max(hi - lo, 1e-6)
    t = np.clip((arr[m] - lo) / span, 0.0, 1.0)
    idx = np.clip(np.round(t * (len(_LUT) - 1)).astype(int), 0, len(_LUT) - 1)
    rgba[m, :3] = _LUT[idx]
    rgba[m, 3] = 235
    return rgba


# ---------------------------------------------------------------- raster
def hex_bounds_utm(hex_gdf: gpd.GeoDataFrame):
    g = hex_gdf.to_crs(hex_gdf.crs)  # keep native
    minx, miny, maxx, maxy = g.total_bounds
    return (minx - HEX_BUFFER_M, miny - HEX_BUFFER_M,
            maxx + HEX_BUFFER_M, maxy + HEX_BUFFER_M)


def load_hex_utci(src_tif: Path, bounds_utm, hex_gdf: gpd.GeoDataFrame):
    """Windowed read of `bounds_utm` from src_tif, reproject the little window
    to Web Mercator and rasterize the hexagon into the same grid.

    Returns (dst float32 array, inside bool mask, bounds_wgs84 [w,s,e,n],
    natural_range [p2, p98])  or  None."""
    if not src_tif.is_file():
        print(f"      MISSING {src_tif}")
        return None
    with rasterio.open(src_tif) as ds:
        win = window_from_bounds(*bounds_utm, ds.transform)
        win = win.round_offsets().round_lengths()
        if win.width < 2 or win.height < 2:
            return None
        data = ds.read(1, window=win).astype("float32")
        wt = ds.window_transform(win)
        src_crs = ds.crs
        nod = ds.nodata if ds.nodata is not None else -9999.0
    data[data == nod] = np.nan
    data[(data < -100) | (data > 100)] = np.nan

    dst_crs = WEBM
    dst_t, dw, dh = calculate_default_transform(
        src_crs, dst_crs, data.shape[1], data.shape[0],
        *rasterio.transform.array_bounds(data.shape[0], data.shape[1], wt))
    dst = np.full((dh, dw), np.nan, dtype="float32")
    reproject(source=data, destination=dst,
              src_transform=wt, src_crs=src_crs,
              dst_transform=dst_t, dst_crs=dst_crs,
              resampling=Resampling.nearest,
              src_nodata=np.nan, dst_nodata=np.nan)

    hex_webm = hex_gdf.to_crs(dst_crs)
    inside = rasterize(
        ((geom, 1) for geom in hex_webm.geometry),
        out_shape=(dh, dw), transform=dst_t, fill=0,
        all_touched=True, dtype="uint8",
    ).astype(bool)

    in_vals = dst[inside & np.isfinite(dst)]
    if in_vals.size == 0:
        return None
    lo = float(np.percentile(in_vals, 2))
    hi = float(np.percentile(in_vals, 98))

    left, top = dst_t.c, dst_t.f
    right = left + dst_t.a * dw
    bottom = top + dst_t.e * dh
    w, s, e, n = transform_bounds(dst_crs, "EPSG:4326", left, bottom, right, top)
    return dst, inside, [w, s, e, n], [lo, hi]


def save_hex_utci_png(dst, inside, lo: float, hi: float, out_png: Path):
    """Colourize a prepared `dst` grid on the continuous ramp stretched to
    [lo, hi], clipped to `inside`, and write the PNG."""
    Image.fromarray(colorize_utci(dst, lo, hi, inside), "RGBA").save(
        out_png, optimize=True)


# ---------------------------------------------------------------- vectors
def load_hex(hex_path: Path) -> gpd.GeoDataFrame:
    return gpd.read_file(hex_path)


def clip_streets(streets_path: Path, bbox_wgs84, out_path: Path,
                 keep_classes: set | None = None) -> int:
    """bbox-filtered read of the OSM streets, keep line geometry, WGS84.
    If keep_classes is given, keep only those `highway` values."""
    if not streets_path.is_file():
        print(f"      MISSING {streets_path}")
        return 0
    try:
        gdf = gpd.read_file(streets_path, bbox=tuple(bbox_wgs84))
    except Exception as e:
        print(f"      streets read failed: {e}")
        return 0
    if gdf.empty:
        out_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        return 0
    if gdf.crs is None:
        gdf.set_crs("EPSG:4326", inplace=True)
    gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
    if keep_classes is not None and "highway" in gdf.columns:
        gdf = gdf[gdf["highway"].isin(keep_classes)]
        if gdf.empty:
            out_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
            return 0
    # keep only 'highway' (a safe controlled vocab); drop free-text 'name'
    # which carries non-UTF-8 bytes that break browser JSON.parse.
    hw = gdf["highway"].fillna("road").astype(str) if "highway" in gdf.columns else "road"
    clean = gpd.GeoDataFrame({"highway": hw}, geometry=gdf.geometry, crs="EPSG:4326")
    _write_geojson(clean, out_path)
    return len(clean)


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path):
    """Emit a plain RFC-7946 FeatureCollection as UTF-8, no `crs` member."""
    obj = json.loads(gdf.to_json(drop_id=True))
    obj.pop("crs", None)
    path.write_text(json.dumps(obj, ensure_ascii=True), encoding="utf-8")


ZONAL_MAX_PX = 3000        # cap the longest raster axis for the zonal read
                           # (~15–20 m effective res — ample per ~200 m hex)
_ZONAL_CACHE = ROOT / "data" / "story" / "_zonal_cache"


def _decimated_read(tif: Path):
    """(arr float32, transform) — a decimated full read of `tif`, longest axis
    <= ZONAL_MAX_PX. Cached to a local .npz keyed by (path, size, mtime) so the
    very slow Drive read happens at most once per raster.

    GDAL random tile access over Google-Drive-streamed storage is pathological
    (Belém's UTCI_MEAN.tif is 22k×31k / ~3 GB); the cache makes re-runs instant
    and the local .npz keeps the pipeline reproducible offline."""
    st = tif.stat()
    key = hashlib.md5(f"{tif}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    cf = _ZONAL_CACHE / f"{key}.npz"
    if cf.is_file():
        z = np.load(cf, allow_pickle=True)
        return z["arr"], Affine(*z["transform"])

    # The Google-Drive-streamed source drops connections mid-read on the big
    # (~3 GB) rasters — retry a few times before giving up on the city.
    last = None
    for attempt in range(1, 5):
        try:
            with rasterio.open(tif) as ds:
                scale = max(1, int(np.ceil(max(ds.width, ds.height) / ZONAL_MAX_PX)))
                oh, ow = ds.height // scale, ds.width // scale
                print(f"        decimated read {tif.parent.name}/{tif.name}  "
                      f"{ds.width}x{ds.height} -> {ow}x{oh}  (attempt {attempt}, slow)…",
                      flush=True)
                # NEAREST, not AVERAGE: average forces every source pixel to be
                # decompressed (fatal over Drive for the 3 GB Belém raster);
                # nearest sub-samples. A hex still lands on ~100 output cells,
                # plenty for a zonal MEAN.
                arr = ds.read(1, out_shape=(oh, ow),
                              resampling=Resampling.nearest).astype("float32")
                t = ds.transform * ds.transform.scale(ds.width / ow, ds.height / oh)
            _ZONAL_CACHE.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cf, arr=arr, transform=np.array(t[:6], dtype="float64"))
            return arr, t
        except rasterio.errors.RasterioIOError as e:
            last = e
            print(f"        read failed ({e}); retrying…", flush=True)
    raise last


def _zonal_mean_typical(tif: Path, hexes_utm: gpd.GeoDataFrame) -> np.ndarray:
    """Mean of the TYPICAL-day UTCI raster inside every hexagon: one decimated
    full read (cached), a rasterized hex-id label grid, a vectorized bincount.
    Returns a float array aligned with hexes_utm.index (NaN where empty)."""
    n = len(hexes_utm)
    out = np.full(n, np.nan, dtype="float64")
    arr, t = _decimated_read(tif)
    oh, ow = arr.shape
    nod = -9999.0
    valid = np.isfinite(arr) & (arr != nod) & (arr > -100) & (arr < 100)

    # label grid: 0 = no hex, i+1 = hexes_utm row i
    labels = rasterize(
        ((geom, i + 1) for i, geom in enumerate(hexes_utm.geometry.values)
         if geom is not None and not geom.is_empty),
        out_shape=(oh, ow), transform=t, fill=0, dtype="int32", all_touched=False,
    )
    m = valid & (labels > 0)
    lab = labels[m].astype(np.int64)
    val = arr[m].astype(np.float64)
    sums = np.bincount(lab, weights=val, minlength=n + 1)[1:]
    cnts = np.bincount(lab, minlength=n + 1)[1:]
    nz = cnts > 0
    out[nz] = sums[nz] / cnts[nz]
    return out


def build_city_hexes(hs: Path, cfg: dict, out: Path):
    """AOI_hex.geojson trimmed to hex_id + geometry + a TYPICAL-day mean-UTCI
    zonal stat for EACH scenario (`utci_present` / `utci_2050` / `utci_2090`),
    computed here from `solweig_gpu/typical_<scen>/UTCI_MEAN.tif`.

    Returns (path, {scen: [lo, hi]}, [lo_all, hi_all])  or  (None, None, None)."""
    src = hs / cfg["aoiHex"]
    if not src.is_file():
        print(f"    MISSING {src}")
        return None, None, None
    g = gpd.read_file(src)
    idcol = "hex_id" if "hex_id" in g.columns else g.columns[0]
    g = g[g.geometry.notna()].reset_index(drop=True)

    # zonal stats want the hexes in the raster's UTM CRS
    with rasterio.open(hs / "solweig_gpu" / "typical_present" / "UTCI_MEAN.tif") as ds:
        ras_crs = ds.crs
    g_utm = g.to_crs(ras_crs)

    cols, ranges = {}, {}
    for sc in SCENARIOS:
        tif = hs / "solweig_gpu" / sc["typical"] / "UTCI_MEAN.tif"
        if not tif.is_file():
            print(f"    ! no typical raster for {sc['id']}: {tif}")
            continue
        vals = _zonal_mean_typical(tif, g_utm)
        cols[sc["id"]] = np.round(vals, 2)
        fin = vals[np.isfinite(vals)]
        if fin.size:
            ranges[sc["id"]] = [round(float(np.percentile(fin, 2)), 1),
                                round(float(np.percentile(fin, 98)), 1)]
        print(f"    zonal typical {sc['id']}: {np.isfinite(vals).sum()}/{len(vals)} hexes  "
              f"{np.nanmin(vals):.1f}..{np.nanmax(vals):.1f}" if fin.size else
              f"    zonal typical {sc['id']}: no data")

    if not cols:
        print("    ! no scenario zonal stats — skipping city hexes")
        return None, None, None

    data = {"hex_id": g[idcol].astype("Int64")}
    for sid, arr in cols.items():
        data[f"utci_{sid}"] = arr
    out_g = gpd.GeoDataFrame(data, geometry=g.geometry, crs="EPSG:4326")
    # keep a hex if ANY scenario has a value
    stat_cols = [f"utci_{s}" for s in cols]
    out_g = out_g[out_g[stat_cols].notna().any(axis=1)]
    # back-compat: `utci` == the present-day value (older frontends read it)
    if "utci_present" in out_g.columns:
        out_g["utci"] = out_g["utci_present"]
    _write_geojson(out_g, out / "city_hexes.geojson")

    # Durable, plain-text copy of the computed zonal stats — one row per hex,
    # scenario-day mean UTCI. Survives independently of the geojson and the
    # decimated-raster cache; re-runs don't need the drive at all if this and
    # the .npz cache are present.
    csv_cols = ["hex_id"] + stat_cols
    csv_df = out_g[csv_cols].copy()
    csv_df.insert(1, "stat", "typical_day_mean_utci")
    csv_df.to_csv(out / "hex_zonal.csv", index=False)
    print(f"    hex_zonal.csv: {len(csv_df)} rows  ({', '.join(stat_cols)})")

    all_fin = np.concatenate([out_g[c].dropna().to_numpy() for c in stat_cols])
    lo = round(float(np.percentile(all_fin, 2)), 1)
    hi = round(float(np.percentile(all_fin, 98)), 1)
    print(f"    city hexes: {len(out_g)}  ·  scenarios {list(cols)}  ·  shared range {lo}..{hi}")
    return "city_hexes.geojson", ranges, [lo, hi]


# ---------------------------------------------------------------- driver
def build_city(city: str, cfg: dict):
    hs = HS / str(cfg["heatscanId"])
    if not hs.is_dir():
        print(f"  ! {hs} not found — skipping {city}")
        return None
    out = OUT_ROOT / city
    out.mkdir(parents=True, exist_ok=True)
    print(f"City: {city}  ({hs})")

    streets_src = hs / cfg["streets"]
    city_hexes_file, hex_ranges, hex_range = build_city_hexes(hs, cfg, out)

    # city-wide streets: clip OSM to the AOI-hex bbox (still small vs the full
    # city file), keep only the through-road classes so the map stays legible.
    city_streets_file = None
    if city_hexes_file:
        cg = gpd.read_file(out / "city_hexes.geojson")
        cb = list(cg.total_bounds)
        THROUGH = {"motorway", "trunk", "primary", "secondary", "tertiary"}
        n = clip_streets(streets_src, cb, out / "city_streets.geojson",
                         keep_classes=THROUGH)
        city_streets_file = "city_streets.geojson" if n else None
        print(f"    city streets: {n} lines")

    hoods = []
    for h in cfg["hoods"]:
        hdir = out / h["dir"]
        hdir.mkdir(parents=True, exist_ok=True)
        print(f"  · {h['dir']}")

        hex_path = hs / "areas_of_interest" / "hexs" / f"{h['dir']}.geojson"
        hex_gdf = load_hex(hex_path)
        hex_id = int(hex_gdf["hex_id"].iloc[0]) if "hex_id" in hex_gdf.columns else None
        bounds_utm = hex_bounds_utm(hex_gdf)
        hex_wgs = hex_gdf.to_crs("EPSG:4326")
        _write_geojson(hex_wgs[["geometry"]], hdir / "hex.geojson")
        bbox_wgs = list(hex_wgs.total_bounds)
        pad = 0.0025
        bbox_pad = [bbox_wgs[0] - pad, bbox_wgs[1] - pad,
                    bbox_wgs[2] + pad, bbox_wgs[3] + pad]

        n_str = clip_streets(streets_src, bbox_pad, hdir / "streets.geojson")
        print(f"      streets: {n_str} lines")

        entry = {"id": h["dir"], "name": h["name"], "blurb": h["blurb"],
                 "hexId": hex_id,
                 "hex": f"{h['dir']}/hex.geojson",
                 "streets": f"{h['dir']}/streets.geojson",
                 "scenarios": []}

        # Pass 1 — load every scenario's raster and its natural 2–98 % range.
        prepared = {}
        for sc in SCENARIOS:
            src_tif = hs / "solweig_gpu" / sc["solweig"] / "UTCI_MEAN.tif"
            prepared[sc["id"]] = load_hex_utci(src_tif, bounds_utm, hex_wgs)

        # The colour scale is PINNED PER NEIGHBOURHOOD across all three
        # horizons: [min of the lows, max of the highs]. So one colour == the
        # same °C in Present / 2050 / 2090 for this hex (2050 & 2090 just read
        # hotter). It is NOT comparable to another neighbourhood's scale.
        lows = [p[3][0] for p in prepared.values() if p]
        highs = [p[3][1] for p in prepared.values() if p]
        hood_range = None
        if lows:
            lo = float(min(lows)); hi = float(max(highs))
            if hi - lo < 0.5:
                mid = 0.5 * (lo + hi); lo, hi = mid - 0.25, mid + 0.25
            hood_range = [round(lo, 2), round(hi, 2)]
        print(f"      hood UTCI scale (all scenarios): {hood_range}")

        # Pass 2 — render each PNG against the pinned hood range.
        for sc in SCENARIOS:
            sid = sc["id"]
            p = prepared[sid]
            has_png = False
            if p and hood_range:
                save_hex_utci_png(p[0], p[1], hood_range[0], hood_range[1],
                                  hdir / f"utci_{sid}.png")
                has_png = True

            entry["scenarios"].append({
                "id": sid, "label": sc["label"], "epoch": sc["epoch"],
                "utci": f"{h['dir']}/utci_{sid}.png" if has_png else None,
                "utciBounds": p[2] if p else None,      # [w,s,e,n] WGS84
                "utciNaturalRange": [round(p[3][0], 2), round(p[3][1], 2)] if p else None,
            })
        entry["utciRange"] = hood_range        # shared colourbar for the hood
        hoods.append(entry)

    manifest = {
        "product": "heatstress", "city": city, "label": cfg["label"],
        "center": cfg["center"],
        "cityHexes": city_hexes_file,
        "cityHexZonalCsv": "hex_zonal.csv" if city_hexes_file else None,
        "cityStreets": city_streets_file,
        # choropleth = TYPICAL-day mean UTCI, one zonal layer PER scenario.
        # `hexRange` is the shared [lo,hi] across all scenarios (so one colour
        # == one °C in Present / 2050 / 2090); `hexRanges` is per-scenario.
        "hexRange": hex_range,
        "hexRanges": hex_ranges,
        "hexStat": "typical_day_mean_utci",
        # continuous UTCI ramp (YlOrRd) used by the 2 m detail raster
        "utciRamp": [{"t": t, "rgb": list(rgb)} for t, rgb in UTCI_RAMP],
        "scenarios": [s["id"] for s in SCENARIOS],
        "neighbourhoods": hoods,
    }
    (out / "story.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  -> {out / 'story.json'}")
    return {"id": city, "label": cfg["label"]}


def main():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    only = os.environ.get("STORY_ONLY")          # e.g. STORY_ONLY=Belem
    idx_path = OUT_ROOT / "index.json"
    cities = []
    if only and idx_path.is_file():              # keep the other cities' entries
        cities = [c for c in json.loads(idx_path.read_text()).get("cities", [])
                  if c["id"] != only]
    for city, cfg in CITIES.items():
        if only and city != only:
            continue
        try:
            m = build_city(city, cfg)
        except Exception as e:                    # one city failing must not
            print(f"  !! {city} failed: {e}")     # discard the others
            m = None
        if m:
            cities = [c for c in cities if c["id"] != m["id"]] + [m]
    cities.sort(key=lambda c: c["id"])
    idx_path.write_text(
        json.dumps({"product": "heatstress", "cities": cities}, indent=2),
        encoding="utf-8")
    print(f"\nindex -> {idx_path}  ({len(cities)} cities)")


if __name__ == "__main__":
    main()
