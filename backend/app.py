"""
Heat-stress dashboard backend — deliberately tiny.

Serves:
  · the static frontend (frontend/)
  · the pre-collected HeatScan story assets (data/story/)
  · a small JSON API describing the product catalogue

Deploy as `gunicorn backend.app:app` on any free tier.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, abort, send_file, send_from_directory

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DATA = ROOT / "data"
STORY = DATA / "story" / "heatstress"
EPW_CATALOG = DATA / "epw" / "catalog.json"
# EPW .epw bytes are streamed straight from the drive (never copied into the
# repo); $EPW_ROOT overrides the default Google-Drive path.
EPW_ROOT = Path(os.environ.get(
    "EPW_ROOT",
    r"G:\Gedeelde drives\B-Kode\Projects\MinorityReport\data\EPW",
))

app = Flask(__name__, static_folder=None)


@app.get("/api/products")
def products():
    idx_path = STORY / "index.json"
    cities = []
    if idx_path.exists():
        cities = json.loads(idx_path.read_text(encoding="utf-8")).get("cities", [])
    return {
        "products": [
            {"id": "heatstress",
             "title": "Urban Heat Stress — UTCI 2 m neighbourhoods",
             "status": "live", "cities": cities},
            {"id": "epw", "title": "EPW Future Scenarios",
             "status": "live" if EPW_CATALOG.exists() else "planned", "cities": []},
            {"id": "liveforecast", "title": "Live Urban Temperature Forecast",
             "status": "planned", "cities": []},
        ]
    }


@app.get("/data/<path:relpath>")
def data_files(relpath: str):
    full = (DATA / relpath).resolve()
    if DATA != full and DATA not in full.parents:
        abort(403)
    if not full.is_file():
        abort(404)
    resp = send_from_directory(DATA, relpath)
    # JSON manifests describe the current build and change every run — never
    # cache them, or the browser keeps showing yesterday's layers/bounds.
    # Heavy immutable-ish assets (PNG / .bin / .geojson) can cache for a day.
    if relpath.endswith(".json"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    else:
        resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.get("/api/epw/download")
def epw_download():
    """Stream one .epw straight from EPW_ROOT. ?path= is drive-relative and
    must resolve inside EPW_ROOT and end in .epw."""
    from flask import request

    rel = (request.args.get("path") or "").strip().replace("\\", "/")
    if not rel or not rel.lower().endswith(".epw") or ".." in rel.split("/"):
        abort(400)
    full = (EPW_ROOT / rel).resolve()
    if EPW_ROOT.resolve() not in full.parents or not full.is_file():
        abort(404)
    resp = send_file(full, mimetype="text/plain",
                     as_attachment=True, download_name=full.name)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _no_store(resp):
    # dev server: never let the browser cache the frontend, so edits show up
    # on a plain reload instead of Ctrl+Shift+R.
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
def index():
    return _no_store(send_from_directory(FRONTEND, "index.html"))


@app.get("/<path:path>")
def frontend_files(path: str):
    target = (FRONTEND / path).resolve()
    if FRONTEND != target and FRONTEND not in target.parents:
        abort(403)
    if target.is_file():
        return _no_store(send_from_directory(FRONTEND, path))
    return _no_store(send_from_directory(FRONTEND, "index.html"))


if __name__ == "__main__":
    print("Heat-stress dashboard  ->  http://127.0.0.1:8000")
    app.run(host="127.0.0.1", port=8000, debug=True)
