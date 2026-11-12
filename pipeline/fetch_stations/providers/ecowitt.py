"""Ecowitt provider — personal weather stations via the Ecowitt cloud API v3
(https://api.ecowitt.net/api/v3/device/history). 5-minute cadence.

Ported from ML-UrbanHeat/workflow/run_fetch_ecowitt.py (same request shape:
station-local start/end, `cycle_type=5min`, at most 1 day per call), but
trimmed to what the Live Forecast needs: a 48 h window, outdoor sensors only,
nothing archived.

Nothing secret lives in the repo. Configuration comes from the environment:

    ECOWITT_APPLICATION_KEY, ECOWITT_API_KEY   account keys (GitHub secrets)
    ECOWITT_STATIONS   JSON list, one object per station:
        {"slug": "ricc-roof", "label": "RICC roof", "mac": "AA:BB:…",
         "lat": 53.348, "lon": -6.2004, "placement": "outdoor",
         "tz": "Europe/Dublin", "key_suffix": ""}
      `placement` defaults to outdoor; indoor stations are ignored (an indoor
      reading is no comparison for an outdoor 2 m forecast). `key_suffix`
      (optional) picks ECOWITT_API_KEY_<suffix> etc. for a station on a
      different Ecowitt account.

Public surface (used by fetch_forecast_stations):

    fetch(aoi_rings, start, stop) -> {slug: [measurement, ...]}
    COORDS   : {slug: (lon, lat)}
    LABELS   : {slug: str}
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .vlinder import _in_aoi

HISTORY_URL = "https://api.ecowitt.net/api/v3/device/history"
CHUNK = timedelta(days=1)            # 5min cycle: max 1 day per request
REQUEST_TIMEOUT = 30
POLITE_DELAY = 0.5
RATE_LIMIT_RETRIES = 4

COORDS: dict[str, tuple[float, float]] = {}
LABELS: dict[str, str] = {}


def _stations() -> list[dict]:
    raw = os.environ.get("ECOWITT_STATIONS", "").strip()
    if not raw:
        raise NotImplementedError("ECOWITT_STATIONS not set - no Ecowitt stations configured")
    return json.loads(raw)


def _creds(st: dict) -> dict:
    sfx = f"_{st['key_suffix']}" if st.get("key_suffix") else ""
    app = os.environ.get(f"ECOWITT_APPLICATION_KEY{sfx}", "")
    api = os.environ.get(f"ECOWITT_API_KEY{sfx}", "")
    if not (app and api and st.get("mac")):
        raise RuntimeError("missing application/api key or mac")
    return {"application_key": app, "api_key": api, "mac": st["mac"]}


def _get(params: dict) -> dict:
    url = HISTORY_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "dashboard-station-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _chunk(creds: dict, t0: datetime, t1: datetime, tz: str, placement: str) -> list[dict]:
    fmt = "%Y-%m-%d %H:%M:%S"
    z = ZoneInfo(tz)
    params = {**creds,
              "start_date": t0.astimezone(z).strftime(fmt),
              "end_date": t1.astimezone(z).strftime(fmt),
              "cycle_type": "5min",
              "call_back": f"{placement}.temperature,{placement}.humidity",
              "temp_unitid": "1"}                         # degC
    delay = 20.0
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        body = _get(params)
        if body.get("code") == 0:
            break
        msg = str(body.get("msg"))
        if "upper limit" in msg and attempt < RATE_LIMIT_RETRIES:
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"Ecowitt API error [{body.get('code')}]: {msg}")

    group = (body.get("data") or {}).get(placement) or {}

    def series(name):
        lst = (group.get(name) or {}).get("list") or {}
        out = {}
        for k, v in lst.items():
            try:
                out[int(k)] = float(v)
            except (TypeError, ValueError):
                pass
        return out

    temp, rh = series("temperature"), series("humidity")
    rows = []
    for ts in sorted(temp):
        rows.append({
            "time": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "temp": temp[ts], "humidity": rh.get(ts),
            "pressure": None, "windSpeed": None, "windGust": None,
            "windDirection": None, "rainIntensity": None, "rainVolume": None,
            "wbgt": None, "status": None,
        })
    return rows


def fetch(aoi_rings, start: datetime, stop: datetime) -> dict[str, list[dict]]:
    COORDS.clear()
    LABELS.clear()
    stations = [s for s in _stations()
                if (s.get("placement") or "outdoor").lower() == "outdoor"]
    inside = [s for s in stations if _in_aoi(float(s["lon"]), float(s["lat"]), aoi_rings)]
    print(f"    ecowitt: {len(inside)} of {len(stations)} outdoor stations inside AOI")

    out: dict[str, list[dict]] = {}
    for i, st in enumerate(inside, 1):
        slug = st["slug"]
        COORDS[slug] = (float(st["lon"]), float(st["lat"]))
        LABELS[slug] = st.get("label") or slug
        rows: list[dict] = []
        try:
            creds = _creds(st)
            t = start
            while t < stop:
                t_next = min(t + CHUNK, stop)
                rows += _chunk(creds, t, t_next, st.get("tz") or "UTC",
                               (st.get("placement") or "outdoor").lower())
                t = t_next
                time.sleep(POLITE_DELAY)
        except Exception as err:                       # one bad station never sinks the rest
            print(f"    [{i}/{len(inside)}] {slug}: {err} - skipped", file=sys.stderr)
            out[slug] = []
            continue
        dedup = {r["time"]: r for r in rows if start.strftime("%Y-%m-%dT%H:%M:%SZ") <= r["time"]}
        out[slug] = [dedup[k] for k in sorted(dedup)]
        print(f"    [{i}/{len(inside)}] {slug:<18} {len(out[slug]):>4} pts")
    return out
