"""Station-observation providers, keyed by forecast-city id.

Each provider is a callable:

    provider(aoi_rings, start, stop) -> {slug: [measurement, ...]}

where `aoi_rings` is a list of [[lon, lat], ...] outer rings (EPSG:4326),
`start`/`stop` are tz-aware UTC datetimes, and each measurement is the dict
`vlinder.normalise_measurement` produces:

    {time (ISO-8601 UTC), temp, humidity, pressure, windSpeed, windGust,
     windDirection, rainIntensity, rainVolume, wbgt, status}

A provider MAY also expose `coords(slug) -> (lon, lat)` and `label(slug) -> str`
for the GeoJSON; `fetch_forecast_stations` falls back to the first sample's
lon/lat carried on the measurement stream if not.

Cities without real coverage get a stub that raises NotImplementedError with the
intended source noted — the orchestrator turns that into an empty stations.geojson
and exits 0.
"""

from __future__ import annotations

from . import vlinder


def _stub(source: str):
    def _p(aoi_rings, start, stop):
        raise NotImplementedError(
            f"no station provider wired yet - intended source: {source}")
    return _p


# forecast-city id  ->  provider callable
PROVIDERS = {
    "ghent": vlinder.fetch,
    # --- stubs: wire these when a feed is chosen -------------------------------
    # Dublin: Met Éireann has no open sub-daily station API; nearest is the
    #   hourly SYNOP CSV at cli.fusio.net / data.gov.ie. RMI opendata is Belgium
    #   only.
    "dublin": _stub("Met Éireann hourly SYNOP (data.gov.ie) or Weather Underground PWS"),
    # Patras: Hellenic NMS (emy.gr) has no open API; NOA / meteo.gr Automatic
    #   Network (penteli.meteo.gr) exposes station JSON.
    "patras": _stub("NOA meteo.gr Automatic Weather Stations (penteli.meteo.gr)"),
    # Wellington: MetService / NIWA CliFlo — CliFlo needs a (free) login;
    #   LAWA / GW regional council has a public timeseries API.
    "wellington": _stub("NIWA CliFlo or Greater Wellington RC environmental data API"),
}
