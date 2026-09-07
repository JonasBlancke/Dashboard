"""
Weather-station observations for a Live Forecast city, aligned to the hindcast.

For the past 48 h the tab overlays real station readings so a click compares the
forecast GRID against MEASURED temperature. This script pulls those readings for
the stations inside the city's AOI polygon (the same aoi.geojson build_forecast
writes) and drops them next to the run.

Provider is chosen per forecast-city from `fetch_stations.providers.PROVIDERS`.
Only Ghent (VLINDER / MOCCA) is wired; the rest are stubs that make this a no-op.

Outputs, all in `data/forecast/<city>/latest/` (committed with the run):

    stations.geojson          Point per in-AOI station; properties: slug, name,
                              provider, n_points_5min, n_points_hourly,
                              first_time, last_time
    stations/<slug>.5min.csv  native cadence
    stations/<slug>.hourly.csv one row per UTC hour (mean of that hour's samples;
                              status = worst-case) — the direct join to the grid

Never raises into the caller: API down / stub city / no stations -> an empty
stations.geojson and exit 0. Called from run_city_forecast.py after build_web().

    python pipeline/fetch_forecast_stations.py --city ghent [--hours 48] [--now ISO]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "forecast"
sys.path.insert(0, str(Path(__file__).resolve().parent / "fetch_stations"))
from providers import PROVIDERS  # noqa: E402

WINDOW_HOURS = 48
CSV_FIELDS = ["time", "temp", "humidity", "pressure", "windSpeed", "windGust",
              "windDirection", "rainIntensity", "rainVolume", "wbgt", "status"]
_STATUS_RANK = {"Ok": 0, "ok": 0, None: 1, "": 1, "Offline": 2, "offline": 2}


# --------------------------------------------------------------------------- #
# AOI                                                                         #
# --------------------------------------------------------------------------- #
def _aoi_rings(aoi_path: Path):
    gj = json.loads(aoi_path.read_text(encoding="utf-8"))
    if gj.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in gj["features"] if f.get("geometry")]
    elif gj.get("type") == "Feature":
        geoms = [gj["geometry"]]
    else:
        geoms = [gj]
    rings = []
    for g in geoms:
        t, coords = g.get("type"), g.get("coordinates", [])
        if t == "Polygon" and coords:
            rings.append(coords[0])
        elif t == "MultiPolygon":
            rings.extend(poly[0] for poly in coords if poly)
    if not rings:
        raise ValueError(f"no polygon rings in {aoi_path}")
    return rings


# --------------------------------------------------------------------------- #
# hourly aggregation                                                          #
# --------------------------------------------------------------------------- #
def _hour_key(iso: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hourly(rows: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        hk = _hour_key(r.get("time", ""))
        if hk:
            buckets.setdefault(hk, []).append(r)
    out = []
    for hk in sorted(buckets):
        grp = buckets[hk]
        agg = {"time": hk}
        for f in CSV_FIELDS[1:-1]:                       # numeric fields
            vals = [x[f] for x in grp if isinstance(x.get(f), (int, float))]
            agg[f] = round(sum(vals) / len(vals), 3) if vals else None
        worst = max((_STATUS_RANK.get(x.get("status"), 1) for x in grp), default=1)
        agg["status"] = {0: "Ok", 1: None, 2: "Offline"}[worst]
        out.append(agg)
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CSV_FIELDS})


# --------------------------------------------------------------------------- #
# entry point                                                                 #
# --------------------------------------------------------------------------- #
def fetch_stations(city_id: str, cfg: dict | None = None,
                   data_dir: Path = DATA, now: datetime | None = None,
                   window_hours: int = WINDOW_HOURS) -> dict | None:
    latest = data_dir / city_id / "latest"
    out_geojson = latest / "stations.geojson"
    series_dir = latest / "stations"

    def _write_empty(reason: str):
        series_dir.mkdir(parents=True, exist_ok=True)
        out_geojson.write_text(json.dumps(
            {"type": "FeatureCollection", "features": [],
             "properties": {"city": city_id, "empty_reason": reason}}, indent=2),
            encoding="utf-8")
        print(f"  stations: {reason} — wrote empty stations.geojson")

    if cfg is not None and not cfg.get("has_local_observations"):
        _write_empty("city has has_local_observations: false")
        return None

    provider = PROVIDERS.get(city_id)
    if provider is None:
        _write_empty(f"no provider registered for {city_id!r}")
        return None

    aoi_path = latest / "aoi.geojson"
    if not aoi_path.is_file():
        _write_empty(f"no aoi.geojson at {aoi_path}")
        return None

    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = now - timedelta(hours=window_hours)

    try:
        rings = _aoi_rings(aoi_path)
        series = provider(rings, start, now)
    except NotImplementedError as e:
        _write_empty(str(e))
        return None
    except Exception as e:                                # network / parse
        _write_empty(f"provider failed: {e}")
        return None

    if not series:
        _write_empty("no stations inside AOI")
        return None

    # provider-supplied coords/labels (vlinder fills these); fall back to none
    prov_mod = sys.modules.get(getattr(provider, "__module__", ""))
    coords = getattr(prov_mod, "COORDS", {}) if prov_mod else {}
    labels = getattr(prov_mod, "LABELS", {}) if prov_mod else {}

    series_dir.mkdir(parents=True, exist_ok=True)
    for stale in series_dir.glob("*.csv"):               # drop last run's stations
        stale.unlink()

    features = []
    for slug, rows in sorted(series.items()):
        rows = [r for r in rows if r.get("time")
                and isinstance(r.get("temp"), (int, float))]
        if not rows:
            # station is registered but reported nothing in the window
            # (offline / decommissioned) — leave it out entirely
            print(f"  stations: {slug} has no readings in the window — skipped")
            continue
        hourly = _hourly(rows)
        _write_csv(series_dir / f"{slug}.5min.csv", rows)
        _write_csv(series_dir / f"{slug}.hourly.csv", hourly)

        lonlat = coords.get(slug)
        if lonlat is None:
            print(f"  stations: no coords for {slug} — skipped from geojson",
                  file=sys.stderr)
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lonlat[0], lonlat[1]]},
            "properties": {
                "slug": slug,
                "name": labels.get(slug, slug),
                "provider": getattr(prov_mod, "__name__", "").split(".")[-1],
                "n_points_5min": len(rows),
                "n_points_hourly": len(hourly),
                "first_time": rows[0]["time"] if rows else None,
                "last_time": rows[-1]["time"] if rows else None,
            },
        })

    out_geojson.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "city": city_id,
            "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }, indent=2), encoding="utf-8")
    print(f"  stations: {len(features)} stations, "
          f"{sum(f['properties']['n_points_5min'] for f in features)} samples "
          f"-> {out_geojson.parent}")
    return {"n_stations": len(features)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True)
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--hours", type=int, default=WINDOW_HOURS)
    ap.add_argument("--now", help="override 'now' (ISO-8601 UTC) for testing")
    a = ap.parse_args()
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else None
    fetch_stations(a.city, None, a.data_dir, now, a.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
