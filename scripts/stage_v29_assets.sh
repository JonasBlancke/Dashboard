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

for p in "$spatial_src" "$model_src" "$config_src"; do
  [ -e "$p" ] || { echo "missing: $p" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> packing spatial TIFs (only what the forecast stage reads)"
# *_resampled.tif + default_grid.tif drive the model; the plain BuildingFraction
# / TreeFraction / WaterFraction masks feed the context overlays + UHI split.
tar -C "$spatial_src" -czf "$work/spatial-ghent-$SIM.tar.gz" \
  $(cd "$spatial_src" && ls \
      *_resampled.tif \
      default_grid.tif \
      BuildingFraction.tif \
      BuildingFraction_fraction_mask.tif \
      TreeFraction.tif \
      TreeFraction_fraction_mask.tif \
      WaterFraction_fraction_mask.tif \
      2>/dev/null)
ls -lh "$work/spatial-ghent-$SIM.tar.gz"

cp "$model_src"  "$work/xgb-$SIM.joblib"
cp "$config_src" "$work/config-$SIM.yaml"

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
  "$work/config-$SIM.yaml"

echo "done. release '$TAG' now carries:"
gh release view "$TAG" "${REPO_ARG[@]}" --json assets --jq '.assets[] | "  \(.name)  (\(.size) bytes)"'
