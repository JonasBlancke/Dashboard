"""
Run one city end-to-end for a date:

  1. resolve the city from cities.forecast.yaml
  2. ensure ML-UrbanHeat's V27_forecast spatial TIFs exist for it
     (copy from a staged location; never touches Google Drive)
  3. run  workflow/run_forecast_downscale_v2.py  (skips Earth Engine when the
     city's merit_height_m is cached — via the MERIT_HEIGHT_M env var)
  4. locate the newest  ta_abs_*_issued*.nc  it produced
  5. build_forecast.py -> data/forecast/<city>/latest/
  6. build_manifest.py -> data/forecast/manifest.json

Idempotent: re-running for the same day overwrites latest/.

    python pipeline/run_city_forecast.py --city ghent [--date YYYY-MM-DD]
                                         [--ml-urbanheat <path>] [--skip-model]

--skip-model reuses the newest existing NetCDF (dev / offline).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

from build_forecast import build as build_web
from build_manifest import write_manifest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "cities.forecast.yaml"
DATA = ROOT / "data" / "forecast"

DEFAULT_ML = Path(os.environ.get(
    "ML_URBANHEAT_DIR", str(ROOT.parent / "ML-UrbanHeat")))
# where staged spatial TIFs live if they aren't already in the ML-UrbanHeat tree
STAGED_SPATIAL = Path(os.environ.get(
    "SPATIAL_TIFS_DIR", str(ROOT / "data" / "forecast" / "_spatial")))


def load_registry() -> dict:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}


def ensure_spatial(ml: Path, city_name: str, sim: str, spatial_sim: str) -> None:
    """The forecast stage reads cities/<City>/processed_data/<sim>/spatial/*.tif
    (sim = the forecast config name). If that dir is missing, populate it from,
    in order: STAGED_SPATIAL/<City>/ (CI: the Release tarball), or the city's
    own <spatial_sim>/spatial/ dir (some cities keep the identical V29 resampled
    TIFs under the training sim, e.g. V29_bf1000m_gradual). Never regenerate."""
    dst = ml / "cities" / city_name / "processed_data" / sim / "spatial"
    if dst.is_dir() and any(dst.glob("*_resampled.tif")):
        print(f"  spatial: present ({dst})")
        return
    src = STAGED_SPATIAL / city_name
    if not src.is_dir():
        alt = ml / "cities" / city_name / "processed_data" / spatial_sim / "spatial"
        if spatial_sim != sim and alt.is_dir() and any(alt.glob("*_resampled.tif")):
            src = alt
        else:
            raise SystemExit(
                f"spatial TIFs missing for {city_name}: none of {dst}, {src}, "
                f"{alt} exist. Stage them (see README 'refresh spatial TIFs').")
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob("*.tif*"):
        shutil.copy2(f, dst / f.name)
        n += 1
    print(f"  spatial: copied {n} files {src} -> {dst}")


def run_downscale(ml: Path, cfg: dict, city_cfg: dict, run_date: str | None) -> None:
    # per-city config_path wins (already merged from defaults via setdefault);
    # e.g. a city with big AOI relief points at V29_forecast_lrc_pixel.yaml
    cfg_path = ml / city_cfg.get("config_path", cfg.get("config_path", "configs/V27_forecast.yaml"))
    cmd = [sys.executable, "-u", "workflow/run_forecast_downscale_v2.py",
           "--config", str(cfg_path),
           "--city-name", city_cfg["ml_urbanheat_city_name"]]
    if run_date:
        cmd += ["--start-date", run_date]
    env = dict(os.environ)
    mh = city_cfg.get("merit_height_m")
    if mh is not None:
        env["MERIT_HEIGHT_M"] = repr(float(mh))
        print(f"  MERIT_HEIGHT_M={env['MERIT_HEIGHT_M']} (Earth Engine skipped)")
    print("  $", " ".join(cmd), f"   (cwd={ml})")
    subprocess.run(cmd, cwd=ml, env=env, check=True)


def newest_nc(ml: Path, city_name: str, sim: str) -> Path:
    d = ml / "cities" / city_name / "forecast" / sim
    ncs = sorted(d.glob("ta_abs_*_issued*.nc"), key=lambda p: p.stat().st_mtime)
    if not ncs:
        raise SystemExit(f"no ta_abs_*_issued*.nc under {d}")
    return ncs[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--date", help="forecast start date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--ml-urbanheat", default=str(DEFAULT_ML))
    ap.add_argument("--skip-model", action="store_true",
                    help="reuse the newest existing NetCDF, don't run the model")
    a = ap.parse_args()

    reg = load_registry()
    defaults = reg.get("defaults", {})
    cities = reg.get("cities", {})
    if a.city not in cities:
        raise SystemExit(f"unknown city '{a.city}' — registry has {list(cities)}")
    city_cfg = {**cities[a.city]}
    for k in ("simulation_name", "config_path", "forecast_days", "warmup_hours"):
        city_cfg.setdefault(k, defaults.get(k))
    sim = city_cfg["simulation_name"]
    spatial_sim = city_cfg.get("spatial_sim", sim)
    city_name = city_cfg["ml_urbanheat_city_name"]
    ml = Path(a.ml_urbanheat)

    print(f"=== {a.city} ({city_name}) · {sim} · {a.date or 'today'} ===")
    if not a.skip_model:
        if not ml.is_dir():
            raise SystemExit(f"ML-UrbanHeat not found at {ml} (set --ml-urbanheat)")
        ensure_spatial(ml, city_name, sim, spatial_sim)
        run_downscale(ml, defaults, city_cfg, a.date)
    nc = newest_nc(ml, city_name, sim)
    print(f"  NetCDF: {nc}")

    # Rasters from the ML-UrbanHeat spatial dir (all pixel-aligned with ta_abs):
    #   bf2000m      — coarse BuildingFraction for the urban/rural UHI split
    #                  (V27 uses the 2000 m aggregate, V29 the 1000 m one)
    #   buildings/trees/water_tif — 2 m fraction rasters for context overlays
    spatial = ml / "cities" / city_name / "processed_data" / sim / "spatial"
    for key, fnames in [
        ("bf2000m", ["BuildingFraction_1000m_resampled.tif",
                     "BuildingFraction_2000m_resampled.tif"]),
        ("buildings_tif", ["BuildingFraction.tif"]),
        ("trees_tif", ["TreeFraction.tif"]),
        ("water_tif", ["WaterFraction_fraction_mask.tif"]),
    ]:
        if city_cfg.get(key):
            continue
        for fname in fnames:
            if (spatial / fname).is_file():
                city_cfg[key] = str(spatial / fname)
                break

    # ZOI — an optional smaller "zone of interest" inside the AOI. Same lookup
    # paths as the AOI clip (run_predict_maps.py::_resolve_aoi_path).
    city_root = ml / "cities" / city_name
    if not city_cfg.get("zoi_geojson"):
        for cand in (city_root / "ZOI.geojson",
                     city_root / "raw_data" / "ZOI.geojson",
                     city_root / "raw_data" / "spatial" / "ZOI.geojson"):
            if cand.is_file():
                city_cfg["zoi_geojson"] = str(cand)
                break

    build_web(nc, a.city, city_cfg, DATA)
    write_manifest(DATA)
    print("done.")


if __name__ == "__main__":
    main()
