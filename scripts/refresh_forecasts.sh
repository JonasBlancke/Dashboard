#!/usr/bin/env bash
# Recompute the Live Forecast for every enabled city in cities.forecast.yaml,
# locally, then commit + push so GitHub Pages redeploys.
#
# This is the "I added a city to the list, run everything" button. It does the
# same work the daily-forecast.yml GitHub Action does, but on your machine and
# for every city in one go.
#
#   bash scripts/refresh_forecasts.sh                # all enabled cities
#   bash scripts/refresh_forecasts.sh ghent patras   # only these
#   SKIP_MODEL=1 bash scripts/refresh_forecasts.sh    # reuse newest NetCDF, no model run
#   NO_PUSH=1    bash scripts/refresh_forecasts.sh    # build only, don't commit/push
#
# Needs: the ML-UrbanHeat checkout next to this repo (../ML-UrbanHeat) with, per
# city, its <spatial_sim>/spatial/ TIFs + raw_data/AOI.geojson (+ optional
# ZOI.geojson); the ML conda env active (rasterio, xgboost<3, pvlib, …); a
# cached merit_height_m in the registry per city.
#
# Env: ML_URBANHEAT_DIR (../ML-UrbanHeat)  SKIP_MODEL  NO_PUSH  DATE=YYYY-MM-DD

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export ML_URBANHEAT_DIR="${ML_URBANHEAT_DIR:-$ROOT/../ML-UrbanHeat}"

mapfile -t CITIES < <(python - "$@" <<'PY'
import sys, yaml
reg = yaml.safe_load(open("cities.forecast.yaml"))
want = set(sys.argv[1:])
for cid, c in reg["cities"].items():
    if c.get("enabled", True) and (not want or cid in want):
        print(cid)
PY
)
[ ${#CITIES[@]} -gt 0 ] || { echo "no matching enabled cities" >&2; exit 1; }
echo "cities: ${CITIES[*]}"

extra=(); [ -n "${SKIP_MODEL:-}" ] && extra+=(--skip-model)
[ -n "${DATE:-}" ] && extra+=(--date "$DATE")

changed=()
for cid in "${CITIES[@]}"; do
  echo "════════ $cid ════════"
  d="data/forecast/$cid"

  # rotate latest -> previous up front (a failed run leaves both = yesterday,
  # harmless; we only add to git if latest/ actually changed below)
  prev_issue="none"
  if [ -f "$d/latest/meta.json" ]; then
    prev_issue=$(python -c "import json;print(json.load(open('$d/latest/meta.json'))['forecast_issue_time_utc'])")
    rm -rf "$d/previous"; cp -r "$d/latest" "$d/previous"
  fi

  if ! python pipeline/run_city_forecast.py --city "$cid" "${extra[@]}"; then
    echo "  !! $cid failed — leaving its previous forecast in place" >&2
    [ -d "$d/previous" ] && { rm -rf "$d/latest"; mv "$d/previous" "$d/latest"; }
    continue
  fi

  new_issue=$(python -c "import json;print(json.load(open('$d/latest/meta.json'))['forecast_issue_time_utc'])")
  if [ "$new_issue" = "$prev_issue" ]; then
    echo "  issue time unchanged ($new_issue) — nothing new"
    [ -d "$d/previous" ] && rm -rf "$d/previous"   # don't churn git with an identical copy
  else
    echo "  new forecast: $new_issue"
    changed+=("$cid")
  fi
done

[ ${#changed[@]} -gt 0 ] || { echo "nothing changed — done."; exit 0; }
echo "════════ changed: ${changed[*]} ════════"

[ -n "${NO_PUSH:-}" ] && { echo "NO_PUSH set — not committing. Review with: python backend/app.py"; exit 0; }

for cid in "${changed[@]}"; do
  git add -f "data/forecast/$cid/latest" 2>/dev/null || true
  [ -d "data/forecast/$cid/previous" ] && git add -f "data/forecast/$cid/previous"
done
git add -f data/forecast/manifest.json
if git diff --cached --quiet; then echo "no staged changes"; exit 0; fi

git commit -m "chore(data): forecast refresh — ${changed[*]}"
git pull --rebase --autostash
git push
echo "pushed. deploy-site will publish to https://jonasblancke.github.io/Dashboard/ in ~2 min."
