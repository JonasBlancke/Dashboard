"""
Rolling 48 h forecast hindcast archive for the Live Forecast tab.

`build_forecast.build()` writes `data/forecast/<city>/latest/values.bin` — the
NATIVE ta_abs grid for the coming 72 h (shape (n_frames, ny, nx), <f4, row-major,
NOT reprojected). Those forward hours are thrown away tomorrow. This script keeps
them: after every run it upserts the fresh 72 h forecast into a per-city archive
and trims the archive to the last 48 h. Two days of runs later, the hours in
[now-48h, now] are all covered by slabs that — at the time they were archived —
were future forecasts from a run whose issue time is <= that hour, i.e. a real,
verifiable past forecast. `source_issue` on every slab records which run it came
from, so the frontend can be honest about lead time and the small step where two
runs meet.

Outputs, all in `data/forecast/<city>/latest/` (committed alongside the run):

    hindcast.bin              float32 LE, row-major, [H, ny, nx]  (H <= 48)
                              SAME native grid as meta.value_grid
    hindcast.json             { file,width,height,n_hours,index:"hour",dtype,
                                bounds_wgs84, hours:[{utc,local,source_issue}] }
    hindcast_frames/hframe_NNN.png
                              one lng/lat PNG per archived hour, rendered with
                              build_forecast's RAMP on the run's fixed colour
                              domain, so past and future frames share a scale

Called from run_city_forecast.py right after build_web(); wrapped in try/except
there so a failure never breaks the forecast build.

    python pipeline/build_hindcast.py --city ghent [--now ISO8601] [--hours 48]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

# Reuse the exact ramp + colouriser the forward frames use, so hframe_*.png and
# frame_*.png are visually identical for the same value.
from build_forecast import RAMP, _lut, colorize

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "forecast"

WINDOW_HOURS = 48
DST_CRS = "EPSG:4326"


# --------------------------------------------------------------------------- #
# archive IO                                                                  #
# --------------------------------------------------------------------------- #
def _load_archive(latest: Path, previous: Path) -> dict:
    """Return {utc: {"data": np.ndarray(ny,nx), "source_issue": str}} to seed the
    merge from. In order of preference:
      1. latest/hindcast.*   (a prior step may have copied it in)
      2. previous/hindcast.* (CI + refresh.sh rotate latest->previous first)
      3. previous/values.bin (BOOTSTRAP: no archive yet, but yesterday's forecast
         still covers the last ~24 h — fold it in so the first runs already show
         a real past window instead of nothing)
    Empty dict only if none of those exist."""
    for base in (latest, previous):
        binp, jsonp = base / "hindcast.bin", base / "hindcast.json"
        if not (binp.is_file() and jsonp.is_file()):
            continue
        try:
            hdr = json.loads(jsonp.read_text(encoding="utf-8"))
            h, w = int(hdr["height"]), int(hdr["width"])
            hours = hdr["hours"]
            raw = np.frombuffer(binp.read_bytes(), dtype="<f4")
            if raw.size != len(hours) * h * w:
                print(f"  hindcast: {base.name}/hindcast.bin size mismatch — ignoring")
                continue
            cube = raw.reshape(len(hours), h, w)
            out = {}
            for k, hr in enumerate(hours):
                out[hr["utc"]] = {"data": np.array(cube[k], dtype="float32"),
                                  "source_issue": hr.get("source_issue")}
            print(f"  hindcast: loaded {len(out)} slabs from {base.name}/")
            return out
        except Exception as e:  # pragma: no cover - corrupt archive, start fresh
            print(f"  hindcast: could not read {base.name}/hindcast.* ({e})")

    # bootstrap from the previous run's forecast grid
    pmeta_p, pvals_p = previous / "meta.json", previous / "values.bin"
    if pmeta_p.is_file() and pvals_p.is_file():
        try:
            pmeta = json.loads(pmeta_p.read_text(encoding="utf-8"))
            out = {u: {"data": s, "source_issue": i}
                   for u, s, i in _fresh_slabs(pmeta, pvals_p)}
            print(f"  hindcast: bootstrapped {len(out)} slabs from previous/values.bin"
                  f" (issue {pmeta.get('forecast_issue_time_utc')})")
            return out
        except Exception as e:  # pragma: no cover
            print(f"  hindcast: could not bootstrap from previous/values.bin ({e})")
    return {}


def _fresh_slabs(meta: dict, values_bin: Path):
    """Yield (utc_iso, np.ndarray(ny,nx) float32, issue_iso) for every hour in
    the just-written values.bin."""
    vg = meta["value_grid"]
    h, w, n = int(vg["height"]), int(vg["width"]), int(vg["n_hours"])
    raw = np.frombuffer(values_bin.read_bytes(), dtype="<f4")
    if raw.size != n * h * w:
        raise RuntimeError(f"values.bin size {raw.size} != {n}*{h}*{w}")
    cube = raw.reshape(n, h, w)
    issue = meta["forecast_issue_time_utc"]
    for k, fr in enumerate(meta["frames"]):
        yield fr["utc"], np.array(cube[k], dtype="float32"), issue


def _better_source(old_issue: str | None, new_issue: str, slab_utc: str) -> bool:
    """True if new_issue is a better provenance for a slab at slab_utc than
    old_issue. 'Better' = the latest issue that is still <= the slab hour
    (shortest positive lead). If neither is <= the hour yet (both still future
    forecasts), the newer issue wins so later runs keep improving it."""
    if old_issue is None:
        return True
    o_ok, n_ok = old_issue <= slab_utc, new_issue <= slab_utc
    if o_ok and n_ok:
        return new_issue > old_issue          # both valid -> shorter lead
    if n_ok and not o_ok:
        return True                            # new is a real past forecast, old isn't
    if o_ok and not n_ok:
        return False
    return new_issue > old_issue               # both future -> keep the newer run


# --------------------------------------------------------------------------- #
# frame rendering — recompute the dst grid from meta (build_forecast is untouched)
# --------------------------------------------------------------------------- #
def _dst_grid(meta: dict):
    """Reproduce build_forecast.build()'s lng/lat destination grid from meta, so
    hframe_*.png lands exactly where frame_*.png does. The native grid is in
    grid_crs over bbox_wgs84; build() picks the pixel count on the longer ground
    axis and makes pixels ~square. We render straight in 4326 from the native
    slab, which is already effectively plate-carree for these small AOIs — close
    enough for a background frame and avoids needing the source UTM transform."""
    vg = meta["value_grid"]
    ny, nx = int(vg["height"]), int(vg["width"])
    l, b, r, t = vg["bounds_wgs84"]
    lat0 = math.radians((b + t) / 2)
    span_x = (r - l) * math.cos(lat0)
    span_y = (t - b)
    if span_x >= span_y:
        dw = max(nx, ny)
        dh = max(1, round(dw * span_y / span_x))
    else:
        dh = max(nx, ny)
        dw = max(1, round(dh * span_x / span_y))
    dst_transform = rasterio.transform.from_bounds(l, b, r, t, dw, dh)
    src_transform = from_origin(l, t, (r - l) / nx, (t - b) / ny)
    return (dh, dw), dst_transform, src_transform


def _render_frames(cube: np.ndarray, meta: dict, out_dir: Path) -> int:
    """cube: (H, ny, nx). Write hframe_000..H-1 into out_dir, fixed domain from
    meta.value_domain_c so the scale matches the forward frames."""
    (dh, dw), dst_transform, src_transform = _dst_grid(meta)
    lo, hi = (float(x) for x in meta["value_domain_c"])
    lut = _lut(RAMP)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for k in range(cube.shape[0]):
        src = np.ascontiguousarray(cube[k], dtype="float32")
        dst = np.full((dh, dw), np.nan, "float32")
        reproject(src, dst, src_transform=src_transform, src_crs=DST_CRS,
                  dst_transform=dst_transform, dst_crs=DST_CRS,
                  resampling=Resampling.bilinear,
                  src_nodata=np.nan, dst_nodata=np.nan)
        Image.fromarray(colorize(dst, lo, hi, lut), "RGBA").save(
            out_dir / f"hframe_{k:03d}.png", optimize=True)
    return cube.shape[0]


# --------------------------------------------------------------------------- #
# entry point                                                                 #
# --------------------------------------------------------------------------- #
def update_hindcast(city_id: str, cfg: dict | None = None,
                    data_dir: Path = DATA, now: datetime | None = None,
                    window_hours: int = WINDOW_HOURS) -> dict | None:
    """Merge the fresh values.bin into the rolling archive and (re)write
    hindcast.bin / hindcast.json / hindcast_frames/. Returns the header dict, or
    None if there is nothing to archive yet."""
    latest = data_dir / city_id / "latest"
    previous = data_dir / city_id / "previous"
    meta_p = latest / "meta.json"
    values_p = latest / "values.bin"
    if not (meta_p.is_file() and values_p.is_file()):
        print(f"  hindcast: no fresh build at {latest} — skipped")
        return None

    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = (now - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    archive = _load_archive(latest, previous)

    # upsert every fresh slab
    for utc, slab, issue in _fresh_slabs(meta, values_p):
        cur = archive.get(utc)
        if cur is None or _better_source(cur["source_issue"], issue, utc):
            archive[utc] = {"data": slab, "source_issue": issue}

    # keep only [now-48h, now]: drop older than the cutoff and anything still in
    # the future (a slab is "past" only once wall-clock has passed it)
    kept = {u: v for u, v in archive.items() if cutoff <= u <= now_iso}
    if not kept:
        print(f"  hindcast: no slabs in [{cutoff}, {now_iso}] yet — archive empty")
        # still write an empty header so the frontend flag is deterministic
        _write(latest, meta, [], np.empty((0, 0, 0), "float32"))
        return None

    hours_sorted = sorted(kept)
    vg = meta["value_grid"]
    ny, nx = int(vg["height"]), int(vg["width"])
    cube = np.stack([kept[u]["data"] for u in hours_sorted]).astype("<f4")
    assert cube.shape == (len(hours_sorted), ny, nx), cube.shape

    tz = meta.get("timezone", "UTC")
    zi = ZoneInfo(tz)
    hour_meta = []
    for u in hours_sorted:
        dt = datetime.strptime(u, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        hour_meta.append({
            "utc": u,
            "local": dt.astimezone(zi).strftime("%Y-%m-%d %H:%M"),
            "hour_local": dt.astimezone(zi).hour,
            "source_issue": kept[u]["source_issue"],
        })

    hdr = _write(latest, meta, hour_meta, cube)
    n_frames = _render_frames(cube, meta, latest / "hindcast_frames")
    print(f"  hindcast: {len(hours_sorted)} h  [{hours_sorted[0]} .. {hours_sorted[-1]}]"
          f"  + {n_frames} frames")
    return hdr


def _write(latest: Path, meta: dict, hour_meta: list[dict], cube: np.ndarray) -> dict:
    vg = meta["value_grid"]
    (latest / "hindcast.bin").write_bytes(np.ascontiguousarray(cube).tobytes())
    hdr = {
        "file": "hindcast.bin",
        "width": int(vg["width"]),
        "height": int(vg["height"]),
        "n_hours": len(hour_meta),
        "index": "hour",
        "dtype": "float32",
        "bounds_wgs84": vg["bounds_wgs84"],
        "window_hours": WINDOW_HOURS,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hour_meta,
    }
    (latest / "hindcast.json").write_text(json.dumps(hdr, indent=2), encoding="utf-8")
    return hdr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True)
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--now", help="override 'now' (ISO-8601 UTC) for testing")
    ap.add_argument("--hours", type=int, default=WINDOW_HOURS)
    a = ap.parse_args()
    now = None
    if a.now:
        now = datetime.fromisoformat(a.now.replace("Z", "+00:00"))
    update_hindcast(a.city, None, a.data_dir, now, a.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
