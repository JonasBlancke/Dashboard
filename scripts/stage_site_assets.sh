#!/usr/bin/env bash
# Stage the bulky static tab assets (EPW heatmaps, Heat Stress story) as a
# GitHub Release on THIS repo, so deploy-site.yml can bundle them into the
# published site without any of it living in git.
#
# Run locally after (re)building those assets:
#   python pipeline/build_epw.py     # -> data/epw/{catalog.json, heat/*.png}
#   python pipeline/build_story.py   # -> data/story/heatstress/**
#   bash scripts/stage_site_assets.sh
#
# Re-run whenever data/epw/ or data/story/ change — it clobbers the assets.
#
# Env overrides:
#   REPO          default: origin / `gh repo set-default`
#   RELEASE_TAG   default: site-assets

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${RELEASE_TAG:-site-assets}"

REPO_ARG=()
[ -n "${REPO:-}" ] && REPO_ARG=(--repo "$REPO")

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

assets=()

if [ -d "$ROOT/data/epw" ] && [ -n "$(ls -A "$ROOT/data/epw" 2>/dev/null)" ]; then
  echo "==> packing data/epw/  ($(du -sh "$ROOT/data/epw" | cut -f1))"
  tar -C "$ROOT/data" -czf "$work/epw.tar.gz" epw
  assets+=("$work/epw.tar.gz")
else
  echo "!! data/epw/ missing or empty — run pipeline/build_epw.py first" >&2
fi

if [ -d "$ROOT/data/story" ] && [ -n "$(ls -A "$ROOT/data/story" 2>/dev/null)" ]; then
  echo "==> packing data/story/  ($(du -sh "$ROOT/data/story" | cut -f1))"
  tar -C "$ROOT/data" -czf "$work/story.tar.gz" story
  assets+=("$work/story.tar.gz")
else
  echo "-- data/story/ missing or empty — skipping (Heat Stress tab stays empty on Pages)"
fi

[ ${#assets[@]} -gt 0 ] || { echo "nothing to upload" >&2; exit 1; }

echo "==> ensuring release $TAG exists"
if ! gh release view "$TAG" "${REPO_ARG[@]}" >/dev/null 2>&1; then
  gh release create "$TAG" "${REPO_ARG[@]}" \
    --title "Static site assets" \
    --notes "EPW heatmaps + Heat Stress story, unpacked into the site by deploy-site.yml. Managed by scripts/stage_site_assets.sh — not tracked in git." \
    --latest=false
fi

echo "==> uploading (clobber)"
gh release upload "$TAG" "${REPO_ARG[@]}" --clobber "${assets[@]}"

echo "done. release '$TAG' now carries:"
gh release view "$TAG" "${REPO_ARG[@]}" --json assets \
  --jq '.assets[] | "  \(.name)  (\(.size) bytes)"'
