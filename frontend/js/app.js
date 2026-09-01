/* ============================================================
   Urban Climate Dashboard — Heat Stress (UTCI 2 m)
   Click-driven. City = interactive MapLibre map: AOI hex zonal
   stats (choropleth) + OSM through-roads (+ optional OSM basemap),
   with the 3 selected neighbourhood hexes outlined & clickable.
   Click one -> an inline detail opens below: a full-width
   interactive map of the REAL 2 m UTCI raster (continuous °C
   ramp, clipped to the hexagon) + a thick black hex outline,
   with thin dashed connector lines drawn from the hex on the
   city map down to the detail map.
   ============================================================ */
(() => {
  "use strict";

  const API = {
    index: "/data/story/heatstress/index.json",
    story: (c) => `/data/story/heatstress/${encodeURIComponent(c)}/story.json`,
    asset: (c, rel) => `/data/story/heatstress/${encodeURIComponent(c)}/${rel}`,
  };

  // Choropleth ramp for the hex hot-day mean (sequential warm)
  const HEX_RAMP = ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c",
                    "#fc4e2a", "#e31a1c", "#bd0026", "#800026"];
  // Continuous UTCI ramp for the 2 m detail raster — matches build_story.py's
  // UTCI_RAMP (ColorBrewer YlOrRd 9-class). Overridden by story.utciRamp if set.
  let UTCI_RAMP = [
    [0.000, [255, 255, 204]], [0.125, [255, 237, 160]], [0.250, [254, 217, 118]],
    [0.375, [254, 178, 76]],  [0.500, [253, 141, 60]],  [0.625, [252, 78, 42]],
    [0.750, [227, 26, 28]],   [0.875, [189, 0, 38]],    [1.000, [128, 0, 38]],
  ];
  const rampCSS = (stops) =>
    stops.map(([t, c]) => `rgb(${c.join(",")}) ${(t * 100).toFixed(1)}%`).join(", ");

  const SCEN_LABEL = { present: "Present", "2050": "2050", "2090": "2090" };

  const state = {
    story: null, city: null, scenarioId: "present",
    cityMap: null, detailMap: null, openHood: null,
    cityBasemap: false, utciVisible: true,
  };

  const $ = (s) => document.querySelector(s);
  const el = {
    citySelect: $("#citySelect"), scenarioSeg: $("#scenarioSeg"),
    scenarioEpoch: $("#scenarioEpoch"), scenWarn: $("#scenWarn"),
    hoodNav: $("#hoodNav"),
    hexbar: $("#hexbar"), zoomBar: $("#zoomBar"), dataFoot: $("#dataFoot"),
    cityBasemapBtn: $("#cityBasemapBtn"), utciLayerBtn: $("#utciLayerBtn"),
    tabs: $("#tabs"), stage: $("#stage"),
    cityMap: $("#cityMap"), cityTitle: $("#cityTitle"), mapHint: $("#mapHint"),
    detail: $("#detail"), dhKicker: $("#dhKicker"), dhTitle: $("#dhTitle"),
    dhBody: $("#dhBody"), detailClose: $("#detailClose"), detailWarn: $("#detailWarn"),
    detailMap: $("#detailMap"), dmScen: $("#dmScen"),
    detailBar: $("#detailBar"), detailReadout: $("#detailReadout"),
    hexLink: $("#hexLink"),
  };
  const OSM_STYLE = {
    version: 8,
    sources: {
      osm: {
        type: "raster", tileSize: 256,
        tiles: ["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png"],
        attribution: "© OpenStreetMap",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#0b0f15" } },
      { id: "osm", type: "raster", source: "osm",
        layout: { visibility: "none" },
        paint: { "raster-opacity": 0.55, "raster-saturation": -0.6,
                 "raster-brightness-max": 0.55 } },
    ],
  };

  const cornersFor = (b) => [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]];

  // Choropleth = TYPICAL-day mean UTCI, one zonal layer PER scenario. The
  // colour range is SHARED across scenarios (story.hexRange) so one colour ==
  // one °C in Present / 2050 / 2090; the fill just reads whichever
  // `utci_<scen>` property matches the active horizon.
  const hexProp = (scen) => `utci_${scen}`;

  function hexRampExpr(range, scen) {
    const [lo, hi] = range || [30, 40];
    const stops = [];
    HEX_RAMP.forEach((c, i) => {
      stops.push(lo + ((hi - lo) * i) / (HEX_RAMP.length - 1), c);
    });
    // fall back to the present value, then the low end, if a scenario column
    // is missing for a hex
    const val = ["coalesce", ["get", hexProp(scen)], ["get", "utci_present"],
                 ["get", "utci"], lo];
    return ["interpolate", ["linear"], val, ...stops];
  }

  // ---- Scenario control ----------------------------------------
  function renderScenarioSeg() {
    el.scenarioSeg.innerHTML = state.story.scenarios
      .map((id) => `<button data-scen="${id}" class="${
        id === state.scenarioId ? "is-active" : ""}">${SCEN_LABEL[id] || id}</button>`)
      .join("");
    el.scenarioSeg.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => setScenario(b.dataset.scen)));
    syncEpoch();
  }
  function syncEpoch() {
    const sc = state.story.neighbourhoods[0]?.scenarios.find((s) => s.id === state.scenarioId);
    el.scenarioEpoch.textContent = sc ? sc.epoch : "";
    renderScenWarn();
  }

  // Warn the user what the horizon switch does. BOTH layers now move with the
  // horizon: the city choropleth swaps to that scenario's TYPICAL-day zonal
  // stat, and the 2 m detail swaps to that scenario's HOT-day raster. The
  // colour scales are fixed across horizons so a colour == the same °C.
  function renderScenWarn() {
    const future = state.scenarioId !== "present";
    const label = SCEN_LABEL[state.scenarioId] || state.scenarioId;
    const msg = future
      ? `<b>${label} horizon.</b> The city choropleth now shows the ${label}
         <b>typical-day</b> mean UTCI (SSP5-8.5); the 2&nbsp;m detail shows the
         ${label} <b>hot-day</b> UTCI. Both colour scales are <b>fixed across
         Present / 2050 / 2090</b>, so ${label} reads hotter at the same
         colour = same °C. The detail scale is still per-neighbourhood
         (not comparable between hoods).`
      : "";
    el.scenWarn.innerHTML = msg;
    el.scenWarn.hidden = !future;
    if (el.detailWarn) { el.detailWarn.innerHTML = msg; el.detailWarn.hidden = !future || el.detail.hidden; }
  }

  function setScenario(id) {
    if (id === state.scenarioId) return;
    state.scenarioId = id;
    el.scenarioSeg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.scen === id));
    syncEpoch();
    applyHexScenario();                       // swap the choropleth zonal layer
    if (state.openHood) refreshDetail();      // swap the 2 m raster
  }

  // Repaint the city choropleth for the active horizon's typical-day zonal
  // column. Range stays shared (story.hexRange).
  function applyHexScenario() {
    const map = state.cityMap;
    if (!map || !map.getLayer || !map.getLayer("hex-fill")) return;
    try {
      map.setPaintProperty("hex-fill", "fill-color",
        hexRampExpr(state.story.hexRange, state.scenarioId));
    } catch (e) {}
  }

  // ---- Colour bars -------------------------------------------
  // Two independent scales in the sidebar:
  //  · #hexbar  — HEX_RAMP, story.hexRange: the city choropleth (typical day,
  //               shared across horizons)
  //  · #zoomBar — UTCI_RAMP, the open hood's pinned range (hot-day 2 m detail).
  //               Before a hex is opened it shows the widest hood range so the
  //               user still has a reference.
  function renderHexbar() {
    const [lo, hi] = state.story.hexRange || [30, 40];
    const grad = HEX_RAMP.map((c, i) =>
      `${c} ${((i / (HEX_RAMP.length - 1)) * 100).toFixed(0)}%`).join(", ");
    el.hexbar.innerHTML = `
      <div class="hb-bar" style="background:linear-gradient(90deg,${grad})"></div>
      <div class="hb-ticks"><span>${lo}°</span><span>${((lo + hi) / 2).toFixed(0)}°</span><span>${hi}°</span></div>
      <div class="hb-cap">Shared across Present / 2050 / 2090.</div>`;
  }

  function renderZoomBar() {
    if (!el.zoomBar) return;
    let range = state.openHood && state.openHood.utciRange;
    let capTail = "";
    if (!range) {
      // no hex open yet — union of all hood ranges as a reference
      const rs = (state.story.neighbourhoods || [])
        .map((h) => h.utciRange).filter(Boolean);
      if (rs.length) {
        range = [Math.min(...rs.map((r) => r[0])), Math.max(...rs.map((r) => r[1]))];
        capTail = " (widest — open a hex for its own pinned scale)";
      }
    }
    if (!range) { el.zoomBar.innerHTML = ""; return; }
    const [lo, hi] = range;
    const grad = rampCSS(UTCI_RAMP);
    el.zoomBar.innerHTML = `
      <div class="hb-bar" style="background:linear-gradient(90deg,${grad})"></div>
      <div class="hb-ticks"><span>${lo.toFixed(1)}°</span><span>${((lo + hi) / 2).toFixed(1)}°</span><span>${hi.toFixed(1)}°</span></div>
      <div class="hb-cap">Pinned per neighbourhood, shared across horizons.${capTail}</div>`;
  }

  // ---- City map --------------------------------------------
  async function buildCityMap() {
    if (state.cityMap) { try { state.cityMap.remove(); } catch (e) {} state.cityMap = null; }
    const m = state.story;
    const [hexGeo, streetGeo] = await Promise.all([
      m.cityHexes ? fetch(API.asset(state.city, m.cityHexes)).then((r) => r.json()) : null,
      m.cityStreets ? fetch(API.asset(state.city, m.cityStreets)).then((r) => r.json()).catch(() => null) : null,
    ]);
    const sel = new Set(m.neighbourhoods.map((h) => h.hexId));

    const map = new maplibregl.Map({
      container: el.cityMap,
      style: JSON.parse(JSON.stringify(OSM_STYLE)),
      center: m.center, zoom: 10.5,
      attributionControl: { compact: true }, dragRotate: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-left");
    state.cityMap = map;
    map.on("move", drawHexLink);
    map.on("zoom", drawHexLink);

    map.on("load", () => {
      applyCityBasemap();
      if (hexGeo) {
        map.addSource("hexes", { type: "geojson", data: hexGeo, promoteId: "hex_id" });
        map.addLayer({ id: "hex-fill", type: "fill", source: "hexes",
          paint: { "fill-color": hexRampExpr(m.hexRange, state.scenarioId),
                   "fill-opacity": 0.82 } });
        map.addLayer({ id: "hex-edge", type: "line", source: "hexes",
          paint: { "line-color": "#0b0f15", "line-opacity": 0.35, "line-width": 0.4 } });
        // selectable hexes: thick outline + white halo so they pop on the ramp
        map.addLayer({ id: "hex-sel-glow", type: "line", source: "hexes",
          filter: ["in", ["get", "hex_id"], ["literal", [...sel]]],
          paint: { "line-color": "#ffffff", "line-width": 5, "line-opacity": 0.55,
                   "line-blur": 1 } });
        map.addLayer({ id: "hex-sel", type: "line", source: "hexes",
          filter: ["in", ["get", "hex_id"], ["literal", [...sel]]],
          paint: { "line-color": "#0b0f15", "line-width": 2.4 } });

        const byId = {};
        m.neighbourhoods.forEach((h) => (byId[h.hexId] = h));
        map.on("mouseenter", "hex-sel", () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", "hex-sel", () => (map.getCanvas().style.cursor = ""));
        map.on("click", "hex-fill", (e) => {
          const f = e.features && e.features[0];
          if (!f) return;
          const hid = f.properties.hex_id;
          if (byId[hid]) openDetail(byId[hid].id);
        });

        // hover value tooltip
        const pop = new maplibregl.Popup({ closeButton: false, closeOnMove: true, className: "hex-pop" });
        map.on("mousemove", "hex-fill", (e) => {
          const f = e.features && e.features[0];
          if (!f) return;
          const p = f.properties;
          const v = p[hexProp(state.scenarioId)] ?? p.utci_present ?? p.utci;
          const scLbl = SCEN_LABEL[state.scenarioId] || state.scenarioId;
          pop.setLngLat(e.lngLat)
            .setHTML(`<b>${v == null ? "—" : (+v).toFixed(1) + " °C"}</b>
               <span class="hp-sub">${scLbl} typical day</span>${
               byId[p.hex_id] ? " · click for 2 m detail" : ""}`)
            .addTo(map);
        });
        map.on("mouseleave", "hex-fill", () => pop.remove());

        const b = new maplibregl.LngLatBounds();
        (hexGeo.features || []).forEach((ft) => addCoordsToBounds(ft.geometry.coordinates, b));
        if (!b.isEmpty()) map.fitBounds(b, { padding: 40, duration: 0 });
      }
      if (streetGeo && streetGeo.features && streetGeo.features.length) {
        map.addSource("cstreets", { type: "geojson", data: streetGeo });
        map.addLayer({ id: "cstreets", type: "line", source: "cstreets",
          paint: { "line-color": "#e9edf3", "line-opacity": 0.35,
                   "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.3, 15, 1.6] } });
      }
      applyCityBasemap();       // set fill opacity / street colour for the mode
      applyUtciVisible();       // honour the UTCI-colour toggle
    });
  }

  // OSM basemap on/off for the city map. When it's on we lighten the hex
  // choropleth and darken the through-roads so the basemap reads through.
  function applyCityBasemap() {
    const map = state.cityMap;
    if (!map) return;
    const on = state.cityBasemap;
    const set = (id, prop, val) => { try { if (map.getLayer(id)) map.setPaintProperty(id, prop, val); } catch (e) {} };
    const setL = (id, val) => { try { if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", val); } catch (e) {} };
    setL("osm", on ? "visible" : "none");
    set("hex-fill", "fill-opacity", on ? 0.55 : 0.82);
    set("hex-edge", "line-color", on ? "#22303c" : "#0b0f15");
    set("cstreets", "line-color", on ? "#1b2733" : "#e9edf3");
    set("cstreets", "line-opacity", on ? 0.5 : 0.35);
    if (el.cityBasemapBtn) el.cityBasemapBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  function toggleCityBasemap() {
    state.cityBasemap = !state.cityBasemap;
    applyCityBasemap();
  }

  // UTCI colour on/off — hides the city choropleth fill AND the detail 2 m
  // raster, so the user can orient on the streets / basemap underneath.
  function applyUtciVisible() {
    const on = state.utciVisible;
    const vis = on ? "visible" : "none";
    const setL = (map, id) => { try { if (map && map.getLayer(id)) map.setLayoutProperty(id, "visibility", vis); } catch (e) {} };
    setL(state.cityMap, "hex-fill");
    setL(state.cityMap, "hex-edge");
    setL(state.detailMap, "utci");
    if (el.utciLayerBtn) el.utciLayerBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  function toggleUtciVisible() {
    state.utciVisible = !state.utciVisible;
    applyUtciVisible();
  }

  function addCoordsToBounds(c, b) {
    if (typeof c[0] === "number") b.extend(c);
    else c.forEach((x) => addCoordsToBounds(x, b));
  }
  function geojsonBounds(gj) {
    const b = new maplibregl.LngLatBounds();
    (gj.features || [gj]).forEach((f) => addCoordsToBounds(f.geometry.coordinates, b));
    return b;
  }

  // ---- Detail (inline, full-width, below the city map) ------
  async function openDetail(hoodId) {
    const h = state.story.neighbourhoods.find((x) => x.id === hoodId);
    if (!h) return;
    state.openHood = h;

    el.dhKicker.textContent = `Neighbourhood · hex ${h.hexId}`;
    el.dhTitle.textContent = h.name;
    el.dhBody.textContent = h.blurb;
    el.detail.hidden = false;
    el.stage.classList.add("has-detail");
    if (state.cityMap) requestAnimationFrame(() => state.cityMap.resize());
    el.stage.scrollIntoView({ behavior: "smooth", block: "start" });

    if (state.detailMap) { try { state.detailMap.remove(); } catch (e) {} state.detailMap = null; }
    state.detailSampler = null;
    const [hexGeo, streetGeo] = await Promise.all([
      fetch(API.asset(state.city, h.hex)).then((r) => r.json()),
      fetch(API.asset(state.city, h.streets)).then((r) => r.json()).catch(() => null),
    ]);
    state.openHexGeo = hexGeo;
    const dm = new maplibregl.Map({
      container: el.detailMap,
      style: JSON.parse(JSON.stringify(OSM_STYLE)),
      bounds: geojsonBounds(hexGeo), fitBoundsOptions: { padding: 34 },
      attributionControl: { compact: true }, dragRotate: false,
    });
    dm.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    dm.addControl(new maplibregl.ScaleControl({ maxWidth: 90, unit: "metric" }), "bottom-left");
    state.detailMap = dm;

    dm.on("load", () => {
      dm._hood = h;
      // faint OSM under the UTCI so streets/water read through the hex
      try { dm.setLayoutProperty("osm", "visibility", "visible"); } catch (e) {}
      applyDetailRaster(dm, h);
      if (streetGeo && streetGeo.features && streetGeo.features.length) {
        dm.addSource("streets", { type: "geojson", data: streetGeo });
        dm.addLayer({ id: "streets-casing", type: "line", source: "streets",
          paint: { "line-color": "#0b0f15", "line-opacity": 0.4,
                   "line-width": ["interpolate", ["linear"], ["zoom"], 13, 1.4, 17, 4.5] } });
        dm.addLayer({ id: "streets", type: "line", source: "streets",
          paint: { "line-color": "#f2f5f9", "line-opacity": 0.85,
                   "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.5, 17, 2.2] } });
      }
      dm.addSource("hex", { type: "geojson", data: hexGeo });
      dm.addLayer({ id: "hex-line", type: "line", source: "hex",
        paint: { "line-color": "#000000", "line-width": 4 } });
      dm.addLayer({ id: "hex-halo", type: "line", source: "hex",
        paint: { "line-color": "#ffffff", "line-width": 1, "line-opacity": 0.6 } });
      applyUtciVisible();      // honour the UTCI-colour toggle on the detail too
      // the maps settle over a few frames after layout changes — nudge the
      // connector a handful of times so it doesn't wait for the first pan.
      [0, 120, 300, 600].forEach((t) => setTimeout(drawHexLink, t));
    });
    dm.once("idle", drawHexLink);

    // hover readout — approximate °C by decoding the PNG pixel back through
    // the ramp (no extra data file). Falls back silently if the canvas read
    // is tainted / not ready.
    dm.on("mousemove", (e) => {
      const s = state.detailSampler;
      if (!s) return;
      const v = s.sample(e.lngLat.lng, e.lngLat.lat);
      const rd = el.detailReadout;
      if (v == null) { rd.classList.remove("is-on"); return; }
      rd.textContent = v.toFixed(1) + " °C";
      rd.classList.add("is-on");
    });
    dm.on("mouseout", () => el.detailReadout.classList.remove("is-on"));

    // keep the connector lines glued to the maps
    dm.on("move", drawHexLink);
    dm.on("zoom", drawHexLink);

    refreshDetail(true);
  }

  function applyDetailRaster(dm, h) {
    const sc = h.scenarios.find((s) => s.id === state.scenarioId) || h.scenarios[0];
    if (!sc.utci || !sc.utciBounds) return;
    const url = location.origin + API.asset(state.city, sc.utci);
    const coords = cornersFor(sc.utciBounds);
    const src = dm.getSource("utci");
    if (src) src.updateImage({ url, coordinates: coords });
    else {
      dm.addSource("utci", { type: "image", url, coordinates: coords });
      const before = dm.getLayer("streets-casing") ? "streets-casing"
        : dm.getLayer("hex-line") ? "hex-line" : undefined;
      dm.addLayer({ id: "utci", type: "raster", source: "utci",
        paint: { "raster-opacity": 0.9, "raster-resampling": "nearest" } }, before);
    }
    buildDetailSampler(sc);
  }

  // Decode the scenario PNG into an offscreen canvas; expose sample(lng,lat)
  // -> °C by inverting the ramp against the hood's shared range.
  function buildDetailSampler(sc) {
    state.detailSampler = null;
    const range = (state.openHood && state.openHood.utciRange) || sc.utciNaturalRange;
    if (!sc.utci || !sc.utciBounds || !range) return;
    const [w, s, e, n] = sc.utciBounds;
    const [lo, hi] = range;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = img.naturalWidth; cv.height = img.naturalHeight;
      const ctx = cv.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0);
      let data;
      try { data = ctx.getImageData(0, 0, cv.width, cv.height).data; }
      catch (err) { return; }               // tainted — no readout, map still fine
      // ramp lookup table in RGB for nearest-match inversion
      const N = 128;
      const lut = new Float32Array(N * 3);
      for (let i = 0; i < N; i++) {
        const c = sampleRamp(i / (N - 1));
        lut[i * 3] = c[0]; lut[i * 3 + 1] = c[1]; lut[i * 3 + 2] = c[2];
      }
      state.detailSampler = {
        sample(lng, lat) {
          if (lng < w || lng > e || lat < s || lat > n) return null;
          const px = Math.min(cv.width - 1, Math.floor(((lng - w) / (e - w)) * cv.width));
          const py = Math.min(cv.height - 1, Math.floor(((n - lat) / (n - s)) * cv.height));
          const o = (py * cv.width + px) * 4;
          if (data[o + 3] < 40) return null;   // transparent -> outside the hex
          const r = data[o], g = data[o + 1], b = data[o + 2];
          let best = 0, bd = Infinity;
          for (let i = 0; i < N; i++) {
            const dr = r - lut[i * 3], dg = g - lut[i * 3 + 1], db = b - lut[i * 3 + 2];
            const d = dr * dr + dg * dg + db * db;
            if (d < bd) { bd = d; best = i; }
          }
          return lo + (best / (N - 1)) * (hi - lo);
        },
      };
    };
    img.src = location.origin + API.asset(state.city, sc.utci);
  }

  function sampleRamp(t) {
    t = Math.max(0, Math.min(1, t));
    for (let i = 1; i < UTCI_RAMP.length; i++) {
      const [t0, c0] = UTCI_RAMP[i - 1], [t1, c1] = UTCI_RAMP[i];
      if (t <= t1) {
        const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
        return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f,
                c0[2] + (c1[2] - c0[2]) * f];
      }
    }
    return UTCI_RAMP[UTCI_RAMP.length - 1][1].slice();
  }

  function renderDetailBar(h, sc) {
    const [lo, hi] = h.utciRange || sc.utciNaturalRange || [30, 45];
    const grad = rampCSS(UTCI_RAMP);
    const nat = sc.utciNaturalRange;
    const natTxt = nat
      ? ` This horizon's values span ${nat[0].toFixed(1)}–${nat[1].toFixed(1)}&nbsp;°C.`
      : "";
    el.detailBar.innerHTML = `
      <div class="db-bar" style="background:linear-gradient(90deg,${grad})"></div>
      <div class="db-ticks">
        <span>${lo.toFixed(1)}°</span>
        <span>${((lo + hi) / 2).toFixed(1)}°</span>
        <span>${hi.toFixed(1)}°C</span>
      </div>
      <div class="db-cap">Real 2&nbsp;m UTCI · one <b>fixed scale for this
        neighbourhood</b>, shared by Present / 2050 / 2090.${natTxt}</div>`;
  }

  function refreshDetail(initial) {
    const h = state.openHood;
    if (!h) return;
    const sc = h.scenarios.find((s) => s.id === state.scenarioId) || h.scenarios[0];
    el.dmScen.textContent = SCEN_LABEL[sc.id] || sc.id;
    renderDetailBar(h, sc);
    renderZoomBar();                 // sidebar zoom scale -> this hood's range
    renderScenWarn();
    if (!initial && state.detailMap && state.detailMap.isStyleLoaded()) {
      applyDetailRaster(state.detailMap, h);
    }
    requestAnimationFrame(drawHexLink);
  }

  function closeDetail() {
    el.detail.hidden = true;
    el.stage.classList.remove("has-detail");
    state.openHood = null;
    state.openHexGeo = null;
    state.detailSampler = null;
    if (el.detailWarn) el.detailWarn.hidden = true;
    if (state.detailMap) { try { state.detailMap.remove(); } catch (e) {} state.detailMap = null; }
    renderZoomBar();                 // back to the "widest" reference scale
    drawHexLink();
    if (state.cityMap) requestAnimationFrame(() => state.cityMap.resize());
    el.stage.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- Connector lines: hex on the city map -> the same hex on the detail map.
  // Two thin dashed lines from the left/right extremes of the hex on the city
  // map to the corresponding sides of the hex on the detail map below.
  function drawHexLink() {
    const svg = el.hexLink;
    if (!svg) return;
    const h = state.openHood;
    if (!h || el.detail.hidden || !state.cityMap || !state.detailMap ||
        !state.openHexGeo || !state.detailMap.isStyleLoaded()) {
      svg.innerHTML = ""; return;
    }
    const ring = polyRing(state.openHexGeo);
    if (!ring) { svg.innerHTML = ""; return; }

    // extreme (min-x / max-x) vertices of the hex, in lng/lat
    let wPt = ring[0], ePt = ring[0];
    for (const p of ring) {
      if (p[0] < wPt[0]) wPt = p;
      if (p[0] > ePt[0]) ePt = p;
    }
    const cRect = el.cityMap.getBoundingClientRect();
    const dRect = el.detailMap.getBoundingClientRect();
    const toScreen = (map, rect, ll) => {
      const p = map.project(ll);
      return { x: rect.left + p.x, y: rect.top + p.y,
               inside: p.x >= -20 && p.x <= rect.width + 20 &&
                       p.y >= -20 && p.y <= rect.height + 20 };
    };
    const cW = toScreen(state.cityMap, cRect, wPt);
    const cE = toScreen(state.cityMap, cRect, ePt);
    const dW = toScreen(state.detailMap, dRect, wPt);
    const dE = toScreen(state.detailMap, dRect, ePt);

    // need the hex visible on the city map and the detail map on-screen
    if (!cW.inside || !cE.inside ||
        dRect.top > window.innerHeight || dRect.bottom < 0) {
      svg.innerHTML = ""; return;
    }
    // clamp the detail anchors to the detail map rect (hex can be panned partly out)
    const clampY = (y) => Math.max(dRect.top + 4, Math.min(dRect.bottom - 4, y));
    dW.y = clampY(dW.y); dE.y = clampY(dE.y);

    svg.setAttribute("viewBox", `0 0 ${window.innerWidth} ${window.innerHeight}`);
    const seg = (a, b, cls) =>
      `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"${cls ? ` class="${cls}"` : ""}/>`;
    svg.innerHTML =
      seg(cW, dW, "hl-halo") + seg(cE, dE, "hl-halo") +
      seg(cW, dW) + seg(cE, dE);
  }

  function polyRing(gj) {
    // exterior ring [[lng,lat],…] of the first polygon feature
    const f = (gj.features && gj.features[0]) || gj;
    let ring = f.geometry && f.geometry.coordinates;
    if (!ring) return null;
    while (Array.isArray(ring[0]) && Array.isArray(ring[0][0])) ring = ring[0];
    return Array.isArray(ring) && ring.length ? ring : null;
  }

  // ---- Sidebar nav -----------------------------------------
  function renderHoodNav() {
    el.hoodNav.innerHTML = state.story.neighbourhoods
      .map((h) => `<button data-hood="${h.id}">${h.name}</button>`).join("");
    el.hoodNav.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => openDetail(b.dataset.hood)));
  }

  // ---- City load -----------------------------------------
  async function loadCity(city) {
    state.city = city;
    state.openHood = null;
    el.detail.hidden = true;
    el.stage.classList.remove("has-detail");
    if (el.detailWarn) el.detailWarn.hidden = true;
    drawHexLink();
    state.story = await fetch(API.story(city)).then((r) => r.json());
    if (Array.isArray(state.story.utciRamp) && state.story.utciRamp.length) {
      UTCI_RAMP = state.story.utciRamp.map((s) => [s.t, s.rgb]);
    }

    el.cityTitle.textContent = state.story.label;
    el.dataFoot.textContent =
      `${state.story.neighbourhoods.length} detail hexes · ${state.story.scenarios.length} scenarios · AOI zonal + real 2 m SOLWEIG`;

    renderScenarioSeg();
    renderHexbar();
    renderZoomBar();
    renderHoodNav();
    await buildCityMap();
  }

  // ---- Boot ---------------------------------------------
  async function init() {
    let idx;
    try { idx = await fetch(API.index).then((r) => r.json()); }
    catch (e) { el.dataFoot.textContent = "story index unavailable"; return; }

    el.citySelect.innerHTML = idx.cities
      .map((c) => `<option value="${c.id}">${c.label}</option>`).join("");
    el.citySelect.addEventListener("change", () => loadCity(el.citySelect.value));
    el.detailClose.addEventListener("click", closeDetail);
    if (el.cityBasemapBtn) el.cityBasemapBtn.addEventListener("click", toggleCityBasemap);
    if (el.utciLayerBtn) el.utciLayerBtn.addEventListener("click", toggleUtciVisible);

    // keep the hex connector lines glued while the page scrolls / resizes
    window.addEventListener("scroll", drawHexLink, { passive: true });
    window.addEventListener("resize", drawHexLink);

    el.tabs.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        el.tabs.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
        tab.classList.add("is-active");
        const which = tab.dataset.tab;
        document.querySelectorAll(".panel").forEach((p) =>
          p.classList.toggle("is-hidden", p.dataset.panel !== which));
        el.stage.classList.toggle("stage-hidden", which !== "utci");
        document.getElementById("fcStage").classList.toggle("stage-hidden", which !== "live");
        el.hexLink.style.display = which === "utci" ? "" : "none";
        if (which === "utci" && state.cityMap) {
          state.cityMap.resize();
          if (state.detailMap) state.detailMap.resize();
          requestAnimationFrame(drawHexLink);
        }
        if (which === "live") Forecast.activate();
      });
    });

    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("tab") === "live") {
      el.tabs.querySelector('.tab[data-tab="live"]')?.click();
    }

    if (idx.cities.length) {
      const wantCity = h.get("city");
      const start = idx.cities.some((c) => c.id === wantCity) ? wantCity : idx.cities[0].id;
      el.citySelect.value = start;
      if (h.get("scenario")) state.scenarioId = h.get("scenario");
      await loadCity(start);
      const wantHood = h.get("hood");
      if (wantHood && state.story.neighbourhoods.some((n) => n.id === wantHood)) {
        setTimeout(() => openDetail(wantHood), 300);
      }
    }
  }

  init().catch((err) => {
    console.error("init failed", err);
    el.dataFoot.textContent = "init error: " + err.message;
  });

  /* ==========================================================
     Live Forecast tab — hourly ~50 m air-temp forecast.
     Assets: data/forecast/manifest.json, then per city
     <city>/latest/{meta.json, frame_NNN.png, values.bin}.
     ========================================================== */
  const Forecast = (() => {
    const FC = {
      index: "/data/forecast/manifest.json",
      meta: (c) => `/data/forecast/${c}/latest/meta.json`,
      frame: (c, i, mode) => `/data/forecast/${c}/latest/` +
        (mode === "uhi" ? "frame_uhi_" : "frame_") + String(i).padStart(3, "0") + ".png",
      values: (c) => `/data/forecast/${c}/latest/values.bin`,
      asset: (c, f) => `/data/forecast/${c}/latest/${f}`,
    };
    // MapLibre `image` sources need an absolute URL. FC.* return site-root paths
    // ("/data/…" in source; the deploy step makes them relative for the Pages
    // subpath). baseHref() gives the matching prefix (with trailing slash) in
    // both, so `baseHref() + FC.x().replace(/^\//,"")` resolves either way.
    const baseHref = () =>
      document.baseURI.replace(/[#?].*$/, "").replace(/[^/]*$/, "");
    const abs = (p) => baseHref() + String(p).replace(/^\//, "");
    // Turbo, remapped (tight green/yellow) + a deep-red -> #4A0D3F purple hot
    // tail. Generated to match pipeline/build_forecast.py's RAMP — keep in sync.
    const RAMP = [
      [0.00, [48, 18, 59]],   [0.05, [69, 76, 183]],   [0.10, [62, 136, 226]],
      [0.15, [51, 188, 217]], [0.20, [45, 217, 188]],  [0.25, [48, 237, 151]],
      [0.30, [87, 249, 108]], [0.35, [132, 253, 74]],  [0.40, [174, 248, 53]],
      [0.45, [207, 237, 45]], [0.50, [231, 220, 50]],  [0.55, [245, 198, 49]],
      [0.60, [253, 173, 44]], [0.65, [253, 145, 36]],  [0.70, [247, 115, 28]],
      [0.75, [236, 85, 20]],  [0.80, [217, 59, 13]],   [0.85, [177, 36, 6]],
      [0.90, [137, 20, 12]],  [0.95, [106, 17, 38]],   [1.00, [74, 13, 63]],
    ];
    function rampCSS(stops) {
      return stops.map(([t, c]) => `rgb(${c.join(",")}) ${(t * 100).toFixed(0)}%`).join(", ");
    }

    // Blue -> Yellow -> Red diverging ramp — the per-hour "Heat contrast" layer.
    // Kept in sync with pipeline/build_forecast.py (REDS). Coldest pixel of the
    // hour = blue, mid = yellow, warmest = red.
    const REDS = [
      [0.000, [49, 54, 149]],   [0.125, [69, 117, 180]],  [0.250, [116, 173, 209]],
      [0.375, [171, 217, 233]], [0.500, [255, 255, 191]],  [0.625, [254, 224, 144]],
      [0.750, [253, 174, 97]],  [0.875, [244, 109, 67]],   [1.000, [215, 48, 39]],
    ];

    const S = {
      started: false, map: null, city: null, meta: null,
      frame: 0, playing: false, timer: null, values: null,
      frameMean: null, uhi: null, cities: [], mode: "absolute",
      layers: { forecast: true, basemap: true, buildings: true, trees: false, water: true },
      // point selector
      picking: false, marker: null, adv: { lngLat: null, indoor: 24, goal: "cool", series: null },
    };
    const g = (id) => document.getElementById(id);
    const OV = "fc-ov";
    const HYST = 0.5;   // °C hysteresis so near-ties don't flip the recommendation

    async function activate() {
      if (!S.started) { S.started = true; await boot(); }
      if (S.map) S.map.resize();
    }

    async function boot() {
      let mf;
      try { mf = await fetch(FC.index).then((r) => r.json()); }
      catch (e) { g("fcBanner").textContent = "no forecast manifest"; return; }
      S.cities = mf.cities || [];
      g("fcCitySelect").innerHTML = S.cities
        .map((c) => `<option value="${c.id}">${c.display_name}</option>`).join("");
      g("fcCitySelect").addEventListener("change", () => loadCity(g("fcCitySelect").value));
      g("fcSlider").addEventListener("input", () => setFrame(+g("fcSlider").value));
      g("fcPlay").addEventListener("click", togglePlay);
      g("fcLayers").querySelectorAll(".fl-chip").forEach((c) => {
        if (c.id === "fcPickBtn") return;
        c.addEventListener("click", () => toggleLayer(c.dataset.layer));
      });
      const fcMode = g("fcMode");
      if (fcMode) fcMode.querySelectorAll(".fl-chip").forEach((c) =>
        c.addEventListener("click", () => setMode(c.dataset.mode)));
      // point selector
      g("fcPickBtn").addEventListener("click", togglePick);
      g("fcAdvisorClose").addEventListener("click", clearPoint);
      g("faIndoor").addEventListener("input", () => {
        S.adv.indoor = parseFloat(g("faIndoor").value);
        if (isFinite(S.adv.indoor)) renderAdvisor();
      });
      g("faGoalSeg").querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => {
          S.adv.goal = b.dataset.goal;
          g("faGoalSeg").querySelectorAll("button").forEach((x) =>
            x.classList.toggle("is-active", x === b));
          renderAdvisor();
        }));
      let rT;
      window.addEventListener("resize", () => {
        clearTimeout(rT);
        rT = setTimeout(() => { if (S.adv.series) renderChart(computeSchedule()); }, 150);
      });
      // deep-link: #tab=live&layers=buildings,water  (comma list of ON layers)
      const wantL = new URLSearchParams(location.hash.slice(1)).get("layers");
      if (wantL != null) {
        const on = new Set(wantL.split(",").map((s) => s.trim()).filter(Boolean));
        for (const k of Object.keys(S.layers)) S.layers[k] = on.has(k);
        if (!wantL.includes("forecast")) S.layers.forecast = true; // keep temp unless explicitly dropped
      }
      if (S.cities.length) { g("fcCitySelect").value = S.cities[0].id; await loadCity(S.cities[0].id); }
    }

    async function loadCity(cid) {
      stop();
      clearPoint();      // drop any advisor pin from the previous city
      S.city = cid;
      S.meta = await fetch(FC.meta(cid)).then((r) => r.json());
      const m = S.meta;
      // cache-bust the per-run assets (same filenames, new content each build)
      S.v = "?v=" + encodeURIComponent(m.run_time_utc || m.forecast_issue_time_utc || "0");

      g("fcTitle").textContent = m.display_name;
      S.frame = 0;
      // per-run layer descriptors (fallback for pre-"layers" meta.json)
      S.mL = m.layers || {
        absolute: { domain_c: m.value_domain_c, domain_source: m.value_domain_source, equalised: false },
        uhi: null,
      };
      if (!S.mL.uhi && S.mode === "uhi") S.mode = "absolute";
      syncModeChips();
      updateBar();
      renderBanner(m);
      renderCaveats(m.caveats);

      g("fcSlider").max = String(m.n_frames - 1);
      g("fcSlider").value = "0";

      // per-hour UHI (urban − rural, coarse BuildingFraction median split)
      S.uhi = m.uhi || null;
      g("fcUhiItem").hidden = !S.uhi;

      // values.bin — native grid, the shown window (index: "frame")
      const buf = await fetch(FC.values(cid) + S.v).then((r) => r.arrayBuffer());
      const vg = m.value_grid;
      S.values = { w: vg.width, h: vg.height, nH: vg.n_hours,
                   bounds: vg.bounds_wgs84, data: new Float32Array(buf) };
      S.frameMean = computeFrameMeans(S.values);

      buildMap(m);
      preloadFrames(cid, m.n_frames);
      setFrame(0);
    }

    // per-frame city-mean air temp (NaN-aware), from values.bin
    function computeFrameMeans(v) {
      const px = v.w * v.h, out = new Float32Array(v.nH);
      for (let k = 0; k < v.nH; k++) {
        let s = 0, c = 0;
        for (let i = 0; i < px; i++) {
          const x = v.data[k * px + i];
          if (isFinite(x)) { s += x; c++; }
        }
        out[k] = c ? s / c : NaN;
      }
      return out;
    }

    function buildMap(m) {
      if (S.map) { try { S.map.remove(); } catch (e) {} S.map = null; }
      const [w, s, e, n] = m.overlay_bounds_wgs84;
      const map = new maplibregl.Map({
        container: "fcMap",
        style: { version: 8, sources: {
          osm: { type: "raster", tileSize: 256,
            tiles: ["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
                    "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png"],
            attribution: "© OpenStreetMap" },
        }, layers: [
          { id: "bg", type: "background", paint: { "background-color": "#eef1f4" } },
          { id: "osm", type: "raster", source: "osm",
            paint: { "raster-opacity": 0.9, "raster-saturation": -0.55,
                     "raster-contrast": -0.15, "raster-brightness-min": 0.15 } },
        ] },
        bounds: [[w, s], [e, n]], fitBoundsOptions: { padding: 30 },
        attributionControl: { compact: true }, dragRotate: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-left");
      S.map = map;

      const box = [[w, n], [e, n], [e, s], [w, s]];
      map.on("load", () => {
        // forecast temperature raster
        map.addSource(OV, { type: "image",
          url: abs(FC.frame(S.city, S.frame, S.mode)) + S.v, coordinates: box });
        map.addLayer({ id: OV, type: "raster", source: OV,
          paint: { "raster-opacity": 0.86, "raster-resampling": "linear" } });
        // context overlays — static, above the forecast. z-order low→high:
        // water, trees, buildings (buildings always readable on top).
        const ctx = S.meta.context || {};
        for (const key of ["water", "trees", "buildings"]) {
          if (!ctx[key]) continue;
          const id = "ctx-" + key;
          map.addSource(id, { type: "image",
            url: abs(FC.asset(S.city, ctx[key].file)) + S.v, coordinates: box });
          map.addLayer({ id, type: "raster", source: id,
            layout: { visibility: S.layers[key] ? "visible" : "none" },
            paint: { "raster-opacity": key === "trees" ? 0.72
                       : key === "water" ? 0.85 : 0.95,
                     "raster-resampling": "nearest" } });
        }
        // AOI outline — thick black boundary around the modelled area
        if (S.meta.aoi && S.meta.aoi.file) {
          map.addSource("aoi", { type: "geojson",
            data: abs(FC.asset(S.city, S.meta.aoi.file)) + S.v });
          map.addLayer({ id: "aoi-halo", type: "line", source: "aoi",
            paint: { "line-color": "#ffffff", "line-width": 6, "line-opacity": 0.6 } });
          map.addLayer({ id: "aoi-line", type: "line", source: "aoi",
            paint: { "line-color": "#000000", "line-width": 3 } });
        }
        applyLayers();
      });

      map.on("mousemove", (ev) => {
        const v = sampleValues(ev.lngLat.lng, ev.lngLat.lat, S.frame);
        const rd = g("fcReadout");
        if (!isFinite(v)) { rd.classList.add("is-hidden"); return; }
        rd.classList.remove("is-hidden");
        g("fcReadoutVal").textContent = v.toFixed(1) + " °C";
        g("fcReadoutCoord").textContent =
          `${ev.lngLat.lat.toFixed(4)}, ${ev.lngLat.lng.toFixed(4)}`;
      });
      map.on("mouseout", () => g("fcReadout").classList.add("is-hidden"));

      map.on("click", (ev) => { if (S.picking) pickPoint(ev.lngLat); });
    }

    // layer visibility: forecast (temp raster), basemap (OSM), buildings/trees/water.
    // Never gates on isStyleLoaded() — that can stay false while basemap tiles
    // load and would swallow the toggle. Each setLayoutProperty is guarded by
    // getLayer() so it's a no-op until that layer is added, then applyLayers()
    // is called again from map.on("load").
    function applyLayers() {
      const m = S.map;
      if (!m) return;
      const set = (id, on) => {
        try {
          if (m.getLayer(id)) m.setLayoutProperty(id, "visibility", on ? "visible" : "none");
        } catch (e) { /* style not ready yet */ }
      };
      set("osm", S.layers.basemap);
      set(OV, S.layers.forecast);
      set("ctx-buildings", S.layers.buildings);
      set("ctx-trees", S.layers.trees);
      set("ctx-water", S.layers.water);
      try {
        if (m.getLayer(OV)) m.setPaintProperty(OV, "raster-opacity", S.layers.basemap ? 0.86 : 1);
      } catch (e) {}
      document.querySelectorAll("#fcLayers .fl-chip").forEach((c) => {
        if (c.id === "fcPickBtn") return;
        c.classList.toggle("is-on", !!S.layers[c.dataset.layer]);
      });
    }
    function toggleLayer(key) {
      if (!(key in S.layers)) return;
      S.layers[key] = !S.layers[key];
      applyLayers();
    }

    // ---- display mode: "absolute" (fixed °C, equalised ramp) vs
    //      "uhi" (per-hour rescale, Reds — coldest pixel now = white) --------
    function syncModeChips() {
      const box = g("fcMode");
      if (!box) return;
      const hasUhi = !!(S.mL && S.mL.uhi);
      box.querySelectorAll(".fl-chip").forEach((c) => {
        const on = c.dataset.mode === S.mode;
        c.classList.toggle("is-on", on);
        if (c.dataset.mode === "uhi") c.disabled = !hasUhi, c.style.opacity = hasUhi ? "" : ".4";
      });
    }
    function setMode(mode) {
      if (mode === S.mode || (mode === "uhi" && !(S.mL && S.mL.uhi))) return;
      S.mode = mode;
      syncModeChips();
      preloadFrames(S.city, S.meta.n_frames);
      updateBar();
      // swap the overlay image to this mode's frame
      const [w, s, e, n] = S.meta.overlay_bounds_wgs84;
      const src = S.map && S.map.getSource(OV);
      if (src) src.updateImage({
        url: abs(FC.frame(S.city, S.frame, S.mode)) + S.v,
        coordinates: [[w, n], [e, n], [e, s], [w, s]],
      });
    }

    // ---- colour bar — depends on mode (+ current frame in UHI mode) --------
    function updateBar() {
      const bn = g("fcBarNote");
      if (S.mode === "uhi") {
        const d = (S.mL.uhi.per_frame_domain_c || [])[S.frame] || [0, S.mL.uhi.min_span_c || 2];
        g("fcBar").innerHTML = `
          <div class="hb-bar" style="background:linear-gradient(90deg,${rampCSS(REDS)})"></div>
          <div class="hb-ticks"><span>${d[0].toFixed(1)}°</span>` +
          `<span>${((d[0] + d[1]) / 2).toFixed(1)}°</span><span>${d[1].toFixed(1)}°C</span></div>`;
        if (bn) bn.textContent =
          `Heat contrast — rescaled every hour: coldest point now = blue, ` +
          `warmest = red (min ${S.mL.uhi.min_span_c} °C span). Shows the ` +
          `urban-heat pattern, not absolute temperature.`;
      } else {
        const dom = S.mL.absolute.domain_c;
        renderBar(dom);
        if (bn) bn.textContent = (S.mL.absolute.domain_source === "registry"
          ? "Pinned scale — comparable across runs."
          : `Auto scale for this run (${dom[0]}–${dom[1]} °C) — same across all ${S.meta.n_frames} h.`) +
          (S.mL.absolute.equalised ? " Non-linear ramp: more colour where most pixels sit." : "");
      }
    }

    // simple in-memory frame cache so the scrubber is smooth
    const cache = new Map();
    function preloadFrames(cid, n) {
      cache.clear();
      for (let i = 0; i < n; i++) {
        const im = new Image();
        im.src = FC.frame(cid, i, S.mode);
        cache.set(i, im);
      }
    }

    function setFrame(i) {
      if (!S.meta) return;
      S.frame = Math.max(0, Math.min(S.meta.n_frames - 1, i | 0));
      g("fcSlider").value = String(S.frame);
      const ft = S.meta.frames[S.frame];
      g("fcTimeMain").textContent = ft.local + "  ·  local";
      g("fcTimeSub").textContent =
        `day ${ft.day} of forecast · issued ${fmtIssue(S.meta.forecast_issue_time_utc)}`;
      const mv = S.frameMean && S.frameMean[S.frame];
      g("fcMeanVal").textContent = isFinite(mv) ? mv.toFixed(1) + " °C" : "—";
      if (S.uhi) {
        const u = S.uhi.uhi_c[S.meta.frame_start_index + S.frame];
        g("fcUhiVal").textContent = (u >= 0 ? "+" : "") + u.toFixed(2) + " °C";
        g("fcUhiVal").style.color = u >= 0.15 ? "var(--hot)"
          : u <= -0.05 ? "var(--accent)" : "var(--text-1)";
      }
      const [w, s, e, n] = S.meta.overlay_bounds_wgs84;
      const src = S.map && S.map.getSource(OV);
      if (src) src.updateImage({
        url: abs(FC.frame(S.city, S.frame, S.mode)) + S.v,
        coordinates: [[w, n], [e, n], [e, s], [w, s]],
      });
      if (S.mode === "uhi") updateBar();    // per-hour scale changes each frame
      if (S.adv.series) renderAdvisor();    // move the "now" line / re-verdict
    }

    function togglePlay() { S.playing ? stop() : play(); }
    function play() {
      S.playing = true; g("fcPlay").textContent = "⏸";
      S.timer = setInterval(() => {
        setFrame(S.frame + 1 >= S.meta.n_frames ? 0 : S.frame + 1);
      }, 550);
    }
    function stop() {
      S.playing = false; g("fcPlay").textContent = "▶";
      if (S.timer) { clearInterval(S.timer); S.timer = null; }
    }

    // frameIdx 0..n_frames-1 — values.bin is the native-resolution grid for
    // exactly the shown window (index: "frame" in meta), so no interpolation.
    function sampleValues(lng, lat, frameIdx) {
      const v = S.values;
      if (!v) return NaN;
      const [l, b, r, t] = v.bounds;
      if (lng < l || lng > r || lat < b || lat > t) return NaN;
      const col = Math.min(v.w - 1, Math.floor(((lng - l) / (r - l)) * v.w));
      const row = Math.min(v.h - 1, Math.floor(((t - lat) / (t - b)) * v.h));
      const k = Math.max(0, Math.min(v.nH - 1, frameIdx));
      return v.data[k * v.w * v.h + row * v.w + col];
    }

    /* ---------- Point selector ------------------------------
       Pick a point -> read its full 72 h outdoor series from
       values.bin. Given a (constant) indoor temp and a goal,
       recommend windows OPEN when outside air helps:
         cooling  -> open when T_out < T_indoor − HYST
         warming  -> open when T_out > T_indoor + HYST
       Pure ventilation heuristic (see the note in the panel). */
    function togglePick() {
      S.picking = !S.picking;
      g("fcPickBtn").classList.toggle("is-on", S.picking);
      const c = g("fcMap");
      if (c) c.classList.toggle("is-picking", S.picking);
    }

    function clearPoint() {
      S.picking = false;
      g("fcPickBtn").classList.remove("is-on");
      const c = g("fcMap"); if (c) c.classList.remove("is-picking");
      if (S.marker) { try { S.marker.remove(); } catch (e) {} S.marker = null; }
      S.adv.lngLat = null; S.adv.series = null;
      g("fcAdvisor").hidden = true;
    }

    function pickPoint(lngLat) {
      S.picking = false;
      g("fcPickBtn").classList.remove("is-on");
      const c = g("fcMap"); if (c) c.classList.remove("is-picking");

      const series = sampleSeries(lngLat.lng, lngLat.lat);
      if (!series || !series.some(isFinite)) return;  // outside the grid — ignore

      if (S.marker) { try { S.marker.remove(); } catch (e) {} }
      const elm = document.createElement("div");
      elm.className = "fc-pin";
      S.marker = new maplibregl.Marker({ element: elm, anchor: "bottom" })
        .setLngLat(lngLat).addTo(S.map);

      S.adv.lngLat = lngLat;
      S.adv.series = series;
      g("fcAdvisor").hidden = false;
      renderAdvisor();
    }

    // outdoor air-temp for every shown hour at one point
    function sampleSeries(lng, lat) {
      const n = S.values ? S.values.nH : 0;
      const out = new Array(n);
      for (let k = 0; k < n; k++) out[k] = sampleValues(lng, lat, k);
      return out;
    }

    // schedule[k] = true  -> windows OPEN at hour k
    function computeSchedule() {
      const s = S.adv.series, T = S.adv.indoor, cool = S.adv.goal === "cool";
      return s.map((t) => {
        if (!isFinite(t)) return false;
        return cool ? t < T - HYST : t > T + HYST;
      });
    }

    function renderAdvisor() {
      if (!S.adv.series) return;
      const ll = S.adv.lngLat;
      g("faTitle").textContent = `${ll.lat.toFixed(4)}, ${ll.lng.toFixed(4)}`;
      const sched = computeSchedule();
      renderVerdict(sched);
      renderChart(sched);
    }

    function renderVerdict(sched) {
      const el = g("faVerdict");
      const now = S.frame;
      const openNow = sched[now];
      const cool = S.adv.goal === "cool";
      // find the next hour whose state differs
      let change = -1;
      for (let k = now + 1; k < sched.length; k++) {
        if (sched[k] !== openNow) { change = k; break; }
      }
      const act = openNow ? "Open the windows" : "Keep the windows closed";
      let msg = `<b>${openNow ? "OPEN now" : "CLOSED now"}</b> — ${act}`;
      msg += cool ? " to let cooler outside air in." : " to catch the warmer outside air.";
      if (change >= 0) {
        const ft = S.meta.frames[change];
        const verb = openNow ? "close them" : "open them";
        msg += `<br>Then <b>${verb}</b> at ${ft.local.slice(5)} (in ${change - now} h).`;
      } else {
        msg += `<br>No change for the rest of the ${sched.length} h horizon.`;
      }
      // count remaining open hours
      const openLeft = sched.slice(now).filter(Boolean).length;
      msg += `<br><span style="color:var(--text-2)">${openLeft} of the next ${sched.length - now} h favour open windows.</span>`;
      el.innerHTML = msg;
      el.classList.toggle("is-shut", !openNow);
    }

    // 72 h outdoor line + open/closed shading + indoor reference + "now" marker.
    // viewBox width == the SVG's real pixel width so 1 unit = 1 px: no
    // preserveAspectRatio stretch, so text / ticks / the dot stay undistorted.
    function renderChart(sched) {
      const svg = g("faChart");
      const W = Math.max(320, Math.round(svg.getBoundingClientRect().width) || 720);
      const H = 150, padL = 34, padR = 8, padT = 10, padB = 18;
      const s = S.adv.series, n = s.length;
      const fin = s.filter(isFinite);
      let lo = Math.min(S.adv.indoor, ...fin), hi = Math.max(S.adv.indoor, ...fin);
      if (hi - lo < 4) { const m = (hi + lo) / 2; lo = m - 2; hi = m + 2; }
      lo = Math.floor(lo - 0.5); hi = Math.ceil(hi + 0.5);
      const x = (k) => padL + (k / (n - 1)) * (W - padL - padR);
      const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

      const parts = [];
      // open/closed background bands (one rect per contiguous run)
      let runStart = 0;
      for (let k = 1; k <= n; k++) {
        if (k === n || sched[k] !== sched[runStart]) {
          const x0 = x(runStart) - (runStart === 0 ? padL : (x(runStart) - x(runStart - 1)) / 2);
          const x1 = x(k - 1) + (k === n ? padR : (x(k) - x(k - 1)) / 2);
          const fill = sched[runStart] ? "rgba(76,201,240,0.16)" : "rgba(247,145,58,0.13)";
          parts.push(`<rect x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${H - padT - padB}" fill="${fill}"/>`);
          runStart = k;
        }
      }
      // y gridlines + labels
      const ticks = niceTicks(lo, hi, 4);
      for (const tv of ticks) {
        const yy = y(tv).toFixed(1);
        parts.push(`<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="rgba(255,255,255,0.06)"/>`);
        parts.push(`<text x="${padL - 5}" y="${(+yy + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="#61707f" font-family="ui-monospace,Consolas,monospace">${tv}°</text>`);
      }
      // day boundaries (x ticks) from frame local labels
      for (let k = 0; k < n; k++) {
        const f = S.meta.frames[k];
        if (f && (f.hour_local === 0 || / 00:00$/.test(f.local))) {
          const lbl = f.local.slice(5, 10);   // MM-DD
          parts.push(`<line x1="${x(k).toFixed(1)}" y1="${padT}" x2="${x(k).toFixed(1)}" y2="${H - padB}" stroke="rgba(255,255,255,0.08)" stroke-dasharray="2 3"/>`);
          parts.push(`<text x="${x(k).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="8.5" fill="#61707f" font-family="ui-monospace,Consolas,monospace">${lbl}</text>`);
        }
      }
      // indoor reference line
      const yi = y(S.adv.indoor).toFixed(1);
      parts.push(`<line x1="${padL}" y1="${yi}" x2="${W - padR}" y2="${yi}" stroke="#9aa7b6" stroke-width="1" stroke-dasharray="4 3"/>`);
      // outdoor temp polyline (break the path across NaN gaps)
      let d = "", pen = false;
      for (let k = 0; k < n; k++) {
        if (!isFinite(s[k])) { pen = false; continue; }
        d += `${pen ? " L" : " M"}${x(k).toFixed(1)} ${y(s[k]).toFixed(1)}`;
        pen = true;
      }
      parts.push(`<path d="${d.trim()}" fill="none" stroke="#f7913a" stroke-width="1.6"/>`);
      // "now" marker
      const xn = x(S.frame).toFixed(1);
      parts.push(`<line x1="${xn}" y1="${padT}" x2="${xn}" y2="${H - padB}" stroke="#4cc9f0" stroke-width="1.5"/>`);
      if (isFinite(s[S.frame]))
        parts.push(`<circle cx="${xn}" cy="${y(s[S.frame]).toFixed(1)}" r="3" fill="#4cc9f0"/>`);

      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      svg.innerHTML = parts.join("");
    }

    function niceTicks(lo, hi, want) {
      const span = hi - lo;
      const raw = span / want;
      const mag = Math.pow(10, Math.floor(Math.log10(raw)));
      const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag;
      const out = [];
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-6; v += step) out.push(Math.round(v * 10) / 10);
      return out;
    }

    function renderBar(dom) {
      const [lo, hi] = dom;
      g("fcBar").innerHTML = `
        <div class="hb-bar" style="background:linear-gradient(90deg,${rampCSS(RAMP)})"></div>
        <div class="hb-ticks"><span>${lo}°</span><span>${((lo + hi) / 2).toFixed(0)}°</span><span>${hi}°C</span></div>`;
    }
    function renderBanner(m) {
      const run = new Date(m.run_time_utc);
      const ageH = (Date.now() - Date.parse(m.forecast_issue_time_utc)) / 3.6e6;
      const stale = ageH > 36;
      g("fcBanner").innerHTML =
        `<b>${stale ? "⚠ stale" : "updated"}</b> · issued ${fmtIssue(m.forecast_issue_time_utc)} · ` +
        `${ageH.toFixed(0)} h ago<br><span class="fc-b-sub">${m.forecast_model.toUpperCase()} → ` +
        `${m.simulation_name} · ${m.n_hours} h horizon</span>`;
      g("fcBanner").classList.toggle("is-stale", stale);
    }
    function renderCaveats(cav) {
      const wrap = g("fcCaveatsWrap");
      if (!cav || !cav.length) { wrap.hidden = true; return; }
      wrap.hidden = false;
      g("fcCaveats").innerHTML = cav.map((c) => `<li>${c}</li>`).join("");
    }

    function fmtIssue(iso) {
      const d = new Date(iso);
      return d.toISOString().slice(0, 16).replace("T", " ") + "Z";
    }

    return { activate };
  })();
})();
