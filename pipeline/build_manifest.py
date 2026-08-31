"""Write data/forecast/manifest.json from every <city>/latest/meta.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_manifest(data_dir: Path) -> Path:
    cities = []
    for meta_p in sorted(data_dir.glob("*/latest/meta.json")):
        m = json.loads(meta_p.read_text(encoding="utf-8"))
        cid = meta_p.parent.parent.name
        cities.append({
            "id": cid,
            "display_name": m.get("display_name", cid),
            "path": f"{cid}/latest",
            "issue_time_utc": m.get("forecast_issue_time_utc"),
            "run_time_utc": m.get("run_time_utc"),
            "n_hours": m.get("n_hours"),
            "n_frames": m.get("n_frames"),
            "timezone": m.get("timezone"),
            "has_local_observations": m.get("has_local_observations"),
        })
    out = data_dir / "manifest.json"
    out.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cities": cities,
    }, indent=2), encoding="utf-8")
    print(f"  manifest -> {out}  ({len(cities)} cities)")
    return out


if __name__ == "__main__":
    import sys
    write_manifest(Path(sys.argv[1]) if len(sys.argv) > 1
                   else Path(__file__).resolve().parents[1] / "data" / "forecast")
