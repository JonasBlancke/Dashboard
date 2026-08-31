"""
NetCDF -> web assets for the Live Forecast tab   (strategy A of the brief).

Input : one  ta_abs_{start}_to_{end}_issued{ISSUE}.nc  from ML-UrbanHeat's
        workflow/run_forecast_downscale_v2.py  (var `ta_abs`, degC, CF grid).

Output (per city, overwritten each run):
    data/forecast/<city>/latest/
        frame_000.png … frame_NNN.png   one lng/lat PNG per hour, fixed
                                        colour domain, NaN transparent
        values.bin                      float32, [n_hours_total, gh, gw] row-major,
                                        downsampled (<= VALUE_MAX per axis) — the
                                        FULL horizon, for the point time-series
        meta.json                       run metadata + per-frame local-time index
        preview.png                     the stage's preview_ta_abs_*.png, copied

Only the first FRAME_HOURS hours (from the forecast issue time) get map PNGs;
`values.bin` keeps every hour so the chart can still run the full ~15 days.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
import xarray as xr
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

ROOT = Path(__file__).resolve().parents[1]

FRAME_HOURS = 72          # hourly map PNGs for the coming N hours only
MIN_DOMAIN_SPAN = 5.0     # colour scale is auto from the data, but never < this °C
# Overlays reproject to EPSG:4326 (plate carrée). A MapLibre `image` source is
# *defined* to take a lng/lat image and warp it into the map's Mercator — so a
# 4326 raster with its exact lng/lat corners lands correctly, unlike a UTM or
# 3857 raster whose grid convergence leaves a small rotation vs the basemap.
DST_CRS = "EPSG:4326"

# Google "turbo" colormap, REMAPPED so the bar reads:
#   0.00–0.18  turbo's blue band, compressed (small cold portion)
#   0.18–0.55  turbo's green→yellow, roughly linear
#   0.55–1.00  turbo's orange→red, stretched (large hot portion)
# Kept in sync with the frontend (Forecast.RAMP in js/app.js).
_TURBO = [
    (0.00, (48, 18, 59)), (0.07, (70, 68, 176)), (0.14, (65, 122, 224)),
    (0.21, (56, 170, 231)), (0.28, (44, 211, 199)), (0.36, (48, 239, 148)),
    (0.43, (100, 253, 94)), (0.50, (156, 253, 58)), (0.57, (202, 240, 44)),
    (0.64, (237, 216, 51)), (0.71, (253, 182, 47)), (0.79, (253, 136, 34)),
    (0.86, (241, 92, 22)), (0.93, (214, 55, 12)), (1.00, (150, 22, 2)),
]
# (bar position, turbo position)  — piecewise-linear remap, interpolated between.
_REMAP = [(0.00, 0.00), (0.18, 0.24), (0.55, 0.55), (1.00, 1.00)]


def _sample_turbo(u: float):
    import bisect
    xs = [p[0] for p in _TURBO]
    i = min(len(xs) - 2, max(0, bisect.bisect_right(xs, u) - 1))
    (x0, c0), (x1, c1) = _TURBO[i], _TURBO[i + 1]
    f = 0.0 if x1 == x0 else (u - x0) / (x1 - x0)
    return tuple(round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))


def _remap(x: float) -> float:
    for (a, ta), (b, tb) in zip(_REMAP, _REMAP[1:]):
        if x <= b:
            f = 0.0 if b == a else (x - a) / (b - a)
            return ta + (tb - ta) * f
    return _REMAP[-1][1]


RAMP = [(x, _sample_turbo(_remap(x))) for x in [i / 20 for i in range(21)]]


def _lut(n: int = 512) -> np.ndarray:
    xs = np.array([s[0] for s in RAMP])
    cols = np.array([s[1] for s in RAMP], dtype=float)
    g = np.linspace(0, 1, n)
    out = np.empty((n, 3), np.uint8)
    for c in range(3):
        out[:, c] = np.clip(np.interp(g, xs, cols[:, c]), 0, 255)
    return out


def colorize(arr: np.ndarray, lo: float, hi: float, lut: np.ndarray) -> np.ndarray:
    finite = np.isfinite(arr)
    t = np.clip((np.where(finite, arr, lo) - lo) / (hi - lo), 0, 1)
    idx = (t * (lut.shape[0] - 1)).round().astype(np.int32)
    rgb = lut[idx]
    a = np.where(finite, 235, 0).astype(np.uint8)
    return np.dstack([rgb, a])


def _epsg_from_crs_wkt(wkt: str) -> str:
    m = re.search(r'AUTHORITY\["EPSG","(\d{4,6})"\]\s*\]\s*$', wkt)
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"UTM zone (\d+)([NS])", wkt)
    if m:
        zone, hemi = int(m.group(1)), m.group(2)
        return f"EPSG:{(32600 if hemi == 'N' else 32700) + zone}"
    raise ValueError(f"cannot resolve EPSG from crs attr: {wkt[:80]}")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# context overlays: (registry key, output name, RGB, threshold).
# Colours chosen to NOT overlap the forecast ramp: buildings near-black,
# trees a dark forest green (ramp's green is light), water a deep navy
# (ramp's blue is a mid sky blue).
CONTEXT_LAYERS = [
    ("buildings_tif", "ctx_buildings.png", (14, 14, 16), 0.5),
    ("trees_tif",     "ctx_trees.png",     (20, 70, 32), 0.5),
    ("water_tif",     "ctx_water.png",     (13, 42, 92), 0.5),
]


CONTEXT_MAX = 1200   # context overlays render sharper than the temp frames


def _render_context_overlay(tif_path, rgb, thr, geo_bounds, src_crs, out_png):
    """One static PNG: pixels with fraction >= thr get `rgb`, rest transparent.
    Rendered on its own lng/lat grid (long edge <= CONTEXT_MAX) covering
    the same extent as the forecast frames, so the mask stays crisp."""
    if not tif_path or not Path(tif_path).is_file():
        return None
    l, b, r, t = geo_bounds
    # ground aspect (lng degrees shrink by cos(lat)), so pixels are ~square
    lat0 = math.radians((b + t) / 2)
    gx = (r - l) * math.cos(lat0)
    gy = (t - b)
    if gx >= gy:
        dw = CONTEXT_MAX; dh = max(1, round(CONTEXT_MAX * gy / gx))
    else:
        dh = CONTEXT_MAX; dw = max(1, round(CONTEXT_MAX * gx / gy))
    dst_t = rasterio.transform.from_bounds(l, b, r, t, dw, dh)
    with rasterio.open(tif_path) as ds:
        a = ds.read(1).astype("float32")
        s_transform, s_crs, nd = ds.transform, ds.crs, ds.nodata
    if nd is not None:
        a[a == nd] = np.nan
    dst = np.full((dh, dw), np.nan, "float32")
    reproject(a, dst, src_transform=s_transform, src_crs=s_crs,
              dst_transform=dst_t, dst_crs=DST_CRS,
              resampling=Resampling.average, src_nodata=np.nan, dst_nodata=np.nan)
    mask = np.isfinite(dst) & (dst >= thr)
    rgba = np.zeros((dh, dw, 4), np.uint8)
    rgba[mask] = (*rgb, 255)
    Image.fromarray(rgba, "RGBA").save(out_png, optimize=True)
    return (dw, dh)


def _urban_rural_split(bf_path: Path, ta_shape):
    """Read BuildingFraction_2000m (pixel-aligned with ta_abs), split valid
    pixels at the median into Urban (upper half) / Rural (lower half).
    Returns (urban_mask, rural_mask, threshold) or None if unavailable."""
    if not bf_path or not Path(bf_path).is_file():
        return None
    with rasterio.open(bf_path) as ds:
        bf = ds.read(1).astype("float64")
        nd = ds.nodata
    if bf.shape != tuple(ta_shape):
        print(f"      ! BF2000m shape {bf.shape} != ta_abs {tuple(ta_shape)} — UHI skipped")
        return None
    valid = np.isfinite(bf) if nd is None else (np.isfinite(bf) & (bf != nd))
    thr = float(np.median(bf[valid]))
    urban = valid & (bf >= thr)
    rural = valid & (bf < thr)
    return urban, rural, thr


def build(nc_path: Path, city_id: str, cfg: dict, out_root: Path):
    ds = xr.open_dataset(nc_path)
    da = ds["ta_abs"]                       # (time, y, x) degC
    src_crs = _epsg_from_crs_wkt(ds.attrs["crs"])
    gt = [float(v) for v in ds.attrs["GeoTransform"].split()]
    ny, nx = da.shape[1], da.shape[2]
    # The GeoTransform is north-up (origin = top-left, dy < 0) but this NetCDF's
    # `y` coordinate is ASCENDING — so array row 0 is the SOUTH edge. Flip to
    # north-up so it matches src_transform (otherwise every frame is upside-down).
    if "y" in da.coords and float(da.y.values[0]) < float(da.y.values[-1]):
        da = da.isel(y=slice(None, None, -1))
    src_transform = from_origin(gt[0], gt[3], gt[1], -gt[5])

    times = np.asarray(ds["time"].values).astype("datetime64[s]")
    n_all = len(times)
    issue = np.datetime64(
        datetime.strptime(ds.attrs["forecast_issue_time_utc"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=None)
    )
    i0 = int((times >= issue).argmax()) if (times >= issue).any() else 0
    i1 = min(i0 + FRAME_HOURS, n_all)
    frame_idx = list(range(i0, i1))

    # ---- destination lng/lat grid (shared by every frame) -----------------
    # Source pixel-edge extent (GeoTransform origin is the top-left pixel EDGE).
    src_l = gt[0]
    src_t = gt[3]
    src_r = src_l + gt[1] * nx
    src_b = src_t + gt[5] * ny
    m_l, m_b, m_r, m_t = transform_bounds(src_crs, DST_CRS, src_l, src_b, src_r, src_t)
    overlay_bounds = [m_l, m_b, m_r, m_t]            # [w, s, e, n], EPSG:4326
    bbox_wgs84 = list(overlay_bounds)
    # Build the dst grid ourselves so lng/lat pixels are ~square on the ground
    # (calculate_default_transform's aspect guess over-stretches E–W at 51°N).
    # Keep the native pixel count on the longer ground axis, no resampling.
    lat0 = math.radians((m_b + m_t) / 2)
    span_x_m = (m_r - m_l) * math.cos(lat0)          # ground metres, relative
    span_y_m = (m_t - m_b)
    if span_x_m >= span_y_m:
        dw = max(nx, ny)
        dh = max(1, round(dw * span_y_m / span_x_m))
    else:
        dh = max(nx, ny)
        dw = max(1, round(dh * span_x_m / span_y_m))
    dst_transform = rasterio.transform.from_bounds(m_l, m_b, m_r, m_t, dw, dh)

    out_dir = out_root / city_id / "latest"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    lut = _lut()
    # Colour domain: auto from the frames actually shown, rounded outward to
    # whole degrees, then widened symmetrically to at least MIN_DOMAIN_SPAN °C.
    # A `value_domain_c` in the registry still wins (pin it deliberately).
    if cfg.get("value_domain_c"):
        lo, hi = (float(x) for x in cfg["value_domain_c"])
        dom_src = "registry"
    else:
        shown = da.isel(time=frame_idx).values
        shown = shown[np.isfinite(shown)]
        lo = float(np.floor(shown.min())); hi = float(np.ceil(shown.max()))
        if hi - lo < MIN_DOMAIN_SPAN:
            pad = (MIN_DOMAIN_SPAN - (hi - lo)) / 2.0
            lo -= pad; hi += pad
        dom_src = "auto"

    print(f"[{city_id}] {nc_path.name}")
    print(f"  grid {nx}x{ny} @ {src_crs}  ->  {dw}x{dh} @ 4326 (native res)")
    print(f"  {n_all} hourly steps; map frames {i0}..{i1 - 1} ({len(frame_idx)}h); "
          f"domain {lo}..{hi} °C ({dom_src})")

    # ---- per-hour PNGs (native resolution, warp only) --------------------
    for k, ti in enumerate(frame_idx):
        src = da.isel(time=ti).values.astype("float32")
        dst = np.full((dh, dw), np.nan, "float32")
        reproject(src, dst, src_transform=src_transform, src_crs=src_crs,
                  dst_transform=dst_transform, dst_crs=DST_CRS,
                  resampling=Resampling.bilinear,
                  src_nodata=np.nan, dst_nodata=np.nan)
        Image.fromarray(colorize(dst, lo, hi, lut), "RGBA").save(
            out_dir / f"frame_{k:03d}.png", optimize=True)

    # ---- values.bin — the shown window, NATIVE grid (no resampling) -----
    # one float32 per pixel per shown hour, so the hover readout is the exact
    # ta_abs value, not an interpolated one.
    grid = da.isel(time=frame_idx).values.astype("<f4")   # (n_frames, y, x)
    (out_dir / "values.bin").write_bytes(np.ascontiguousarray(grid).tobytes())
    gh, gw = ny, nx
    v_hours = len(frame_idx)

    # ---- per-hour UHI = mean(urban) - mean(rural) ----------------------
    #   split all valid pixels at the median of a coarse BuildingFraction
    #   aggregate (V27 = 2000 m, V29 = 1000 m); Urban = upper half (dense),
    #   Rural = lower half. Reported for the FULL horizon.
    uhi_block = None
    bf_path = cfg.get("bf2000m")
    split = _urban_rural_split(bf_path, (ny, nx))
    if split is not None:
        urban, rural, thr = split
        predictor = Path(bf_path).stem.replace("_resampled", "")
        vals_all = da.values                               # (time, y, x)
        u_mean = np.array([np.nanmean(vals_all[h][urban]) for h in range(n_all)])
        r_mean = np.array([np.nanmean(vals_all[h][rural]) for h in range(n_all)])
        uhi = u_mean - r_mean
        uhi_block = {
            "predictor": predictor,
            "split": "median",
            "threshold": round(thr, 4),
            "n_urban": int(urban.sum()), "n_rural": int(rural.sum()),
            "definition": "mean(ta_abs, urban) - mean(ta_abs, rural), per hour",
            "urban_mean_c": [round(float(x), 3) for x in u_mean],
            "rural_mean_c": [round(float(x), 3) for x in r_mean],
            "uhi_c": [round(float(x), 3) for x in uhi],
        }
        print(f"  UHI: split {predictor} @ {thr:.3f}  urban={int(urban.sum())} "
              f"rural={int(rural.sum())}  range {uhi.min():+.2f}..{uhi.max():+.2f} °C")

    # ---- meta.json -------------------------------------------------------
    tz = cfg.get("timezone", "UTC")
    zi = ZoneInfo(tz)
    def _iso(dt64):
        return datetime.fromtimestamp(dt64.astype("int64"), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    frame_times = []
    for ti in frame_idx:
        u = datetime.fromtimestamp(times[ti].astype("int64"), tz=timezone.utc)
        loc = u.astimezone(zi)
        day_n = (loc.date() - datetime.fromtimestamp(times[i0].astype("int64"), tz=timezone.utc).astimezone(zi).date()).days + 1
        frame_times.append({"utc": u.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "local": loc.strftime("%Y-%m-%d %H:%M"),
                            "hour_local": loc.hour, "day": day_n})
    all_times = [_iso(t) for t in times]

    # ---- context overlays (buildings / trees / water) — static PNGs ----
    geo_bounds = (m_l, m_b, m_r, m_t)   # DST_CRS metres — same extent as frames
    ctx = {}
    for key, name, rgb, thr in CONTEXT_LAYERS:
        got = _render_context_overlay(cfg.get(key), rgb, thr, geo_bounds,
                                      src_crs, out_dir / name)
        if got:
            ctx[key.replace("_tif", "")] = {
                "file": name, "color": "#%02x%02x%02x" % rgb, "threshold": thr,
                "px": list(got)}
    if ctx:
        print(f"  context overlays: {', '.join(ctx)}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "city": ds.attrs.get("city", city_id),
        "display_name": cfg.get("display_name", city_id),
        "simulation_name": cfg.get("simulation_name", "V27_forecast"),
        "forecast_model": ds.attrs.get("forecast_model", "ecmwf_ifs"),
        "forecast_issue_time_utc": ds.attrs.get("forecast_issue_time_utc"),
        "fetch_time_utc": ds.attrs.get("fetch_time_utc"),
        "run_time_utc": stamp,
        "source_nc": nc_path.name,
        "source_nc_sha256": _sha256(nc_path),
        "time_start_utc": all_times[0], "time_end_utc": all_times[-1],
        "n_hours": n_all,
        "frame_start_index": i0, "n_frames": len(frame_idx),
        "timezone": tz,
        "grid_crs": src_crs,
        "bbox_wgs84": [round(v, 6) for v in bbox_wgs84],
        "overlay_bounds_wgs84": [round(v, 6) for v in overlay_bounds],
        "value_domain_c": [round(lo, 2), round(hi, 2)],
        "value_domain_source": dom_src,
        "value_grid": {"file": "values.bin", "width": gw, "height": gh,
                       "n_hours": v_hours, "index": "frame", "dtype": "float32",
                       "bounds_wgs84": [round(v, 6) for v in bbox_wgs84]},
        "frames": frame_times,
        "all_times_utc": all_times,
        "context": ctx,
        "uhi": uhi_block,
        "has_local_observations": bool(cfg.get("has_local_observations", False)),
        "caveats": list(cfg.get("caveats", [])) + (
            [] if cfg.get("has_local_observations") else
            ["No local air-temperature observations for this city — the city-mean "
             "offset is unanchored (≈ ±0.4 °C 1σ)."]),
        "lapse_rate": {
            "forecast_grid_elevation_m": _num(ds.attrs.get("lapse_rate_forecast_grid_elevation_m")),
            "merit_height_m": _num(ds.attrs.get("lapse_rate_merit_height_m")),
            "delta_h_m": _num(ds.attrs.get("lapse_rate_delta_h_m")),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ---- preview.png (pass through the stage's quick-look) --------------
    prev = sorted(nc_path.parent.glob("preview_ta_abs_*.png"))
    if prev:
        shutil.copy2(prev[-1], out_dir / "preview.png")

    vb_kb = (out_dir / "values.bin").stat().st_size / 1024
    print(f"  wrote {len(frame_idx)} frames + values.bin ({vb_kb:.0f} KB) -> {out_dir}")
    return meta


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="ta_abs NetCDF -> Live Forecast web assets")
    ap.add_argument("--nc", required=True, help="path to a ta_abs_*.nc")
    ap.add_argument("--city", required=True, help="city id (key in the forecast registry)")
    ap.add_argument("--registry", default=str(ROOT / "cities.forecast.yaml"))
    ap.add_argument("--out", default=str(ROOT / "data" / "forecast"))
    a = ap.parse_args()

    reg = _load_registry(Path(a.registry))
    cfg = reg["cities"].get(a.city, {})
    cfg.setdefault("simulation_name", reg.get("defaults", {}).get("simulation_name", "V27_forecast"))
    build(Path(a.nc), a.city, cfg, Path(a.out))


def _load_registry(p: Path) -> dict:
    if not p.exists():
        return {"cities": {}, "defaults": {}}
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except ModuleNotFoundError:
        # tiny fallback parser — enough for this flat registry
        return _mini_yaml(p.read_text(encoding="utf-8"))


def _mini_yaml(text: str) -> dict:  # pragma: no cover - only if pyyaml missing
    raise SystemExit("pyyaml is required to read cities.forecast.yaml — pip install pyyaml")


if __name__ == "__main__":
    main()
