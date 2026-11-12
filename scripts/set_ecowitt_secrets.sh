#!/usr/bin/env bash
# Push the Ecowitt (Dublin PWS) credentials + station list to this repo's
# GitHub Actions secrets, straight from ML-UrbanHeat's registry — values are
# piped into `gh secret set`, never echoed.
#
#   bash scripts/set_ecowitt_secrets.sh                 # default registry path
#   bash scripts/set_ecowitt_secrets.sh path/to/stations.csv
#   DRY_RUN=1 bash scripts/set_ecowitt_secrets.sh       # show the station list (no keys), set nothing
#
# DASHBOARD_EXCLUDE (comma-separated station_ids) keeps stations off the PUBLIC
# map while leaving them active in ML-UrbanHeat's validation fetch. Default:
# DUBLIN_APARTMENT (a private home). Set DASHBOARD_EXCLUDE= to include all.
#
# Sets: ECOWITT_APPLICATION_KEY, ECOWITT_API_KEY, ECOWITT_STATIONS (JSON, outdoor
# stations only, no keys). Read by pipeline/fetch_stations/providers/ecowitt.py.
# Assumes all stations share one Ecowitt account (true today); a station on a
# second account would need key_suffix + ECOWITT_*_<suffix> secrets.
set -euo pipefail

REG="${1:-${ML_URBANHEAT_DIR:-../ML-UrbanHeat}/cities/Dublin/raw_data/ecowitt/stations.csv}"
[ -f "$REG" ] || { echo "registry not found: $REG" >&2; exit 1; }
export DASHBOARD_EXCLUDE="${DASHBOARD_EXCLUDE-DUBLIN_APARTMENT}"

py() { python - "$REG" "$1" <<'EOF'
import csv, json, os, re, sys
excl = {s.strip() for s in os.environ.get("DASHBOARD_EXCLUDE", "").split(",") if s.strip()}
rows = [r for r in csv.DictReader(open(sys.argv[1], encoding="utf-8-sig"))
        if str(r.get("active", "1")).strip().lower() not in ("0", "false", "no")
        and r["station_id"].strip() not in excl]
what = sys.argv[2]
if what in ("application_key", "api_key"):
    vals = {r[what].strip() for r in rows if r[what].strip()}
    if len(vals) != 1:
        sys.exit(f"expected exactly one {what} across stations, found {len(vals)}")
    sys.stdout.write(vals.pop())
else:
    out = [{"slug": re.sub(r"[^a-z0-9]+", "-", r["station_id"].strip().lower()).strip("-"),
            "label": r["station_id"].strip(),
            "mac": r["mac"].strip(),
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "placement": "outdoor",
            "tz": r.get("tz", "").strip() or "Europe/Dublin"}
           for r in rows if r["placement"].strip().lower() == "outdoor"]
    if what == "preview":
        for s in out:
            print(f"  {s['slug']:<20} {s['label']:<20} {s['lat']:.4f}, {s['lon']:.4f}")
    else:
        sys.stdout.write(json.dumps(out))
EOF
}

echo "outdoor stations in $REG (excluding: ${DASHBOARD_EXCLUDE:-none}):"
py preview
[ -n "${DRY_RUN:-}" ] && { echo "(dry run — nothing set)"; exit 0; }

py application_key | gh secret set ECOWITT_APPLICATION_KEY
py api_key         | gh secret set ECOWITT_API_KEY
py stations        | gh secret set ECOWITT_STATIONS
echo "done — secrets set on $(gh repo view --json nameWithOwner -q .nameWithOwner)"
