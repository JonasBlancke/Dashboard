r"""
EPW drive  ->  data/epw/  catalogue + per-file infographics   (EPW Files tab).

Source root (SHARED folders only):
    G:\Gedeelde drives\B-Kode\Projects\MinorityReport\data\EPW\<City>\<TYPE>\<SCENARIO>_SHARED\*.epw
    $EPW_ROOT overrides the drive path (it is Google-Drive-streamed and slow).

Cities are fixed to the three with published SHARED output:
    Dublin, Patras, Wellington.

Six EPW variables are extracted per file (column index, 0-based):
    tdb  6   dry-bulb temperature      degC
    tdp  7   dew-point temperature     degC
    rh   8   relative humidity         %
    atmp 9   atmospheric pressure      Pa      (kept for completeness, not shown)
    ghi  13  global horizontal irrad.  Wh/m2
    dni  14  direct normal irradiance  Wh/m2
    wspd 21  wind speed                m/s

Per file, all derived from those series (no matplotlib, no tiles):
  · one annual heatmap PNG per variable   365x24 px, data/epw/heat/<hash>_<var>.png
  · monthly median per variable           12 values
  · summary stats per variable            min / p1 / median / p99 / max

Catalogue tree  (data/epw/catalog.json):
    cities[]
      historical: { AMY: {files:[{year, variants[]}]},
                    TMY: {period, variants[]},
                    DSY: {period, variants[]} }
      future:     ssps[] -> periods[] -> yearTypes:
                    { TMY:{variants[]}, DSY:{variants[]},
                      XTMY:{metrics:[{id,label,returnPeriod,window,info,variants[]}]},
                      XDSY:{...} }
    a "variant" = { id:"base"|"LCZ3", label, path (drive-relative),
                    stats:{var->{min,p1,median,p99,max}},
                    monthly:{var->[12]}, heat:{var->"heat/<h>_<var>.png"} }

The .epw bytes are NOT copied — backend/app.py streams them from EPW_ROOT via
`path` on demand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "epw"
HEAT = OUT / "heat"
EPW_ROOT = Path(os.environ.get(
    "EPW_ROOT",
    r"G:\Gedeelde drives\B-Kode\Projects\MinorityReport\data\EPW",
))

CITIES = {
    "Dublin":     {"center": [-6.2265, 53.3442]},
    "Patras":     {"center": [21.7346, 38.2466]},
    "Wellington": {"center": [174.7762, -41.2865]},
}

# variable id -> (epw column, label, unit, matters-as-heatmap?, ramp key)
VARS = {
    "tdb":  {"col": 6,  "label": "Dry-bulb temp",     "unit": "°C",     "ramp": "temp"},
    "tdp":  {"col": 7,  "label": "Dew-point temp",    "unit": "°C",     "ramp": "temp"},
    "rh":   {"col": 8,  "label": "Relative humidity", "unit": "%",      "ramp": "humid"},
    "ghi":  {"col": 13, "label": "Global horiz. irr.", "unit": "Wh/m²", "ramp": "solar"},
    "dni":  {"col": 14, "label": "Direct normal irr.", "unit": "Wh/m²", "ramp": "solar"},
    "wspd": {"col": 21, "label": "Wind speed",        "unit": "m/s",    "ramp": "wind"},
}
# EPW per-field missing sentinels (>= => missing)
MISSING = {"tdb": 99.0, "tdp": 99.0, "rh": 999.0, "ghi": 9999.0, "dni": 9999.0, "wspd": 999.0}

RAMPS = {
    "temp": [[8, 48, 107], [33, 102, 172], [67, 147, 195], [146, 197, 222],
             [209, 229, 240], [253, 219, 199], [244, 165, 130], [214, 96, 77],
             [178, 24, 43], [140, 10, 30], [103, 0, 31], [74, 0, 20]],
    "humid": [[26, 24, 61], [37, 58, 110], [30, 100, 145], [30, 143, 158],
              [58, 178, 148], [125, 201, 128], [200, 219, 110], [255, 221, 110],
              [255, 180, 95], [232, 130, 90]],
    "solar": [[8, 12, 30], [30, 22, 74], [78, 27, 110], [131, 34, 116],
              [182, 55, 101], [222, 90, 79], [246, 137, 66], [252, 191, 79],
              [252, 233, 122], [255, 255, 209]],
    "wind": [[10, 25, 40], [24, 68, 92], [38, 116, 130], [72, 161, 148],
             [140, 196, 130], [206, 219, 110], [244, 179, 90], [230, 120, 78],
             [180, 55, 60]],
}


def ramp_rgb(norm: np.ndarray, ramp: list) -> np.ndarray:
    arr = np.asarray(ramp, dtype=np.float32)
    x = np.clip(norm, 0, 1) * (len(arr) - 1)
    lo = np.floor(x).astype(int)
    hi = np.minimum(lo + 1, len(arr) - 1)
    f = (x - lo)[..., None]
    return (arr[lo] * (1 - f) + arr[hi] * f).astype(np.uint8)


# ---- EPW parsing --------------------------------------------------------
_LOC = re.compile(r"^LOCATION,", re.I)


def read_epw(path: Path):
    """Return (dict var->float32[8760], meta) or (None, {}) on failure."""
    try:
        with open(path, "r", encoding="latin-1") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None, {}
    if not lines or not _LOC.match(lines[0]):
        return None, {}
    loc = lines[0].split(",")
    meta = {}
    if len(loc) >= 10:
        meta = {"station": loc[1], "country": loc[3],
                "lat": _f(loc[6]), "lon": _f(loc[7]),
                "tz": _f(loc[8]), "elev": _f(loc[9])}
    rows = lines[8:8 + 8760]
    ncol = max(v["col"] for v in VARS.values()) + 1
    raw = np.full((8760, ncol), np.nan, dtype=np.float32)
    for i, ln in enumerate(rows):
        parts = ln.split(",")
        if len(parts) < ncol:
            continue
        for j in range(ncol):
            raw[i, j] = _f(parts[j], np.nan)
    out = {}
    for vid, spec in VARS.items():
        v = raw[:, spec["col"]].copy()
        v[v >= MISSING[vid]] = np.nan
        out[vid] = v
    if np.isnan(out["tdb"]).all():
        return None, meta
    return out, meta


def _f(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


# ---- per-file products ------------------------------------------------
_MONTH_LEN = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
_MONTH_OF_HOUR = np.repeat(np.arange(12), [d * 24 for d in _MONTH_LEN])


def write_heat(series: np.ndarray, vmin: float, vmax: float, ramp: list,
               rel_id: str, var: str) -> str:
    # PNG is 365 wide (day of year) x 24 tall (hour of day, row 0 = 00:00).
    grid = series[: 365 * 24].reshape(365, 24).T          # -> (24, 365)
    norm = (grid - vmin) / max(vmax - vmin, 1e-6)
    rgb = ramp_rgb(norm, ramp)
    a = np.where(np.isnan(grid), 0, 255).astype(np.uint8)
    name = hashlib.sha1(rel_id.encode()).hexdigest()[:16] + f"_{var}.png"
    Image.fromarray(np.dstack([rgb, a]), "RGBA").save(HEAT / name, format="PNG", optimize=True)
    return "heat/" + name


def monthly_band(series: np.ndarray):
    """Per calendar month: p10 / p50 / p90 of the hourly values."""
    out = []
    s = series[: 365 * 24]
    for m in range(12):
        col = s[_MONTH_OF_HOUR == m]
        col = col[~np.isnan(col)]
        if col.size == 0:
            out.append(None)
            continue
        out.append({
            "p10": round(float(np.percentile(col, 10)), 2),
            "p50": round(float(np.median(col)), 2),
            "p90": round(float(np.percentile(col, 90)), 2),
        })
    return out


def stat5(series: np.ndarray):
    v = series[~np.isnan(series)]
    if v.size == 0:
        return None
    return {
        "min": round(float(v.min()), 2),
        "p1": round(float(np.percentile(v, 1)), 2),
        "median": round(float(np.median(v)), 2),
        "p99": round(float(np.percentile(v, 99)), 2),
        "max": round(float(v.max()), 2),
    }


# ---- filename semantics ---------------------------------------------
_LCZ = re.compile(r"_LCZ(\w+)_urban", re.I)
_YEAR = re.compile(r"_(\d{4})\.epw$", re.I)
_XMY = re.compile(
    r"_(TX\d+d|EHF|HWMId|Hotspell)_(?:CMIP6_)?(\d+)yr_(\d{4}-\d\d-\d\d)_(\d{4}-\d\d-\d\d)", re.I)

METRIC_LABEL = {"TX7D": "TX7d — hottest 7-day spell",
                "EHF": "EHF — Excess Heat Factor"}
METRIC_INFO = {
    "TX7D": "TX7d — hottest 7-consecutive-day window: the future-period spell whose "
            "7-day mean daily-maximum temperature matches the chosen return period.",
    "EHF":  "EHF — Excess Heat Factor: a heat-stress index combining a 3-day "
            "temperature anomaly against the local 95th percentile with a short-term "
            "acclimatisation term. The spell with the chosen return period is spliced in.",
}


def variant_of(name: str):
    m = _LCZ.search(name)
    return (f"LCZ{m.group(1)}", f"LCZ{m.group(1)} urban") if m else ("base", "base (rural)")


def scen_bits(dirname: str):
    ssp = re.search(r"SSP(\d)(\d)\d?", dirname, re.I)
    yr = re.search(r"_(\d{4})_(\d{4})_", dirname)
    amoc = "AMOC" in dirname.upper()
    ssp_id = f"ssp{ssp.group(1)}{ssp.group(2)}5" if ssp else "ssp?"
    ssp_lbl = f"SSP{ssp.group(1)}-{ssp.group(2)}.5" if ssp else dirname
    per_id = f"{yr.group(1)}_{yr.group(2)}" if yr else "?"
    per_lbl = f"{yr.group(1)}\u2013{yr.group(2)}" if yr else dirname
    if amoc:
        ssp_id += "_amoc"; ssp_lbl += " (AMOC)"
    return ssp_id, ssp_lbl, per_id, per_lbl


def collect(paths):
    """EPW paths ordered base-first, then LCZ* by name."""
    return sorted(paths, key=lambda p: (variant_of(p.name)[0] != "base", p.name))


def main():
    if not EPW_ROOT.exists():
        raise SystemExit(f"EPW_ROOT not found: {EPW_ROOT}  (set $EPW_ROOT)")
    OUT.mkdir(parents=True, exist_ok=True)
    HEAT.mkdir(parents=True, exist_ok=True)
    for old in HEAT.glob("*.png"):
        old.unlink()

    cities_out = []
    for city, cfg in CITIES.items():
        cdir = EPW_ROOT / city
        if not cdir.is_dir():
            print(f"skip city (no dir): {city}")
            continue
        print(f"== {city} ==")

        # ---------- pass 1: gather series, per-variable colour range -----
        # (min 1st pct .. max 99th pct across every SHARED file of this city,
        #  so heatmaps of all variants/scenarios share one scale per variable)
        acc = {v: [] for v in VARS}
        file_index = []           # (typ, sdir_name, path)
        for typ in ["TMY", "DSY", "XTMY", "XDSY", "EPW-historical"]:
            tdir = cdir / typ
            if not tdir.is_dir():
                continue
            shared = sorted(tdir.glob("*_SHARED"))
            # EPW-historical SHARED dirs are byte-identical across scenarios —
            # only read the first (saves ~200 slow drive reads per city).
            if typ == "EPW-historical":
                shared = shared[:1]
            for sdir in shared:
                for epw in sorted(sdir.glob("*.epw")):
                    file_index.append((typ, sdir.name, epw))

        # read every file once, stash series for pass 2
        cache = {}
        for typ, sname, epw in file_index:
            series, meta = read_epw(epw)
            if series is None:
                continue
            cache[str(epw)] = (series, meta)
            for v, s in series.items():
                good = s[~np.isnan(s)]
                if good.size:
                    acc[v].append(good)
        vmm = {}
        for v, chunks in acc.items():
            if chunks:
                allv = np.concatenate(chunks)
                vmm[v] = [float(np.floor(np.percentile(allv, 1))),
                          float(np.ceil(np.percentile(allv, 99)))]
            else:
                vmm[v] = [0.0, 1.0]

        # ---------- pass 2: build the tree, attaching products ----------
        def variant_from_cache(epw: Path):
            key = str(epw)
            if key not in cache:
                return None
            series, meta = cache[key]
            rel = str(epw.relative_to(EPW_ROOT)).replace("\\", "/")
            vid, vlabel = variant_of(epw.name)
            stats, monthly, heat = {}, {}, {}
            for var, s in series.items():
                st = stat5(s)
                if st is None:
                    continue
                stats[var] = st
                monthly[var] = monthly_band(s)
                heat[var] = write_heat(s, vmm[var][0], vmm[var][1],
                                       RAMPS[VARS[var]["ramp"]], rel, var)
            return {"id": vid, "label": vlabel, "path": rel, "name": epw.name,
                    "meta": meta, "stats": stats, "monthly": monthly, "heat": heat}

        def variants_in(dirpath: Path, pred):
            out = []
            for epw in collect([p for p in dirpath.glob("*.epw") if pred(p.name)]):
                v = variant_from_cache(epw)
                if v:
                    out.append(v)
            return out

        historical = {}
        future_ssp = {}   # ssp_id -> {label, periods: {per_id -> {...}}}

        for typ in ["TMY", "DSY", "XTMY", "XDSY"]:
            tdir = cdir / typ
            if not tdir.is_dir():
                continue
            for sdir in sorted(tdir.glob("*_SHARED")):
                ssp_id, ssp_lbl, per_id, per_lbl = scen_bits(sdir.name)

                # historical TMY/DSY: identical across scenario folders -> take once
                if typ in ("TMY", "DSY") and typ not in historical:
                    hv = variants_in(sdir, lambda n: "historical" in n.lower())
                    if hv:
                        pm = re.search(r"_(\d{4})_(\d{4})", hv[0]["name"])
                        historical[typ] = {
                            "id": typ,
                            "label": {"TMY": "TMY — Typical Meteorological Year",
                                      "DSY": "DSY — Design Summer Year"}[typ],
                            "period": f"{pm.group(1)}\u2013{pm.group(2)}" if pm else None,
                            "variants": hv,
                        }

                # future leaves
                node = future_ssp.setdefault(ssp_id, {"id": ssp_id, "label": ssp_lbl, "periods": {}})
                per = node["periods"].setdefault(per_id, {"id": per_id, "label": per_lbl, "yearTypes": {}})

                if typ in ("TMY", "DSY"):
                    fv = variants_in(sdir, lambda n: "future" in n.lower())
                    if fv:
                        per["yearTypes"][typ] = {
                            "id": typ,
                            "label": {"TMY": "TMY — Typical Meteorological Year",
                                      "DSY": "DSY — Design Summer Year"}[typ],
                            "variants": fv,
                        }
                else:  # XTMY / XDSY -> group by metric
                    metrics = {}
                    for epw in sorted(sdir.glob("*.epw")):
                        mm = _XMY.search(epw.name)
                        if not mm:
                            continue
                        met = mm.group(1).upper()
                        rp = int(mm.group(2))
                        w0, w1 = mm.group(3), mm.group(4)
                        v = variant_from_cache(epw)
                        if not v:
                            continue
                        entry = metrics.setdefault(met, {
                            "id": met, "label": METRIC_LABEL.get(met, met),
                            "returnPeriod": rp,
                            "returnPeriodsAvailable": [rp],
                            "window": [w0, w1],
                            "info": METRIC_INFO.get(met, ""),
                            "variants": [],
                        })
                        entry["variants"].append(v)
                    for e in metrics.values():
                        e["variants"] = collect_variants(e["variants"])
                    if metrics:
                        per["yearTypes"][typ] = {
                            "id": typ,
                            "label": {"XTMY": "XTMY — Extreme TMY (heatwave-spliced)",
                                      "XDSY": "XDSY — Extreme DSY (heatwave-spliced)"}[typ],
                            "metrics": list(metrics.values()),
                        }

        # AMY: EPW-historical, scenario-independent (dirs are byte-identical)
        eh = cdir / "EPW-historical"
        if eh.is_dir():
            shared = sorted(eh.glob("*_SHARED"))
            if shared:
                years = []
                for epw in sorted(shared[0].glob("*.epw")):
                    ym = _YEAR.search(epw.name)
                    if not ym:
                        continue
                    v = variant_from_cache(epw)
                    if v:
                        years.append({"year": int(ym.group(1)), "variants": [v]})
                if years:
                    historical["AMY"] = {
                        "id": "AMY",
                        "label": "AMY — Actual Meteorological Year",
                        "years": years,
                    }

        if not historical and not future_ssp:
            continue

        # order the future tree deterministically
        future = []
        for ssp_id in sorted(future_ssp):
            node = future_ssp[ssp_id]
            node["periods"] = [node["periods"][p] for p in sorted(node["periods"])]
            for per in node["periods"]:
                per["yearTypes"] = [per["yearTypes"][k]
                                    for k in ["TMY", "DSY", "XTMY", "XDSY"]
                                    if k in per["yearTypes"]]
            future.append(node)

        cities_out.append({
            "id": city, "label": city, "center": cfg["center"],
            "varRange": vmm,
            "historical": historical,
            "future": future,
        })
        print(f"   done: {len(future)} SSPs, historical keys {list(historical)}")

    catalog = {
        "generated_from": str(EPW_ROOT),
        "vars": {k: {"label": v["label"], "unit": v["unit"]} for k, v in VARS.items()},
        "ramps": RAMPS,
        "varRampKey": {k: v["ramp"] for k, v in VARS.items()},
        "cities": cities_out,
    }
    (OUT / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    npng = len(list(HEAT.glob("*.png")))
    print(f"\nwrote {OUT/'catalog.json'}  ·  {len(cities_out)} cities · {npng} heatmap PNGs")


def collect_variants(vlist):
    return sorted(vlist, key=lambda v: (v["id"] != "base", v["id"]))


if __name__ == "__main__":
    main()
