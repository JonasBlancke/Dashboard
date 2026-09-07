"""VLINDER / MOCCA provider — public API at https://mooncake.ugent.be/api
(source: github.com/bmesuere/vlinder). No key. 5-minute cadence, 48 h+ history.

The API keys stations by opaque ids; `GET /stations` already carries name +
coordinates + city, so we work straight off that and never need the dashboard's
data.csv slug map.

Public surface (used by fetch_forecast_stations):

    fetch(aoi_rings, start, stop) -> {slug: [measurement, ...]}
    COORDS   : {slug: (lon, lat)}   populated as a side effect of fetch()
    LABELS   : {slug: str}
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

API_BASE = "https://mooncake.ugent.be/api"
USER_AGENT = "dashboard-vlinder-fetch/1.1 (+urban-climate pipeline)"
REQUEST_TIMEOUT = 60
RETRIES = 3
RETRY_BACKOFF = 3.0
POLITE_DELAY = 0.4          # server caches 60 s; be gentle between stations

COORDS: dict[str, tuple[float, float]] = {}
LABELS: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #
def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
            last_err = err
            if attempt < RETRIES:
                wait = RETRY_BACKOFF * attempt
                print(f"    ! {err} - retry {attempt}/{RETRIES - 1} in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"GET failed after {RETRIES} attempts: {url}") from last_err


def _station_list() -> list[dict]:
    data = _get_json(f"{API_BASE}/stations")
    if not isinstance(data, list):
        raise RuntimeError("unexpected /stations payload")
    return data


def _series(station_id: str, start: datetime, stop: datetime) -> list[dict]:
    qs = urllib.parse.urlencode({"start": format_datetime(start),
                                 "end": format_datetime(stop)})
    data = _get_json(f"{API_BASE}/measurements/{urllib.parse.quote(station_id)}?{qs}")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"API error for {station_id}: {data['error']}")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected measurements payload for {station_id}")
    return data


# --------------------------------------------------------------------------- #
# geometry — ray-casting point-in-polygon, stdlib only                        #
# --------------------------------------------------------------------------- #
def _point_in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_aoi(lon: float, lat: float, rings) -> bool:
    return any(_point_in_ring(lon, lat, r) for r in rings)


# --------------------------------------------------------------------------- #
# normalisation                                                               #
# --------------------------------------------------------------------------- #
def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _parse_time(s: str) -> str:
    """API time -> ISO-8601 UTC. Input like 'Tue, 01 Sep 2026 08:15:00 UTC'."""
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return s


def normalise_measurement(m: dict) -> dict:
    return {
        "time": _parse_time(m.get("time", "")),
        "temp": _num(m.get("temp")),
        "humidity": _num(m.get("humidity")),
        "pressure": _num(m.get("pressure")),
        "windSpeed": _num(m.get("windSpeed")),
        "windGust": _num(m.get("windGust")),
        "windDirection": _num(m.get("windDirection")),
        "rainIntensity": _num(m.get("rainIntensity")),
        "rainVolume": _num(m.get("rainVolume")),
        "wbgt": _num(m.get("wbgt")),
        "status": m.get("status"),
    }


def _slug(st: dict) -> str:
    name = (st.get("name") or "").strip()
    return name if name else st["id"]


def _coords(st: dict):
    c = st.get("coordinates") or {}
    lat, lon = c.get("latitude"), c.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lon), float(lat)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# provider entry point                                                        #
# --------------------------------------------------------------------------- #
def fetch(aoi_rings, start: datetime, stop: datetime) -> dict[str, list[dict]]:
    COORDS.clear()
    LABELS.clear()
    stations = _station_list()
    inside = []
    for st in stations:
        c = _coords(st)
        if c and _in_aoi(c[0], c[1], aoi_rings):
            inside.append(st)
    print(f"    vlinder: {len(inside)} of {len(stations)} stations inside AOI")

    out: dict[str, list[dict]] = {}
    for i, st in enumerate(inside, 1):
        slug = _slug(st)
        lon, lat = _coords(st)
        COORDS[slug] = (lon, lat)
        LABELS[slug] = st.get("given_name") or st.get("name") or slug
        try:
            raw = _series(st["id"], start, stop)
        except RuntimeError as err:
            print(f"    [{i}/{len(inside)}] {slug}: {err} — skipped", file=sys.stderr)
            out[slug] = []
            continue
        rows = sorted((normalise_measurement(m) for m in raw), key=lambda r: r["time"])
        out[slug] = rows
        print(f"    [{i}/{len(inside)}] {slug:<16} {len(rows):>4} pts")
        time.sleep(POLITE_DELAY)
    return out
