# Urban Climate Dashboard

A bespoke dark-mode climate dashboard. Three tabs are implemented:

- **Heat Stress (UTCI 2 m)** — Nouakchott & Belém, from pre-produced HeatScan
  SOLWEIG outputs (click a hex → interactive, hexagon-clipped 2 m UTCI detail).
- **EPW Files** — a world map with three published cities (Dublin, Patras,
  Wellington). Sidebar cascade: **Historical** (AMY + year / TMY / DSY — TMY &
  DSY use a fixed aggregation window, no year) or **Future** (SSP → period →
  year-type; XTMY/XDSY add a metric + return period), then a rural / LCZ-urban
  variant. The analysis stage shows a tall full-width heatmap (hour on Y, day
  of year on X), then a two-column row: monthly p10–p90 + median seasonal
  cycle on the left, min/p1/median/p99/max stats on the right — all switchable
  across six variables (dry-bulb, dew-point, RH, GHI, DNI, wind). **Compare**
  reveals a second full picker inline and overlays both files on every panel.
  A large button opens a config-YAML request form (editable lat/lon, SSP only,
  download-only). `.epw` files stream from the drive.
- **Live Forecast** — hourly ~50 m urban air-temperature forecast, Ghent,
  refreshed daily from an ECMWF-IFS forecast downscaled by the `ML-UrbanHeat`
  XGBoost model.

Lightweight: no build toolchain, no tile server. A thin Flask app serves a
static frontend (vanilla JS + MapLibre GL, vendored) and the generated assets.

```
Dashboard/
├─ pipeline/
│   ├─ build_story.py          # HeatScan assets -> data/story/
│   ├─ build_epw.py            # EPW drive -> data/epw/{catalog.json, heat/*.png}
│   ├─ build_forecast.py       # ta_abs NetCDF -> data/forecast/<city>/latest/
│   ├─ run_city_forecast.py    # orchestrate: ML-UrbanHeat run + build_forecast + manifest
│   └─ build_manifest.py       # data/forecast/manifest.json
├─ cities.forecast.yaml        # Live Forecast city registry
├─ scripts/                    # stage_v29_assets.sh, stage_site_assets.sh (Release uploaders)
├─ data/forecast/…             # committed (latest/ + previous/ per city)
├─ data/{story,epw}/…          # generated, NOT in git — ride on the site-assets Release
├─ backend/app.py              # Flask: static frontend + /data + /api/products + /api/epw/download
├─ frontend/                   # index.html, css/app.css, js/{app,epw}.js, vendor/
└─ .github/workflows/          # daily-forecast.yml, deploy-site.yml
```

## Run

```bash
pip install -r requirements.txt
python pipeline/build_story.py                        # Heat Stress assets
python pipeline/build_epw.py                          # EPW catalogue + heatmaps ($EPW_ROOT to override drive)
python pipeline/run_city_forecast.py --city ghent --skip-model   # Live Forecast (reuse newest NetCDF)
python backend/app.py                                 # -> http://127.0.0.1:8000
```

## Interaction model

**City view** — one interactive MapLibre map:
- `AOI_hex.geojson` zonal-stats hex grid, choropleth-shaded by **hot-day mean
  UTCI** (`Hot_UTCI_mean` / `Hot_UTCI_tmean_mean`).
- OSM **through-roads** (`streets-osm.geojson`, clipped to the AOI bbox, motorway
  → tertiary only) drawn on top.
- an optional **OpenStreetMap basemap** (sidebar toggle) — when on, the
  choropleth lightens and the roads darken so the basemap reads through.
- a **UTCI colour** toggle (sidebar, on by default) hides the choropleth fill on
  the city map *and* the 2 m raster on the detail map, so you can orient on the
  streets / basemap.
- The **three selected neighbourhood hexes** get a thick outline + white halo
  and are **clickable**; hover any hex for its value.

**Click an outlined hex** → an **inline detail** expands. The city map shrinks
to a sticky ~32 vh strip at the top; the detail map fills most of the remaining
screen directly beneath it (so you don't scroll to see it), and two thin dashed
connector lines link the hex on the city map to the same hex in the detail
(redrawn on every pan / zoom / scroll):
- a big interactive MapLibre map of that hex — the real **2 m UTCI** raster
  (windowed straight from the source `UTCI_MEAN.tif`), coloured on a **continuous
  YlOrRd ramp** and **clipped to the hexagon** (rasterized polygon mask — no
  square frame), over a faint OSM basemap, with OSM streets and the hex outlined
  in **thick black**. Pan / zoom freely; hover for an approximate °C (decoded
  from the PNG through the ramp — no extra data file). A colourbar under the map
  shows the scale.

**Scenario switcher** (Present / 2050 / 2090):
- swaps the detail's 2 m raster to that horizon.
- the colour scale is **pinned per neighbourhood across all three horizons**
  (`neighbourhoods[].utciRange` = `[min of the 3 lows, max of the 3 highs]`), so
  one colour == the same °C in Present / 2050 / 2090 — 2050 & 2090 just read
  hotter (more red). It is **not** comparable between different neighbourhoods.
- the **city hex choropleth does not change** — it is a present-day zonal stat
  only (labelled "present-day"); there is no future zonal layer.
- a **warning box** (sidebar + detail header) spells this out whenever a future
  horizon is selected.

`#city=…&scenario=…&hood=…` deep-links open a hex directly.

## Data pipeline (`pipeline/build_story.py`)

Reads the HeatScan `data/cities` tree directly — **windowed / bbox-filtered, no
full-file copies**. Root is `G:\Gedeelde drives\Worldbank\Projects\HeatScan\
data\cities` by default; override with `$HEATSCAN_CITIES`.

Per city → `data/story/heatstress/<City>/`:

| asset | source | note |
|---|---|---|
| `city_hexes.geojson` | `areas_of_interest/AOI_hex.geojson` | trimmed to `hex_id` + `utci` + geometry, WGS84 |
| `city_streets.geojson` | `urbanscape/raw/streets-osm.geojson` | AOI bbox, through-roads, UTF-8, no `crs` member |
| `<hood>/hex.geojson` | `areas_of_interest/hexs/<hood>.geojson` | the AOI hexagon, WGS84 |
| `<hood>/streets.geojson` | `streets-osm.geojson` | hex bbox + pad |
| `<hood>/utci_<scen>.png` | `solweig_gpu/hot_{present,2050,2100}/UTCI_MEAN.tif` | **windowed read** of the hex bbox, reprojected to WebMercator, coloured on the **continuous YlOrRd ramp** stretched to the **per-neighbourhood pinned range** (§ scenario switcher), then **clipped to the hexagon** (rasterized mask → transparent outside), 2 m native res |
| `story.json` | — | `center`, `hexRange`, `utciRamp` (9 stops), `neighbourhoods[]` with `hexId`, `utciRange` (shared, all scenarios), and per-scenario `utci` / `utciBounds` / `utciNaturalRange` |

The build does a **two pass** per neighbourhood: pass 1 reprojects each
scenario's raster and takes its natural in-hex 2–98 % range; pass 2 re-renders
all three PNGs against `[min low, max high]` so the colourbar is identical
across horizons.

`-9999` and out-of-range sentinels are treated as nodata. Belém's source
rasters are ~22k × 31k px; the windowed reads keep the build to ~90 s. No PNGs
are copied from the HeatScan figures any more — the donuts and the category
legend were dropped.

## Live Forecast tab

Downscaled ECMWF-IFS forecast → `ML-UrbanHeat` XGBoost model (`V29_forecast`) →
~50 m hourly `ta_abs` (absolute air temp, °C). The dashboard consumes the
model's NetCDF; it does **not** re-implement the model.

**`pipeline/build_forecast.py --nc <ta_abs_*.nc> --city ghent`** — strategy A:
- one **native-resolution Web-Mercator PNG per hour** for the coming
  `frame_hours` (72) — the reprojection only warps the grid (`resolution=None`),
  no up/down-sampling. NaN transparent.
- Colour ramp **Blue–Yellow–Red**, domain **auto** from the shown frames
  (whole degrees, widened to ≥ 5 °C span). Pin it by setting
  `value_domain_c: [lo, hi]` in `cities.forecast.yaml`.
- **`values.bin`** — float32 `[n_frames, y, x]` at the **native grid**, exactly
  the 72 shown hours, so the hover readout is the true `ta_abs`, not
  interpolated (`meta.value_grid.index = "frame"`).
- **Context overlays** — three static PNGs from the ML-UrbanHeat 2 m fraction
  rasters (`BuildingFraction` / `TreeFraction` / `WaterFraction`, all pixel-aligned
  with `ta_abs`): pixels with fraction ≥ 0.5 painted **black / green / blue**, rest
  transparent, ≤ 1200 px (`CONTEXT_MAX` — kept under mobile-GPU image-texture
  limits). Toggled from the layer panel (`ctx_*.png`, `meta.context`). The panel
  also toggles **Forecast** (the temp raster) and **Basemap** (OSM) on/off;
  deep-link `#tab=live&layers=buildings,water`.
- All forecast PNGs are reprojected to **EPSG:3857** — the basemap's projection —
  so the `image` overlays land pixel-true (a plate-carrée raster over a Mercator
  basemap trapezoids at the edges).
- Backend sends `Cache-Control: no-store` for `*.json` under `/data/` (manifests
  change every build) and `max-age=86400` for the heavy PNG/`.bin` assets, which
  the frontend cache-busts with `?v=<run_time>`.
- **`meta.uhi`** — per-hour urban-heat-island signal for the FULL horizon:
  `BuildingFraction_2000m` (pixel-aligned with `ta_abs`) is median-split into
  Urban (upper half) / Rural (lower half), then
  `uhi_c[h] = mean(ta_abs[h], urban) − mean(ta_abs[h], rural)`. The tab shows the
  current hour's value in the top-right stats panel. The BF raster is
  auto-found in the ML-UrbanHeat spatial dir (or set `bf2000m:` in the registry).
- **Point selector** — client-side only, no new assets. "Point selector" in the
  layer panel arms pick mode; a map click drops a pin and opens a panel under the
  scrubber. Given a (constant) indoor temperature and a goal
  (**Cooling** / **Warming**), it reads the pin's full 72 h outdoor series from
  `values.bin` and recommends windows **open** when the outside air helps
  (cooling → `T_out < T_indoor − 0.5`; warming → `T_out > T_indoor + 0.5`, 0.5 °C
  hysteresis). Shows an action callout (open/closed now, next change) and an SVG
  line chart of the outdoor series with open/closed shading, the indoor
  reference line and a "now" marker. Pure ventilation heuristic — no thermal
  mass, solar gain or humidity.
- **`meta.json`** — issue/run times, `overlay_bounds_wgs84`, `value_domain_c`,
  per-frame local-time index, `caveats` (auto-adds the no-observations note).
- passes the stage's `preview_ta_abs_*.png` through as `preview.png`.

**`pipeline/run_city_forecast.py --city ghent [--date …] [--skip-model]`** —
resolves the city, stages spatial TIFs into the `ML-UrbanHeat` tree (from
`data/forecast/_spatial/<City>/`, never Google Drive), runs
`workflow/run_forecast_downscale_v2.py` with `MERIT_HEIGHT_M` set from the
cached `merit_height_m` (skips Earth Engine), then `build_forecast` +
`build_manifest`. `--skip-model` reuses the newest existing NetCDF.

Modeling rules carried over: display **`ta_abs`** only (never `ta_delta`); UTC in,
local for display; pinned `simulation_name` + model file; never fill gaps.

### Live Forecast — GitHub Actions

`.github/workflows/daily-forecast.yml` (cron `0 6 * * *` UTC + manual) runs the
orchestrator per enabled city and commits `data/forecast/<city>/{latest,previous}/**`
— **at most two forecasts per city on disk**: each run rotates the committed
`latest/` into `previous/` and writes the fresh build to `latest/`. If Open-Meteo
has no newer issue time, or the run fails, nothing is committed and the last good
forecast stays live. `deploy-site.yml` assembles the site (rewriting the few
absolute `/data/` `/vendor/` paths to relative so it works under
`jonasblancke.github.io/Dashboard`).

### What is committed vs. what rides on a Release

Only **code + `data/forecast/<city>/{latest,previous}/**`** live in git (~26 MB).
Everything bulky is a GitHub **Release asset** on this repo — Releases don't count
toward repo size and aren't fetched by a normal `git clone`:

| Release | Assets | From | Used by |
|---|---|---|---|
| **`v29-assets`** | `spatial-ghent-V29_forecast.tar.gz` (~42 MB), `xgb-V29_forecast.joblib` (1.7 MB), `config-V29_forecast.yaml` | `ML-UrbanHeat/{cities/Ghent/processed_data,models,configs}/V29_forecast/…` | `daily-forecast.yml` — model inputs |
| **`site-assets`** | `epw.tar.gz` (~34 MB), `story.tar.gz` (~56 MB) | `data/epw/`, `data/story/` | `deploy-site.yml` — EPW + Heat Stress tabs |

`daily-forecast.yml` downloads `v29-assets`, unpacks the TIFs to
`data/forecast/_spatial/Ghent/` (gitignored) and drops the model + config into
the sparse `ML-UrbanHeat` checkout. `deploy-site.yml` downloads `site-assets` and
unpacks `data/epw/` + `data/story/` before bundling the Pages artifact (a missing
asset just leaves that tab empty).

**One-time setup**

1. **`git push origin main`** in the `b-kode/ML-UrbanHeat` checkout — the forecast
   pipeline (`forecast/*`, `workflow/run_forecast_downscale_v2.py`, the
   `MERIT_HEIGHT_M` hooks, the `run_predict_maps.py` helpers) is committed on
   `main`. The workflow's `ML_REF: main` checks it out sparsely. *(Already done if
   `git rev-parse origin/main HEAD` match.)*
2. `git init -b main` here, then
   `gh repo create JonasBlancke/Dashboard --private --source=. --remote=origin --push`.
3. Build + stage the bulky assets:
   ```
   python pipeline/build_epw.py        # if data/epw/ not fresh
   python pipeline/build_story.py      # if data/story/ not fresh
   bash scripts/stage_site_assets.sh   # -> site-assets Release
   bash scripts/stage_v29_assets.sh    # -> v29-assets Release (needs ../ML-UrbanHeat)
   ```
   Re-run either script whenever its inputs change (`--clobber`).
4. Repo **secret `ML_URBANHEAT_TOKEN`** — a token with *read* access to
   `b-kode/ML-UrbanHeat` (this repo's `GITHUB_TOKEN` cannot reach another
   account's private repo).
5. Settings → Pages → Source: **GitHub Actions**.
6. `gh workflow run daily-forecast.yml` to smoke-test (also replaces the still-V27
   committed `data/forecast/`).

V29 drops the `ERA5zdiff` feature, so CI needs **no** GDAL/momepy and **none** of
`ML-UrbanHeat/Resources/` — the lapse-rate correction uses Open-Meteo's reported
grid elevation plus the cached `merit_height_m`.

## Backend API

| Route | Purpose |
|---|---|
| `GET /api/products` | catalogue (reads `data/story/heatstress/index.json`) |
| `GET /data/<path>` | static story + forecast assets (`no-store` for `*.json`, `max-age=86400` for PNG/`.bin`) |
| `GET /` , `/<path>` | frontend (SPA fallback) |

## Notes / gaps

- Only **Nouakchott (222)** and **Belém (4593)** have populated `solweig_gpu`
  output on the drive; the other AOI cities' folders are empty.
- The MapLibre vector layers (hex choropleth, streets, hex outline) could not be
  visually verified here — headless Chrome's GeoJSON worker doesn't complete in
  this environment. The `image` overlays (2 m UTCI, and the Live Forecast hourly
  frames) render fine. Confirm the vector layers in a real browser.
- The frontend uses absolute `/data/…` `/vendor/…` paths — fine under Flask and
  a domain root, **breaks under a GitHub Pages project subpath**. Serve at a
  domain root, or make the paths relative before deploying.
- Live Forecast ships with **Ghent only**. The pipeline now targets
  `V29_forecast` (see `cities.forecast.yaml`); the model + `V29_forecast`
  spatial TIFs exist in `ML-UrbanHeat`, but there is **no `V29_forecast/
  ta_abs_*.nc` yet** — the committed `data/forecast/` is still the V27 build.
  Regenerate it with a real `python pipeline/run_city_forecast.py --city ghent`
  (needs the `ML-UrbanHeat` env + staged spatial TIFs, see the workflow);
  `--skip-model` only works once that first NetCDF exists.

## Adding a city — Heat Stress

1. It needs `solweig_gpu/hot_{present,2050,2100}/UTCI_MEAN.tif`,
   `areas_of_interest/AOI_hex.geojson` and
   `areas_of_interest/hexs/<hood>.geojson` for three neighbourhoods.
2. Add an entry to `CITIES` in `pipeline/build_story.py` (`heatscanId`,
   `center`, `hoods`).
3. `python pipeline/build_story.py`.

## Adding a city — Live Forecast

1. Add a block to `cities.forecast.yaml` (`ml_urbanheat_city_name`, `timezone`,
   `center`, `value_domain_c`, `merit_height_m`, `has_local_observations`).
2. Stage its `V29_forecast` spatial TIFs — locally under
   `data/forecast/_spatial/<City>/` (or the `ML-UrbanHeat` tree directly); for
   CI, add them to the `v29-assets` Release (extend `scripts/stage_v29_assets.sh`
   and the workflow's download step to the new city).
3. `python pipeline/run_city_forecast.py --city <id>` — no code changes.

To **refresh spatial TIFs**: they come from
`ML-UrbanHeat/cities/<City>/processed_data/V29_forecast/spatial/` (generated by
`run_prepare_spatial.py`, which needs the land-cover source rasters). Treat them
as versioned static data keyed by `(city, V29_forecast)`.
