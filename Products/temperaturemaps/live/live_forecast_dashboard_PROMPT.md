# Prompt: build a daily‑updating urban‑heat forecast dashboard

Paste everything below into the target repo. It is written to be dropped in as a
single task brief for an AI coding agent (or a human). It assumes the *modeling*
work is done in the separate `ML-UrbanHeat` repo and that this new repo only has
to (a) run the existing forecast‑downscaling stage on a schedule, (b) turn its
NetCDF output into web assets, and (c) serve a dashboard.

---

## 0. Context you are inheriting

There is an existing research repo, **`ML-UrbanHeat`**, that trains an XGBoost
model predicting the *urban heat residual* `ta_int - t2m_baseline` (observed
near‑surface air temperature minus a lapse‑rate‑corrected ECMWF/ERA5 background)
from land‑cover + meteorological predictors, and reconstructs full‑city hourly
temperature maps.

That repo already has a **working live‑forecast path**, and it has already been
run for Ghent. Do not re‑implement it. The relevant pieces:

| Piece | Path in `ML-UrbanHeat` | What it does |
|---|---|---|
| Forecast config | `configs/V27_forecast.yaml` | `simulation_name: V27_forecast`, `era5_temperature_col: t2m_lrc`, `target_variable: ta_delta`. Trained on Open‑Meteo `historical_forecast` (ECMWF‑IFS) data, not ERA5 reanalysis, so it can be applied to a *live* forecast. |
| Trained model | `models/V27_forecast/XGB_model.joblib` | The model the dashboard serves. Also `models/V27_forecast/loco_models/` (LOCO surrogates, not needed for inference). |
| Spatial inputs (Ghent) | `cities/Ghent/processed_data/V27_forecast/spatial/*_resampled.tif` + `default_grid.tif` | Produced by `run_prepare_spatial.py`. Static per city — generate once, cache. |
| Live inference stage | `workflow/run_forecast_downscale_v2.py` | Fetches an Open‑Meteo ECMWF‑IFS forecast for the city centre, adds solar + rolling/derived features, builds the pixel×hour Cartesian product, predicts, writes NetCDF. **This is the script the dashboard runs daily.** |
| Forecast fetch client | `forecast/open_meteo_fetch.py` | Thin Open‑Meteo REST client, no API key needed. |
| Lapse‑rate helper | `forecast/forecast_lapse_rate.py` | Computes `t2m_lrc` for forecast data using Open‑Meteo's own grid elevation + a MERIT‑DEM city‑centre height (needs Earth Engine auth — see §4). |
| Training‑side (only if retraining) | `workflow/run_build_forecast_total_set.py`, `run_create_train_test.py`, `run_build_model.py`, `run_validate_loco.py` | Not needed for the dashboard unless you retrain. |

### Output contract of `run_forecast_downscale_v2.py`

Writes into `cities/{CityName}/forecast/{simulation_name}/`:

- `ta_delta_{start}_to_{end}_issued{ISSUE}.nc` — residual, °C
- `ta_abs_{start}_to_{end}_issued{ISSUE}.nc` — **absolute air temperature, °C — this is what the dashboard shows**
- `preview_ta_abs_*.png`, `preview_ta_delta_*.png` — quick‑look PNGs
- accompanying `.nc.aux.xml` sidecars

`ta_abs` NetCDF structure (verified):

```
Dimensions:   (y: 245, x: 272, time: 361)          # ~15 days hourly, Ghent 50 m grid
Coordinates:  y, x  (projected metres), time (datetime64, UTC), time_str (<U20)
Data variables:
    ta_abs   (time, y, x) float32   # °C, NaN outside the valid SOLWEIG footprint
    crs_var  int64                  # CF grid_mapping
Attributes:   city, date, Conventions=CF-1.8,
              crs = 'PROJCS["WGS 84 / UTM zone 31N", ... EPSG:32631 ...]',
              GeoTransform = '<gdal 6-tuple>',
              product_type='downscaled_forecast', forecast_source='open-meteo',
              forecast_model='ecmwf_ifs', forecast_mode='live',
              forecast_issue_time_utc, fetch_time_utc, warmup_hours='24',
              skt_source='proxy=t2m ...',
              lapse_rate_forecast_grid_elevation_m, lapse_rate_merit_height_m,
              lapse_rate_delta_h_m
```

- Grid CRS for Ghent is **EPSG:32631** (UTM 31N). Other cities get their own UTM
  zone — always read the CRS from the file, never hard‑code.
- `time` is **UTC**, instantaneous at each full hour. Convert to the city's local
  timezone for display only.
- Pixels outside the valid mask are `NaN` — keep them transparent, never fill.

### Non‑negotiable modeling rules (carried over from `ML-UrbanHeat/CLAUDE.md`)

Even though the dashboard only *consumes* the model, respect these so you don't
silently ship wrong numbers:

1. **Never fill missing feature/pixel values.** If the forecast fetch returns a
   short/gappy series, the stage already trims (not fills) the tail. Don't add
   imputation anywhere downstream.
2. **The map to display is `ta_abs` (= `ta_delta + t2m_lrc`).** Never publish
   `ta_delta` alone as a temperature.
3. **One simulation = one fixed feature set / units / baseline.** Pin
   `simulation_name: V27_forecast` and the exact model file. If `ML-UrbanHeat`
   retrains under the same name, re‑pin deliberately.
4. **UTC in, local for display.** All arithmetic on `time` stays UTC.
5. A city with **no local observations** has an unanchored city‑mean offset
   (≈ ±0.4 °C 1σ). Surface this as a caveat in the UI for such cities.

---

## 1. Goal of THIS repo

A **web dashboard** showing the next ~10–15 days of hourly urban air‑temperature
forecast for a set of cities, at neighbourhood resolution (~50 m), refreshed
**once per day via GitHub Actions**.

- **Start with Ghent.** Architect everything so new cities are added by config only.
- Each daily run: fetch a fresh ECMWF‑IFS forecast → downscale → convert to web
  tiles/arrays → commit/publish assets → dashboard picks them up.
- Dashboard must be static‑hostable (GitHub Pages / Cloudflare Pages / Netlify):
  no live backend required at view time. The daily job produces all assets.

---

## 2. Deliverables

### 2.1 Repo layout (create this)

```
urban-heat-dashboard/
  cities.yaml                     # city registry (see §3)
  pipeline/
    run_city_forecast.py          # orchestrates one city end-to-end for a date
    nc_to_web.py                  # NetCDF -> web assets (tiles or COG or PNG+JSON)
    build_manifest.py             # writes site/data/manifest.json
    ml_urbanheat/                 # ML-UrbanHeat as a submodule OR pinned pip/vendored copy
  site/                           # static dashboard (framework of your choice; see §5)
    index.html
    data/
      manifest.json               # latest run metadata, per city
      ghent/
        latest/                   # symlink-free: overwrite each run
          ta_abs.json  (or .cog.tif / tiles/)
          meta.json
          preview.png
        archive/2026-08-27T06Z/…  # optional retention
  .github/workflows/
    daily-forecast.yml            # the scheduled job (see §6)
    deploy-site.yml               # build + publish the static site
  requirements.txt
  README.md
```

### 2.2 Functional deliverables

1. **`pipeline/run_city_forecast.py --city ghent [--date YYYY-MM-DD]`**
   - Resolves the city from `cities.yaml`.
   - Ensures `ML-UrbanHeat` spatial prerequisites exist for that city +
     `V27_forecast` (calls `run_prepare_spatial.py` if missing — it needs Drive
     land‑cover TIFs; see §4 "spatial inputs" for how to supply them without
     Google Drive).
   - Runs `python workflow/run_forecast_downscale_v2.py --config configs/V27_forecast.yaml --city-name <CityName>`
     (default window = today → +15 days).
   - Locates the newest `ta_abs_*_issued*.nc` it produced.
   - Calls `nc_to_web.py` to emit web assets into `site/data/<city>/latest/`.
   - Calls `build_manifest.py`.
   - Idempotent: re‑running for the same day overwrites `latest/`.

2. **`pipeline/nc_to_web.py`** — convert one `ta_abs` NetCDF to browser‑ready form.
   Pick ONE primary strategy (recommend **A** for simplicity first):
   - **A. Per‑hour PNG colour tiles + JSON index.** Reproject each `time` slice to
     Web Mercator (EPSG:3857), render with a fixed diverging/sequential ramp and a
     **fixed global temperature domain per city** (so colours are comparable
     across hours/days — compute the domain from a rolling climatology or a fixed
     e.g. `[-5, 40] °C`, store it in `meta.json`). Also emit a small `values`
     array (downsampled, e.g. to ~150×150) as JSON/Float32 base64 so the UI can
     show the exact value under the cursor. Keep NaN transparent.
   - **B. Cloud‑Optimized GeoTIFF** (`ta_abs.cog.tif`, all time bands) + client‑side
     rendering with `geotiff.js` / `georaster-layer-for-leaflet`. More flexible,
     heavier client.
   - **C. Vector/JSON only** — decimated grid to GeoJSON per hour. Simplest, ugly
     at 50 m; only for a fallback.
   Always also copy the stage's `preview_ta_abs_*.png` through as `preview.png`.

3. **`meta.json` per city per run** — must include:
   ```json
   {
     "city": "Ghent",
     "simulation_name": "V27_forecast",
     "model_file": "models/V27_forecast/XGB_model.joblib",
     "model_sha256": "…",
     "forecast_model": "ecmwf_ifs",
     "forecast_issue_time_utc": "2026-08-27T06:00:00Z",
     "fetch_time_utc": "…",
     "run_time_utc": "…",
     "time_start_utc": "…", "time_end_utc": "…", "n_hours": 361,
     "timezone": "Europe/Brussels",
     "grid_crs": "EPSG:32631",
     "bbox_wgs84": [minlon, minlat, maxlon, maxlat],
     "value_domain_c": [-5, 40],
     "has_local_observations": true,
     "caveats": []
   }
   ```

4. **`site/` dashboard** (see §5).

5. **GitHub Actions**: `daily-forecast.yml` (scheduled, matrix over cities) and
   `deploy-site.yml`.

---

## 3. `cities.yaml` (registry)

```yaml
cities:
  ghent:
    ml_urbanheat_city_name: Ghent        # must match a key in ML-UrbanHeat/city_id_converter.json
    display_name: Ghent
    country: Belgium
    timezone: Europe/Brussels
    center: {lat: 51.054, lon: 3.717}
    has_local_observations: true
    value_domain_c: [-5, 40]             # fixed colour scale, or null => auto from climatology
    enabled: true
defaults:
  simulation_name: V27_forecast
  config_path: configs/V27_forecast.yaml
  forecast_days: 16                       # ECMWF-IFS max horizon via Open-Meteo
  warmup_hours: 24                        # do NOT change unless model rolling windows change
```

Adding a city = one block here + making its spatial inputs available (§4).

---

## 4. Integrating `ML-UrbanHeat` (the hard part — spell it out)

### How to vendor it
- Preferred: **git submodule** `pipeline/ml_urbanheat/` pinned to a specific
  commit. The daily job checks it out and runs its `workflow/run_forecast_downscale_v2.py`.
- Install its deps: `xgboost`, `joblib`, `xarray`, `netCDF4`, `rioxarray`,
  `rasterio`, `exactextract`, `pvlib`, `pandas`, `numpy`, `requests`, `pyproj`,
  `earthengine-api`. (Check `ML-UrbanHeat` for a `requirements`/`environment` file
  and reuse it; pin versions.)

### Config
- Use `configs/V27_forecast.yaml` **as‑is** for the model/feature definitions.
- It contains machine‑specific Google Drive paths under `data_sources.*` and
  `observations.aws_root` / `weatherstation_validation.path`. **None of those are
  needed for `run_forecast_downscale_v2.py`** *if the spatial prerequisites are
  already built* — that script only does spatial‑grid load + Open‑Meteo fetch +
  predict. Provide a thin override config or env so those Drive paths are never
  dereferenced. If `run_prepare_spatial.py` must run, it needs the land‑cover
  rasters (see next).

### Spatial inputs (static per city — build once, cache in the repo or a release)
`run_forecast_downscale_v2.py` needs, under
`cities/<City>/processed_data/V27_forecast/spatial/`:
`default_grid.tif` and `<feat>_resampled.tif` for every entry in
`model.spatial_predictors` (BuildingFraction, TreeFraction, GrassFraction,
PavedFraction, NDVI_p50, NDVI_std, BuildingHeight, BuildingVolume_500m, Altitude,
BuildingFraction_2000m, TreeFraction_200m, GrassFraction_300m, PavedFraction_300m,
WallAreaDensity_300m, AHF, AHF_2000m).

Options, best first:
1. **Commit the resampled TIFs** for each enabled city into the dashboard repo
   (or attach as a GitHub Release asset the job downloads). They're static; Ghent's
   already exist in `ML-UrbanHeat/cities/Ghent/processed_data/V27_forecast/spatial/`.
   The daily job just copies them into place — **no Drive, no Earth Engine, fast.**
2. Run `run_prepare_spatial.py` in the job — requires the source land‑cover /
   building / DEM / NDVI rasters (normally on Google Drive) staged somewhere the
   job can read (S3/GCS bucket, Release asset). Heavier; only if you must
   regenerate.

**Recommendation: option 1.** Treat spatial TIFs as versioned static data keyed by
`(city, simulation_name)`.

### Earth Engine
`forecast_lapse_rate.py` calls Earth Engine for the MERIT‑DEM city‑centre height.
For a fixed city this is **one scalar** (`lapse_rate_merit_height_m`, e.g. Ghent
= 11.449 m; the Open‑Meteo grid elevation ≈ 11.0 m ⇒ `delta_h ≈ -0.449 m`).
Options:
1. **Cache the scalar per city in `cities.yaml`** (`merit_height_m:` and/or
   `lapse_delta_h_m:`) and add a small shim / config value so the stage skips the
   EE lookup when the cached value is present. Cleanest for CI.
2. Provide an EE service‑account key as a GitHub secret and `earthengine
   authenticate --service-account`. Works but adds a credential + network dep.

Prefer option 1: check whether `run_forecast_downscale_v2.py` /
`forecast_lapse_rate.py` already accept a pre‑supplied elevation (grep for
`merit_height`, `read_era5_merit_height`, `meta["elevation"]`); if not, add a
minimal, upstream‑friendly hook (env var or config key) rather than patching
logic.

### Networking in CI
- Open‑Meteo: free, no key, but **rate‑limited**. One city ≈ a handful of point
  requests per run. Add ret/backoff (client already has it) and stagger the matrix
  (`max-parallel: 1` or small).

---

## 5. Dashboard UI (`site/`)

Static SPA. Framework: plain TS + MapLibre GL, or React + MapLibre, or Leaflet.
No backend at view time.

**Views / features:**
- **Map**, city‑centred, basemap (MapLibre demo tiles or a free raster basemap),
  with the `ta_abs` layer for the selected hour overlaid (PNG tiles / COG / raster).
- **Time scrubber**: slider + play/pause stepping through all forecast hours;
  show local time + "day N of forecast" + the forecast **issue time**.
- **Colour legend** with the fixed per‑city domain and units (°C). State the ramp
  is comparable across hours & days.
- **Hover / tap readout**: temperature at the pointer (from the decimated `values`
  array or COG read), lat/lon, local time.
- **City switcher** (from `manifest.json`; just Ghent for now).
- **"Last updated"** banner: run time + issue time; warn if stale (> 36 h).
- **Caveats panel**: renders `meta.caveats` + the no‑local‑observations note when
  `has_local_observations` is false.
- Optional: a **point time‑series chart** (pick a location → line of the next N
  days at that pixel), diurnal min/max per day, and a city‑mean series.
- Responsive; works on mobile; respects `prefers-color-scheme`.

**Data loading:** dashboard reads `site/data/manifest.json`, then per selected
city loads `site/data/<city>/latest/meta.json` + the raster assets. Everything is
relative‑pathed so it works under a project subpath on GitHub Pages.

`manifest.json` shape:
```json
{
  "generated_utc": "2026-08-27T06:32:00Z",
  "cities": [
    {"id": "ghent", "display_name": "Ghent", "path": "ghent/latest",
     "issue_time_utc": "...", "n_hours": 361, "timezone": "Europe/Brussels"}
  ]
}
```

---

## 6. GitHub Actions

### `daily-forecast.yml`
- **Trigger:** `schedule: cron '0 6 * * *'` (06:00 UTC — after ECMWF 00Z run is
  on Open‑Meteo) + `workflow_dispatch` (manual, with optional `city` + `date`
  inputs).
- **Permissions:** `contents: write` (to commit assets) — or push assets to a
  `gh-pages` / data branch, or upload as an artifact the deploy job consumes.
- **Matrix** over enabled cities from `cities.yaml` (`max-parallel: 1`).
- **Steps:**
  1. `actions/checkout` with `submodules: recursive`.
  2. Restore cached spatial TIFs (keyed by city + simulation_name + their hash);
     if missing, fetch the Release asset / run `run_prepare_spatial.py`.
  3. Set up Python, install pinned `requirements.txt` (+ `ML-UrbanHeat` deps).
  4. (If using EE key) auth Earth Engine from secret; else rely on cached
     `merit_height_m`.
  5. `python pipeline/run_city_forecast.py --city ${{ matrix.city }}`.
  6. Commit `site/data/<city>/latest/**` + updated `manifest.json`
     (`git add`, commit `chore(data): ${{ matrix.city }} forecast <date>`, push).
     Use a concurrency group so overlapping runs don't race the commit.
  7. Upload `preview.png` + `meta.json` as a run artifact for debugging.
- **Failure handling:** if the forecast fetch or downscale fails, **do not** touch
  `latest/` (leave yesterday's data), fail the job, and (optional) open/annotate
  an issue. Never publish a partial NetCDF.

### `deploy-site.yml`
- **Trigger:** `push` to `main` touching `site/**` or `site/data/**`, plus
  `workflow_run` after `daily-forecast.yml` completes.
- Build the static site (if it has a build step) and deploy to GitHub Pages
  (`actions/deploy-pages`) or Cloudflare/Netlify.

### Secrets / config to document in README
- `EE_SERVICE_ACCOUNT_JSON` (optional, only if not caching `merit_height_m`).
- Nothing for Open‑Meteo.
- If spatial TIFs come from a private bucket: bucket creds.

---

## 7. Acceptance criteria

1. `python pipeline/run_city_forecast.py --city ghent` on a clean checkout (with
   cached spatial TIFs + cached `merit_height_m`) produces
   `site/data/ghent/latest/{meta.json, <raster assets>, preview.png}` and updates
   `manifest.json`, using **no Google Drive access**.
2. The daily workflow runs green on schedule and on manual dispatch, and commits
   fresh Ghent assets whose `meta.forecast_issue_time_utc` is within ~24 h of the
   run.
3. The deployed dashboard loads Ghent, scrubs through all forecast hours, shows a
   coherent UHI pattern (city core warmer than rural at night), a working legend
   in °C, hover readouts, local‑time labels, and a "last updated" banner.
4. Values shown equal `ta_abs` from the NetCDF (spot‑check ≥3 pixel/hour samples
   to < 0.1 °C after any reprojection/decimation).
5. Adding a second city is: add a `cities.yaml` block + supply its spatial TIFs
   (+ `merit_height_m`); no code changes.
6. On a forced forecast‑fetch failure, `latest/` is untouched and the job fails
   loudly.
7. README documents: architecture, how the model/config is pinned, how to add a
   city, how to refresh spatial TIFs, all secrets, and the modeling caveats from
   §0.

---

## 8. Explicitly out of scope (unless asked)

- Retraining / changing the XGBoost model or feature set.
- Nowcast/observation assimilation.
- Anything requiring a live server at view time.
- ERA5‑reanalysis hindcast maps (`run_predict_maps.py`) — this dashboard is
  forecast‑only.

## 9. First tasks, in order

1. Scaffold repo layout (§2.1), `cities.yaml` with Ghent, `requirements.txt`.
2. Add `ML-UrbanHeat` as a pinned submodule; get
   `run_forecast_downscale_v2.py --config configs/V27_forecast.yaml --city-name Ghent --dry-run`
   to pass locally.
3. Stage Ghent's spatial TIFs into the expected path; get a real (non‑dry) run to
   produce a `ta_abs_*.nc`.
4. Resolve the Earth Engine dependency (cache `merit_height_m` for Ghent; wire the
   config/env hook).
5. Write `nc_to_web.py` (strategy A) + `build_manifest.py`; produce
   `site/data/ghent/latest/**`.
6. Build the minimal dashboard (map + time scrubber + legend + hover + banner).
7. Wire `daily-forecast.yml` + `deploy-site.yml`; verify a manual dispatch
   end‑to‑end.
8. README + acceptance‑criteria pass.
```
