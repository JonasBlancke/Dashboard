#!/usr/bin/env bash
# Stage the heavy V29 forecast artefacts as a GitHub Release on THIS repo, so
# daily-forecast.yml can pull them without any of it living in git.
#
# Iterates every `enabled` city in cities.forecast.yaml. Per city it uploads
#   spatial-<id>-V29_forecast.tar.gz   resampled TIFs + default_grid + ~2000 px
#                                      context rasters (buildings/trees/water)
#   aoi-<id>.geojson                   simplified AOI (clips the prediction grid)
#   zoi-<id>.geojson                   simplified ZOI, if present (red outline)
# plus, once, the shared model + config:
#   xgb-V29_forecast.joblib   config-V29_forecast.yaml
#
# Run locally (needs `gh` authed + an ML-UrbanHeat checkout). Re-run for a city
# whenever its V29 model / spatial TIFs / AOI change — it clobbers the assets.
#
#   bash scripts/stage_v29_assets.sh              # all enabled cities
#   bash scripts/stage_v29_assets.sh ghent patras # only these
#
# Env overrides:  ML_URBANHEAT_DIR (../ML-UrbanHeat)  REPO  RELEASE_TAG (v29-assets)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ML="${ML_URBANHEAT_DIR:-$ROOT/../ML-UrbanHeat}"
TAG="${RELEASE_TAG:-v29-assets}"
REGISTRY="$ROOT/cities.forecast.yaml"

REPO_ARG=()
[ -n "${REPO:-}" ] && REPO_ARG=(--repo "$REPO")

# ---- registry -> "id<TAB>ml_name<TAB>sim<TAB>spatial_sim<TAB>config" per city
# sim/config are per-city (fall back to defaults) — a city with big AOI relief
# uses V29_forecast_lrc_pixel, so its model + config ship under that name.
mapfile -t ROWS < <(python - "$REGISTRY" "$@" <<'PY'
import sys, yaml
reg = yaml.safe_load(open(sys.argv[1]))
want = set(sys.argv[2:])
d = reg.get("defaults", {})
dsim = d.get("simulation_name", "V29_forecast")
dcfg = d.get("config_path", "configs/V29_forecast.yaml")
for cid, c in reg["cities"].items():
    if not c.get("enabled", True):
        continue
    if want and cid not in want:
        continue
    print("\t".join([cid, c["ml_urbanheat_city_name"],
                     c.get("simulation_name", dsim),
                     c.get("spatial_sim", c.get("simulation_name", dsim)),
                     c.get("config_path", dcfg)]))
PY
)
[ ${#ROWS[@]} -gt 0 ] || { echo "no matching enabled cities in $REGISTRY" >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
uploads=()
SIM=""; CFG=""

find_geojson() {  # $1 = City dir, $2 = basename -> echoes first hit (empty if none)
  for c in "$ML/cities/$1/$2.geojson" \
           "$ML/cities/$1/raw_data/$2.geojson" \
           "$ML/cities/$1/raw_data/spatial/$2.geojson"; do
    if [ -f "$c" ]; then echo "$c"; return 0; fi
  done
  return 0
}
simplify_geojson() {  # $1 = source, $2 = out
  python - "$1" "$2" <<'PY'
import sys, json, os
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
src, out = sys.argv[1:3]
gj = json.load(open(src, encoding="utf-8-sig"))
feats = gj.get("features", [gj])
geom = unary_union([shape(f.get("geometry", f)) for f in feats]).buffer(0)
geom = geom.simplify(0.0003, preserve_topology=True)   # ~30 m
json.dump({"type": "FeatureCollection",
           "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
           "features": [{"type": "Feature", "properties": {}, "geometry": mapping(geom)}]},
          open(out, "w"))
c = mapping(geom)["coordinates"]
n = len(c[0]) if geom.geom_type == "Polygon" else "multi"
print(f"  -> {out}  ({n} pts, {os.path.getsize(out)} bytes)")
PY
}

for row in "${ROWS[@]}"; do
  row="${row%$'\r'}"
  IFS=$'\t' read -r ID NAME SIM SPATIAL_SIM CFG <<<"$row"
  echo "════════ $ID ($NAME)  sim=$SIM  spatial_sim=$SPATIAL_SIM ════════"

  spatial_src="$ML/cities/$NAME/processed_data/$SPATIAL_SIM/spatial"
  [ -d "$spatial_src" ] || { echo "  ! missing $spatial_src — skipped" >&2; continue; }

  stage="$work/$ID"; mkdir -p "$stage"
  echo "  collecting model-input rasters (*_resampled.tif + default_grid.tif)"
  ( cd "$spatial_src" && cp *_resampled.tif default_grid.tif "$stage/" 2>/dev/null )

  echo "  down-sampling the 2 m context rasters (buildings / trees / water)"
  python - "$spatial_src" "$stage" <<'PY'
import sys, pathlib, rasterio
from rasterio.enums import Resampling
src_dir, out_dir = map(pathlib.Path, sys.argv[1:3])
for name in ["BuildingFraction.tif", "TreeFraction.tif", "WaterFraction_fraction_mask.tif"]:
    p = src_dir / name
    if not p.is_file():
        print(f"    ! {name} missing — skipped"); continue
    with rasterio.open(p) as ds:
        scale = min(1.0, 2000 / max(ds.width, ds.height))
        w, h = max(1, round(ds.width * scale)), max(1, round(ds.height * scale))
        data = ds.read(1, out_shape=(h, w), resampling=Resampling.average)
        prof = ds.profile
    prof.update(width=w, height=h,
                transform=ds.transform * ds.transform.scale(ds.width / w, ds.height / h),
                compress="deflate", predictor=2, tiled=False)
    prof.pop("blockxsize", None); prof.pop("blockysize", None)
    with rasterio.open(out_dir / name, "w", **prof) as dst:
        dst.write(data, 1)
    print(f"    {name}: {ds.width}x{ds.height} -> {w}x{h}  ({(out_dir/name).stat().st_size/1e6:.1f} MB)")
PY

  tar -C "$stage" -czf "$work/spatial-$ID-V29_forecast.tar.gz" .
  ls -lh "$work/spatial-$ID-V29_forecast.tar.gz" | awk '{print "  "$5"  "$9}'
  uploads+=("$work/spatial-$ID-V29_forecast.tar.gz")

  # model + config, keyed by this city's sim (distinct sims coexist on the
  # Release: xgb-V29_forecast.joblib, xgb-V29_forecast_lrc_pixel.joblib, ...)
  if [ ! -f "$work/xgb-$SIM.joblib" ]; then
    ms="$ML/models/$SIM/XGB_model.joblib"; cs="$ML/$CFG"
    [ -f "$ms" ] || { echo "  ! missing $ms" >&2; exit 1; }
    [ -f "$cs" ] || { echo "  ! missing $cs" >&2; exit 1; }
    cp "$ms" "$work/xgb-$SIM.joblib"
    cp "$cs" "$work/config-$SIM.yaml"
    uploads+=("$work/xgb-$SIM.joblib" "$work/config-$SIM.yaml")
    echo "  model+config for sim=$SIM staged"
  fi

  aoi_src="$(find_geojson "$NAME" AOI)"
  if [ -n "$aoi_src" ]; then
    echo "  simplifying AOI ($aoi_src)"
    simplify_geojson "$aoi_src" "$work/aoi-$ID.geojson"
    uploads+=("$work/aoi-$ID.geojson")
  else
    echo "  ! no AOI.geojson for $NAME — CI will predict the full grid"
  fi
  zoi_src="$(find_geojson "$NAME" ZOI)"
  if [ -n "$zoi_src" ]; then
    echo "  simplifying ZOI ($zoi_src)"
    simplify_geojson "$zoi_src" "$work/zoi-$ID.geojson"
    uploads+=("$work/zoi-$ID.geojson")
  else
    echo "  (no ZOI.geojson for $NAME — no red outline)"
  fi
done

echo "════════ uploading ${#uploads[@]} asset(s) to release '$TAG' ════════"
if ! gh release view "$TAG" "${REPO_ARG[@]}" >/dev/null 2>&1; then
  gh release create "$TAG" "${REPO_ARG[@]}" \
    --title "V29 forecast assets" \
    --notes "Per-city spatial TIFs + AOI/ZOI + shared model/config for the daily forecast. Managed by scripts/stage_v29_assets.sh — not in git." \
    --latest=false
fi
gh release upload "$TAG" "${REPO_ARG[@]}" --clobber "${uploads[@]}"

echo "done. release '$TAG' now carries:"
gh release view "$TAG" "${REPO_ARG[@]}" --json assets --jq '.assets[] | "  \(.name)  (\(.size) bytes)"'
