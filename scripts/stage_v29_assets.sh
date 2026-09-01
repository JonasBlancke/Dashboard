#!/usr/bin/env bash
# Stage the heavy V29_forecast artefacts as a GitHub Release on THIS repo, so the
# daily-forecast workflow can pull them without any of it living in git.
#
# Run once locally (needs `gh` authed + the ML-UrbanHeat checkout next to this
# repo). Re-run whenever the V29 model / spatial TIFs change — it clobbers the
# existing assets on the `v29-assets` release.
#
#   bash scripts/stage_v29_assets.sh
#
# Env overrides:
#   ML_URBANHEAT_DIR   default: ../ML-UrbanHeat
#   REPO               default: current `gh repo set-default` / origin
#   RELEASE_TAG        default: v29-assets

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ML="${ML_URBANHEAT_DIR:-$ROOT/../ML-UrbanHeat}"
TAG="${RELEASE_TAG:-v29-assets}"
SIM="V29_forecast"
CITY="Ghent"

REPO_ARG=()
[ -n "${REPO:-}" ] && REPO_ARG=(--repo "$REPO")

spatial_src="$ML/cities/$CITY/processed_data/$SIM/spatial"
model_src="$ML/models/$SIM/XGB_model.joblib"
config_src="$ML/configs/$SIM.yaml"
# AOI polygon that clips the prediction grid (run_predict_maps.py::_resolve_aoi_path)
find_geojson() {  # $1 = basename (AOI / ZOI) -> echoes first hit
  for c in "$ML/cities/$CITY/$1.geojson" \
           "$ML/cities/$CITY/raw_data/$1.geojson" \
           "$ML/cities/$CITY/raw_data/spatial/$1.geojson"; do
    [ -f "$c" ] && { echo "$c"; return; }
  done
}
aoi_src="$(find_geojson AOI)"
zoi_src="$(find_geojson ZOI)"

for p in "$spatial_src" "$model_src" "$config_src"; do
  [ -e "$p" ] || { echo "missing: $p" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/spatial"; mkdir -p "$stage"

echo "==> collecting model-input rasters (*_resampled.tif + default_grid.tif)"
# these drive the XGBoost model; ~1 MB each, keep as-is.
( cd "$spatial_src" && cp *_resampled.tif default_grid.tif "$stage/" 2>/dev/null )

echo "==> down-sampling the 2 m context rasters (buildings / trees / water)"
# build_forecast.py only renders these at <=1200 px, but the native files are
# ~600 MB each. Ship ~2000 px COG-ish copies instead (nearest, so masks stay
# crisp). run_city_forecast.py picks these filenames up unchanged.
python - "$spatial_src" "$stage" <<'PY'
import sys, pathlib, numpy as np, rasterio
from rasterio.enums import Resampling
src_dir, out_dir = map(pathlib.Path, sys.argv[1:3])
targets = ["BuildingFraction.tif", "TreeFraction.tif", "WaterFraction_fraction_mask.tif"]
MAXPX = 2000
for name in targets:
    p = src_dir / name
    if not p.is_file():
        print(f"  ! {name} missing — skipped"); continue
    with rasterio.open(p) as ds:
        scale = min(1.0, MAXPX / max(ds.width, ds.height))
        w, h = max(1, round(ds.width * scale)), max(1, round(ds.height * scale))
        data = ds.read(1, out_shape=(h, w), resampling=Resampling.average)
        prof = ds.profile
    prof.update(width=w, height=h,
                transform=ds.transform * ds.transform.scale(ds.width / w, ds.height / h),
                compress="deflate", predictor=2, tiled=False)
    prof.pop("blockxsize", None); prof.pop("blockysize", None)
    with rasterio.open(out_dir / name, "w", **prof) as dst:
        dst.write(data, 1)
    mb = (out_dir / name).stat().st_size / 1e6
    print(f"  {name}: {ds.width}x{ds.height} -> {w}x{h}  ({mb:.1f} MB)")
PY

tar -C "$stage" -czf "$work/spatial-ghent-$SIM.tar.gz" .
ls -lh "$work/spatial-ghent-$SIM.tar.gz"

cp "$model_src"  "$work/xgb-$SIM.joblib"
cp "$config_src" "$work/config-$SIM.yaml"

simplify_geojson() {  # $1 = source, $2 = out
  python - "$1" "$2" <<'PY'
import sys, json, os
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
src, out = sys.argv[1:3]
gj = json.load(open(src))
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

CITY_LC="$(echo "$CITY" | tr '[:upper:]' '[:lower:]')"
upload_extra=()
if [ -n "$aoi_src" ]; then
  echo "==> simplifying AOI polygon ($aoi_src)"
  simplify_geojson "$aoi_src" "$work/aoi-$CITY_LC.geojson"
  upload_extra+=("$work/aoi-$CITY_LC.geojson")
else
  echo "==> no AOI.geojson found near $ML/cities/$CITY — CI will predict the full grid"
fi
if [ -n "$zoi_src" ]; then
  echo "==> simplifying ZOI polygon ($zoi_src)"
  simplify_geojson "$zoi_src" "$work/zoi-$CITY_LC.geojson"
  upload_extra+=("$work/zoi-$CITY_LC.geojson")
else
  echo "==> no ZOI.geojson found near $ML/cities/$CITY — no red zone-of-interest outline"
fi

echo "==> ensuring release $TAG exists"
if ! gh release view "$TAG" "${REPO_ARG[@]}" >/dev/null 2>&1; then
  gh release create "$TAG" "${REPO_ARG[@]}" \
    --title "V29 forecast assets" --notes "Model + spatial TIFs + config for the daily Ghent forecast. Managed by scripts/stage_v29_assets.sh — not tracked in git." \
    --latest=false
fi

echo "==> uploading assets (clobber)"
gh release upload "$TAG" "${REPO_ARG[@]}" --clobber \
  "$work/spatial-ghent-$SIM.tar.gz" \
  "$work/xgb-$SIM.joblib" \
  "$work/config-$SIM.yaml" \
  "${upload_extra[@]}"

echo "done. release '$TAG' now carries:"
gh release view "$TAG" "${REPO_ARG[@]}" --json assets --jq '.assets[] | "  \(.name)  (\(.size) bytes)"'
