/* ============================================================
   Urban Climate Dashboard — EPW Files tab
   World map + three bubbles (Dublin / Patras / Wellington).
   Each "picker" (File A, and optionally File B) is a clone of
   #epwPickTpl bound to one cursor:
     Historical -> AMY (+year) | TMY | DSY        (TMY/DSY: no year)
     Future     -> SSP -> period -> year-type
                   XTMY/XDSY -> metric (+info) + return period
     then rural / LCZ-urban variant
   Analysis (below the map):
     · tall full-width hourly heatmap, hour on Y, day of year on X
     · seasonal cycle (monthly p10-p90 band + median) — left
     · key statistics (min/p1/median/p99/max)        — right
   "Compare with a second file" reveals File B's picker inline;
   every panel then overlays A (warm) and B (cyan).
   Bottom: a large "Request your custom EPW files" button ->
   config-YAML form (lat/lon editable, SSP only, download-only).
   ============================================================ */
(() => {
  "use strict";

  const CATALOG = "/data/epw/catalog.json";
  const DL = (p) => "/api/epw/download?path=" + encodeURIComponent(p);
  const HEAT = (h) => "/data/epw/" + h;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // approx. day-of-year at the 1st of each month (non-leap)
  const MONTH_DOY = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
  const g = (id) => document.getElementById(id);
  const qs = (sel, root) => (root || document).querySelector(sel);
  const qsa = (sel, root) => [...(root || document).querySelectorAll(sel)];

  const S = {
    started: false, map: null, cat: null, city: null,
    variable: "tdb", compare: false,
    pickers: {},                 // slot -> Picker instance
  };

  // ---- boot: tab wiring -----------------------------------
  qsa("#tabs .tab").forEach((t) => {
    t.addEventListener("click", () => {
      const which = t.dataset.tab;
      const st = g("epwStage");
      if (!st) return;
      st.classList.toggle("stage-hidden", which !== "epw");
      if (which === "epw") {
        g("stage").classList.add("stage-hidden");
        g("fcStage").classList.add("stage-hidden");
        const hl = g("hexLink"); if (hl) hl.style.display = "none";
        activate();
      }
    });
  });

  async function activate() {
    if (!S.started) { S.started = true; await boot(); }
    if (S.map) setTimeout(() => S.map.resize(), 60);
  }

  async function boot() {
    try { S.cat = await fetch(CATALOG).then((r) => r.json()); }
    catch (e) { g("epwMapHint").textContent = "EPW catalogue unavailable — run pipeline/build_epw.py"; return; }
    renderCityNav();
    buildMap();
    S.pickers.A = new Picker("A", g("epwPickerA"));
    S.pickers.B = new Picker("B", g("epwPickerB"));
    g("epwCompareBtn").addEventListener("click", toggleCompare);
    g("epwDetailClose").addEventListener("click", () => {
      S.pickers.A.sel = S.pickers.B.sel = null;
      closeDetail();
    });
    wireModal();
  }

  // ---- world map -----------------------------------------
  function buildMap() {
    const style = {
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
        { id: "bg", type: "background", paint: { "background-color": "#070a0e" } },
        { id: "osm", type: "raster", source: "osm",
          paint: { "raster-opacity": 0.5, "raster-saturation": -0.7,
                   "raster-brightness-max": 0.55 } },
      ],
    };
    const map = new maplibregl.Map({
      container: "epwMap", style, center: [40, 25], zoom: 1.4,
      dragRotate: false, attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    S.map = map;
    S.markers = {};
    map.on("load", () => {
      for (const c of S.cat.cities) {
        const elm = document.createElement("div");
        elm.className = "epw-bubble";
        elm.innerHTML = `<span class="epw-bubble-label">${c.label}</span>`;
        elm.addEventListener("click", (ev) => { ev.stopPropagation(); selectCity(c.id, true); });
        S.markers[c.id] = new maplibregl.Marker({ element: elm, anchor: "center" })
          .setLngLat(c.center).addTo(map);
      }
    });
  }

  function renderCityNav() {
    g("epwCityNav").innerHTML = S.cat.cities
      .map((c) => `<button data-city="${c.id}">${c.label}</button>`).join("");
    qsa("#epwCityNav button").forEach((b) =>
      b.addEventListener("click", () => selectCity(b.dataset.city, true)));
  }

  function cityObj() { return S.cat.cities.find((c) => c.id === S.city); }

  function selectCity(id, fly) {
    S.city = id;
    const c = cityObj();
    if (!c) return;
    S.compare = false;
    g("epwCompareBtn").setAttribute("aria-pressed", "false");
    g("epwPickerB").hidden = true;

    Object.entries(S.markers).forEach(([cid, m]) =>
      m.getElement().classList.toggle("is-active", cid === id));
    qsa("#epwCityNav button").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.city === id));
    if (fly && S.map) S.map.flyTo({ center: c.center, zoom: 8, duration: 900 });
    g("epwMapHint").textContent = `${c.label} · build a selection in the panel`;

    g("epwPickerA").hidden = false;
    S.pickers.A.reset(true);
    S.pickers.B.reset(false);
    g("epwCompareWrap").hidden = true;   // shown once A resolves
    closeDetail();
  }

  function toggleCompare() {
    S.compare = !S.compare;
    g("epwCompareBtn").setAttribute("aria-pressed", S.compare ? "true" : "false");
    g("epwPickerB").hidden = !S.compare;
    if (S.compare) {
      S.pickers.B.cloneFrom(S.pickers.A);   // start B from A's selection
    } else {
      S.pickers.B.sel = null;
      render();
    }
  }

  /* ======================================================
     Picker — one cursor + one cloned template subtree.
     ====================================================== */
  function Picker(slot, host) {
    this.slot = slot;
    this.host = host;
    this.cur = freshCur();
    const tpl = g("epwPickTpl").content.cloneNode(true);
    host.appendChild(tpl);
    this.el = {
      slotLabel: qs(".ep-slot-label", host),
      branchSeg: qs('[data-role="branch"]', host),
      histBox: qs('[data-branch="historical"]', host),
      futBox: qs('[data-branch="future"]', host),
      histType: qs('[data-role="histType"]', host),
      histNote: qs('[data-role="histNote"]', host),
      amyYearWrap: qs('[data-role="amyYearWrap"]', host),
      amyYear: qs('[data-role="amyYear"]', host),
      ssp: qs('[data-role="ssp"]', host),
      period: qs('[data-role="period"]', host),
      yearType: qs('[data-role="yearType"]', host),
      metricWrap: qs('[data-role="metricWrap"]', host),
      metric: qs('[data-role="metric"]', host),
      metricInfo: qs('[data-role="metricInfo"]', host),
      metricPop: qs('[data-role="metricPop"]', host),
      ret: qs('[data-role="return"]', host),
      windowNote: qs('[data-role="windowNote"]', host),
      variant: qs('[data-role="variant"]', host),
      download: qs('[data-role="download"]', host),
    };
    this.el.slotLabel.textContent = slot === "A" ? "Weather file" : "Second file (B)";
    this.sel = null;
    this._wire();
  }

  function freshCur() {
    return { branch: "historical", histType: null, amyYear: null,
             ssp: null, period: null, yearType: null,
             metric: null, ret: null, variant: "base" };
  }

  Picker.prototype._wire = function () {
    const E = this.el, self = this;
    qsa("button", E.branchSeg).forEach((b) =>
      b.addEventListener("click", () => { self.cur.branch = b.dataset.b; self.syncBranch(); }));
    E.histType.addEventListener("change", () => { self.cur.histType = E.histType.value; self.syncHist(); });
    E.amyYear.addEventListener("change", () => { self.cur.amyYear = +E.amyYear.value; self.resolve(); });
    E.ssp.addEventListener("change", () => { self.cur.ssp = E.ssp.value; self.cur.period = null; self.syncFuture(); });
    E.period.addEventListener("change", () => { self.cur.period = E.period.value; self.cur.yearType = null; self.syncFuture(); });
    E.yearType.addEventListener("change", () => { self.cur.yearType = E.yearType.value; self.cur.metric = null; self.syncFuture(); });
    E.metric.addEventListener("change", () => { self.cur.metric = E.metric.value; self.syncMetric(); });
    E.ret.addEventListener("change", () => { self.cur.ret = +E.ret.value; self.resolve(); });
    E.metricInfo.addEventListener("click", () => { E.metricPop.hidden = !E.metricPop.hidden; });
  };

  Picker.prototype.reset = function (build) {
    this.cur = freshCur();
    this.sel = null;
    if (build) this.syncBranch();
  };

  Picker.prototype.cloneFrom = function (other) {
    this.cur = JSON.parse(JSON.stringify(other.cur));
    this.syncBranch();
  };

  Picker.prototype.syncBranch = function () {
    const E = this.el, b = this.cur.branch;
    qsa("button", E.branchSeg).forEach((x) => x.classList.toggle("is-active", x.dataset.b === b));
    E.histBox.hidden = b !== "historical";
    E.futBox.hidden = b !== "future";
    if (b === "historical") this.initHist(); else this.initFuture();
  };

  Picker.prototype.initHist = function () {
    const E = this.el, h = cityObj().historical || {};
    const opts = [];
    if (h.AMY) opts.push(["AMY", h.AMY.label]);
    if (h.TMY) opts.push(["TMY", h.TMY.label]);
    if (h.DSY) opts.push(["DSY", h.DSY.label]);
    E.histType.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
    if (!this.cur.histType || !opts.some(([v]) => v === this.cur.histType))
      this.cur.histType = opts.length ? opts[0][0] : null;
    E.histType.value = this.cur.histType || "";
    this.syncHist();
  };

  Picker.prototype.syncHist = function () {
    const E = this.el, h = cityObj().historical || {}, t = this.cur.histType;
    const isAmy = t === "AMY";
    E.amyYearWrap.hidden = !isAmy;
    if (isAmy && h.AMY) {
      const years = h.AMY.years.map((y) => y.year);
      E.amyYear.innerHTML = years.map((y) => `<option value="${y}">${y}</option>`).join("");
      if (!this.cur.amyYear || !years.includes(this.cur.amyYear))
        this.cur.amyYear = years[years.length - 1];
      E.amyYear.value = this.cur.amyYear;
    }
    E.histNote.textContent =
      (t === "TMY" && h.TMY) ? `Typical year, fixed aggregation window ${h.TMY.period}.`
      : (t === "DSY" && h.DSY) ? `Design summer year, reference period ${h.DSY.period}.`
      : (t === "AMY") ? "One real historical year." : "";
    this.resolve();
  };

  Picker.prototype.initFuture = function () {
    const E = this.el, fut = cityObj().future || [];
    E.ssp.innerHTML = fut.map((s) => `<option value="${s.id}">${s.label}</option>`).join("");
    if (!this.cur.ssp || !fut.some((s) => s.id === this.cur.ssp))
      this.cur.ssp = fut.length ? fut[0].id : null;
    E.ssp.value = this.cur.ssp || "";
    this.syncFuture();
  };

  Picker.prototype._ssp = function () { return (cityObj().future || []).find((s) => s.id === this.cur.ssp); };
  Picker.prototype._period = function () { const s = this._ssp(); return s && s.periods.find((p) => p.id === this.cur.period); };
  Picker.prototype._yt = function () { const p = this._period(); return p && p.yearTypes.find((y) => y.id === this.cur.yearType); };
  Picker.prototype._metric = function () { const y = this._yt(); return y && (y.metrics || []).find((m) => m.id === this.cur.metric); };

  Picker.prototype.syncFuture = function () {
    const E = this.el, s = this._ssp();
    if (!s) return;
    E.period.innerHTML = s.periods.map((p) => `<option value="${p.id}">${p.label}</option>`).join("");
    if (!this.cur.period || !s.periods.some((p) => p.id === this.cur.period)) this.cur.period = s.periods[0].id;
    E.period.value = this.cur.period;
    const p = this._period();
    E.yearType.innerHTML = p.yearTypes.map((y) => `<option value="${y.id}">${y.label}</option>`).join("");
    if (!this.cur.yearType || !p.yearTypes.some((y) => y.id === this.cur.yearType)) this.cur.yearType = p.yearTypes[0].id;
    E.yearType.value = this.cur.yearType;
    const y = this._yt();
    const isX = !!(y && y.metrics);
    E.metricWrap.hidden = !isX;
    if (isX) {
      E.metric.innerHTML = y.metrics.map((m) => `<option value="${m.id}">${m.label}</option>`).join("");
      if (!this.cur.metric || !y.metrics.some((m) => m.id === this.cur.metric)) this.cur.metric = y.metrics[0].id;
      E.metric.value = this.cur.metric;
      this.syncMetric();
    } else {
      this.resolve();
    }
  };

  Picker.prototype.syncMetric = function () {
    const E = this.el, m = this._metric();
    if (!m) return;
    E.metricPop.textContent = m.info || "";
    E.metricPop.hidden = true;
    const avail = m.returnPeriodsAvailable || [m.returnPeriod];
    E.ret.innerHTML = [10, 20, 50].map((v) =>
      `<option value="${v}" ${avail.includes(v) ? "" : "disabled"}>${v} yr${avail.includes(v) ? "" : " — n/a"}</option>`).join("");
    if (!avail.includes(this.cur.ret)) this.cur.ret = m.returnPeriod;
    E.ret.value = this.cur.ret;
    E.windowNote.textContent = m.window
      ? `Heatwave window spliced in: ${m.window[0]} → ${m.window[1]}.` : "";
    this.resolve();
  };

  Picker.prototype._leafVariants = function () {
    const p = this.cur, c = cityObj();
    if (p.branch === "historical") {
      const h = c.historical || {};
      if (p.histType === "AMY") {
        const y = (h.AMY.years || []).find((x) => x.year === p.amyYear);
        return y ? y.variants : null;
      }
      return h[p.histType] ? h[p.histType].variants : null;
    }
    const y = this._yt();
    if (!y) return null;
    if (y.metrics) { const m = this._metric(); return m ? m.variants : null; }
    return y.variants;
  };

  Picker.prototype.resolve = function () {
    const E = this.el, vs = this._leafVariants();
    this._renderVariantSeg(vs);
    if (!vs || !vs.length) { this.sel = null; E.download.hidden = true; render(); return; }
    const v = vs.find((x) => x.id === this.cur.variant) || vs[0];
    this.cur.variant = v.id;
    this.sel = { label: this.describe(), variant: v };
    E.download.href = DL(v.path);
    E.download.hidden = false;
    if (this.slot === "A") g("epwCompareWrap").hidden = false;
    render();
  };

  Picker.prototype._renderVariantSeg = function (vs) {
    const seg = this.el.variant, self = this;
    if (!vs || !vs.length) { seg.innerHTML = ""; return; }
    seg.innerHTML = vs.map((v) =>
      `<button data-v="${v.id}" class="${v.id === self.cur.variant ? "is-active" : ""}">${v.label}</button>`).join("");
    qsa("button", seg).forEach((b) =>
      b.addEventListener("click", () => { self.cur.variant = b.dataset.v; self.resolve(); }));
  };

  Picker.prototype.describe = function () {
    const p = this.cur, bits = [cityObj().label];
    if (p.branch === "historical") {
      bits.push("Historical", p.histType);
      if (p.histType === "AMY") bits.push(String(p.amyYear));
    } else {
      const s = this._ssp();
      bits.push(s ? s.label : p.ssp);
      const per = this._period();
      bits.push(per ? per.label : p.period);
      bits.push(p.yearType);
      if (p.metric) bits.push(`${p.metric} · ${p.ret}yr`);
    }
    return bits.join(" · ");
  };

  /* ======================================================
     Analysis render
     ====================================================== */
  function render() {
    const A = S.pickers.A && S.pickers.A.sel;
    if (!A) { closeDetail(); return; }
    const B = S.compare ? (S.pickers.B && S.pickers.B.sel) : null;

    const firstOpen = g("epwDetail").hidden;
    g("epwDetail").hidden = false;
    g("epwStage").classList.add("has-detail");
    if (S.map) requestAnimationFrame(() => S.map.resize());

    g("epwDhKicker").textContent = cityObj().label;
    g("epwDhTitle").textContent = B ? "Comparison" : A.variant.name;
    g("epwDhBody").textContent = B ? `${A.label}   vs   ${B.label}` : A.label;

    g("epwLegend").hidden = !B;
    if (B) {
      g("epwLegA").textContent = shortLabel(A);
      g("epwLegB").textContent = shortLabel(B);
    }

    renderVarSeg(A, B);
    renderHeat(A, B);
    renderSeasonal(A, B);
    renderStats(A, B);

    if (firstOpen) g("epwStage").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function shortLabel(sel) {
    return sel.label.split(" · ").slice(1).join(" · ") || sel.variant.label;
  }

  function renderVarSeg(A, B) {
    const vars = S.cat.vars;
    const avail = Object.keys(vars).filter((v) =>
      A.variant.stats[v] && (!B || B.variant.stats[v]));
    if (!avail.includes(S.variable)) S.variable = avail[0];
    g("epwVarSeg").innerHTML = avail.map((v) =>
      `<button data-v="${v}" class="${v === S.variable ? "is-active" : ""}">${vars[v].label}</button>`).join("");
    qsa("#epwVarSeg button").forEach((b) =>
      b.addEventListener("click", () => {
        S.variable = b.dataset.v;
        qsa("#epwVarSeg button").forEach((x) => x.classList.toggle("is-active", x === b));
        renderHeat(A, B); renderSeasonal(A, B); renderStats(A, B);
      }));
  }

  // ----- heatmap(s): PNG is 365 wide x 24 tall (hour on Y) -----
  function renderHeat(A, B) {
    const v = S.variable, vs = S.cat.vars[v];
    g("epwHeatVar").textContent = `${vs.label} (${vs.unit})`;
    g("epwSeasonVar").textContent = `${vs.label} (${vs.unit})`;
    const rows = [["A", A]];
    if (B) rows.push(["B", B]);
    g("epwHeatStack").innerHTML = rows.map(([tag, sel]) => `
      <div class="epw-heatrow">
        ${B ? `<span class="ehr-tag">${tag}</span>` : ""}
        <div class="ehr-yaxis"><span>00h</span><span>06h</span><span>12h</span><span>18h</span><span>24h</span></div>
        <div class="ehr-plot">
          <img src="${HEAT(sel.variant.heat[v])}" alt="${tag} annual ${v} field, hour by day" />
        </div>
        <div class="ehr-xaxis">
          <span>Jan</span><span>Mar</span><span>May</span><span>Jul</span><span>Sep</span><span>Nov</span>
        </div>
      </div>`).join("");

    const [lo, hi] = cityObj().varRange[v] || [0, 1];
    const ramp = S.cat.ramps[S.cat.varRampKey[v]];
    const grad = ramp.map((c, i) =>
      `rgb(${c.join(",")}) ${((i / (ramp.length - 1)) * 100).toFixed(0)}%`).join(", ");
    g("epwHeatBar").style.background = `linear-gradient(90deg, ${grad})`;
    g("epwHeatTicks").innerHTML =
      `<span>${fmt(lo)} ${vs.unit}</span><span>${fmt((lo + hi) / 2)}</span><span>${fmt(hi)} ${vs.unit}</span>`;
  }

  // ----- seasonal cycle: monthly p10-p90 band + median -----
  function renderSeasonal(A, B) {
    const v = S.variable;
    const W = 640, H = 300, padL = 46, padR = 12, padT = 14, padB = 26;
    const mA = A.variant.monthly[v] || [];
    const mB = B ? (B.variant.monthly[v] || []) : null;
    const flat = [];
    [mA, mB].forEach((m) => m && m.forEach((o) => o && flat.push(o.p10, o.p90)));
    if (!flat.length) { g("epwMonthly").innerHTML = ""; return; }
    let lo = Math.min(...flat), hi = Math.max(...flat);
    const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
    const x = (i) => padL + (i / 11) * (W - padL - padR);
    const y = (val) => padT + (1 - (val - lo) / (hi - lo)) * (H - padT - padB);

    const parts = [];
    for (const tv of niceTicks(lo, hi, 5)) {
      parts.push(`<line class="em-grid" x1="${padL}" y1="${y(tv).toFixed(1)}" x2="${W - padR}" y2="${y(tv).toFixed(1)}"/>`);
      parts.push(`<text x="${padL - 6}" y="${(y(tv) + 3).toFixed(1)}" text-anchor="end">${fmt(tv)}</text>`);
    }
    MONTHS.forEach((mo, i) => {
      if (i % 2 === 0) parts.push(`<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${mo}</text>`);
    });

    const band = (m, cls) => {
      const pts = m.map((o, i) => o ? [i, o] : null).filter(Boolean);
      if (pts.length < 2) return;
      let up = "", dn = "";
      pts.forEach(([i, o]) => { up += `${up ? "L" : "M"}${x(i).toFixed(1)} ${y(o.p90).toFixed(1)} `; });
      for (let k = pts.length - 1; k >= 0; k--) {
        const [i, o] = pts[k];
        dn += `L${x(i).toFixed(1)} ${y(o.p10).toFixed(1)} `;
      }
      parts.push(`<path class="${cls}" d="${up}${dn}Z"/>`);
    };
    const median = (m, cls, dot) => {
      let d = "", pen = false;
      m.forEach((o, i) => {
        if (!o) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(o.p50).toFixed(1)} `;
        pen = true;
      });
      parts.push(`<path class="${cls}" d="${d.trim()}"/>`);
      m.forEach((o, i) => {
        if (o) parts.push(`<circle class="${dot}" cx="${x(i).toFixed(1)}" cy="${y(o.p50).toFixed(1)}" r="2.6"/>`);
      });
    };
    band(mA, "em-band-a");
    if (mB) band(mB, "em-band-b");
    median(mA, "em-a", "em-dot-a");
    if (mB) median(mB, "em-b", "em-dot-b");
    g("epwMonthly").innerHTML = parts.join("");
  }

  // ----- key statistics: min / p1 / median / p99 / max -----
  const STAT_KEYS = ["min", "p1", "median", "p99", "max"];
  function renderStats(A, B) {
    const v = S.variable;
    const sA = A.variant.stats[v], sB = B ? B.variant.stats[v] : null;
    g("epwStatsRef").textContent = B ? "· A vs B" : "";
    const unit = S.cat.vars[v].unit;
    g("epwStats").innerHTML = STAT_KEYS.map((k) => {
      if (sA[k] == null) return "";
      let rows;
      if (sB && sB[k] != null) {
        rows =
          `<div class="es-row"><span class="esk">A</span><span class="esv-a">${sA[k]} ${unit}</span></div>` +
          `<div class="es-row"><span class="esk">B</span><span class="esv-b">${sB[k]} ${unit}</span></div>` +
          `<div class="es-row"><span class="esk">Δ</span><span class="esv">${fmt(sB[k] - sA[k], true)} ${unit}</span></div>`;
      } else {
        rows = `<div class="es-row"><span class="esk"></span><span class="esv">${sA[k]} ${unit}</span></div>`;
      }
      return `<div class="es-item"><div class="es-label">${k.toUpperCase()}</div>${rows}</div>`;
    }).join("");
  }

  function niceTicks(lo, hi, want) {
    const span = hi - lo, raw = span / want;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-6; v += step) out.push(Math.round(v * 100) / 100);
    return out;
  }
  function fmt(v, signed) {
    const r = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10;
    return (signed && r > 0 ? "+" : "") + r;
  }

  function closeDetail() {
    g("epwDetail").hidden = true;
    g("epwStage").classList.remove("has-detail");
    if (S.map) requestAnimationFrame(() => S.map.resize());
  }

  /* ======================================================
     Request form — config-YAML, download-only.
     lat/lon editable, SSP only (no GWL), TMY window fixed.
     ====================================================== */
  const ASHRAE_DEFAULT = "4A";

  function wireModal() {
    g("epwRequestBtn").addEventListener("click", openModal);
    g("epwModalClose").addEventListener("click", () => g("epwModal").hidden = true);
    g("epwModal").addEventListener("click", (e) => { if (e.target === g("epwModal")) g("epwModal").hidden = true; });

    multiChips("#rqSsp");
    multiChips("#rqHist", (on) => {
      g("rqAmyWrap").hidden = !on.has("AMY");
      g("rqTmyNote").hidden = !on.has("TMY");
    });
    multiChips("#rqFuture", (on) => { g("rqXmyWrap").hidden = !on.has("XMY"); });
    g("rqUhiRow").addEventListener("click", () => {
      const on = g("rqUhiRow").getAttribute("aria-pressed") !== "true";
      g("rqUhiRow").setAttribute("aria-pressed", on ? "true" : "false");
      g("rqLczWrap").hidden = !on;
    });
    g("rqDownload").addEventListener("click", downloadYaml);
  }

  function multiChips(sel, cb) {
    const root = qs(sel);
    qsa("button", root).forEach((b) =>
      b.addEventListener("click", () => {
        b.classList.toggle("is-on");
        if (cb) cb(new Set(qsa("button.is-on", root).map((x) => x.dataset.v)));
      }));
  }

  function openModal() {
    const c = cityObj();
    const years = c && c.historical && c.historical.AMY
      ? c.historical.AMY.years.map((y) => y.year)
      : Array.from({ length: 66 }, (_, i) => 2024 - i);
    g("rqAmyYear").innerHTML = years.map((y) => `<option>${y}</option>`).join("");
    if (c) {
      g("rqLat").value = c.center[1].toFixed(4);
      g("rqLon").value = c.center[0].toFixed(4);
    }
    g("epwModal").hidden = false;
  }

  function chipVals(sel) { return qsa(sel + " button.is-on").map((b) => b.dataset.v); }

  function buildConfig() {
    const name = g("rqName").value.trim() || "MyProject";
    const lat = parseFloat(g("rqLat").value), lon = parseFloat(g("rqLon").value);
    const ssp = chipVals("#rqSsp");
    const fStart = +g("rqFStart").value, fEnd = +g("rqFEnd").value;
    const hist = chipVals("#rqHist"), fut = chipVals("#rqFuture");
    const uhiOn = g("rqUhiRow").getAttribute("aria-pressed") === "true";

    const histCfg = {};
    if (hist.includes("TMY")) histCfg.TMY = { start_year: 1991, end_year: 2020 };
    if (hist.includes("AMY")) histCfg.AMY = +g("rqAmyYear").value;
    const futCfg = { period: [fStart, fEnd] };
    if (fut.includes("TMY")) futCfg.TMY = true;
    if (fut.includes("XMY")) futCfg.XMY = { return_period: +g("rqReturn").value, event_type: "Heatwave", metric: g("rqMetric").value };

    const metric = fut.includes("XMY") ? g("rqMetric").value : "TX_max_7d";
    const ret = fut.includes("XMY") ? +g("rqReturn").value : 20;

    return {
      SIMULATION: `${name}_${ssp[0] || "ssp585"}_${fStart}_${fEnd}_epw`,
      CITY: { LAT: isFinite(lat) ? +lat.toFixed(4) : "/",
              LON: isFinite(lon) ? +lon.toFixed(4) : "/" },
      YEARS: { FIRST_FUTURE: fStart, LAST_FUTURE: fEnd },
      CMIP6: { SSP: ssp.length ? ssp : "/", intimeperiod: "YES" },
      EXTREME_SELECTION: { METHOD: metric, RETURN_PERIOD: ret },
      UHI: { ENABLED: uhiOn ? "YES" : "NO",
             LCZ_URBAN: uhiOn ? g("rqLcz").value : "/",
             ASHRAE_CLASS: uhiOn ? ASHRAE_DEFAULT : "/" },
      CLIENT_NOTES: g("rqNotes").value.trim() || "/",
      CLIENT_EXPORT: { HISTORICAL_EPW: histCfg, FUTURE_EPW: futCfg },
    };
  }

  function toYaml(obj, indent) {
    indent = indent || 0;
    const pad = "  ".repeat(indent);
    if (Array.isArray(obj)) {
      if (!obj.length) return "[]";
      return "\n" + obj.map((v) => `${pad}- ${scalar(v)}`).join("\n");
    }
    if (obj && typeof obj === "object") {
      const keys = Object.keys(obj);
      if (!keys.length) return "{}";
      return "\n" + keys.map((k) => {
        const v = obj[k];
        if ((v && typeof v === "object" && !Array.isArray(v)) ||
            (Array.isArray(v) && v.length && typeof v[0] === "object"))
          return `${pad}${k}:${toYaml(v, indent + 1)}`;
        if (Array.isArray(v)) return `${pad}${k}: [${v.map(scalar).join(", ")}]`;
        return `${pad}${k}: ${scalar(v)}`;
      }).join("\n");
    }
    return scalar(obj);
  }
  function scalar(v) {
    if (v === null || v === undefined) return "/";
    return typeof v === "string" ? v : String(v);
  }

  function downloadYaml() {
    const cfg = buildConfig();
    const head =
      "###############################################################################\n" +
      `# Generated: ${new Date().toISOString().slice(0, 19).replace("T", " ")}\n` +
      "# Urban Climate Dashboard — EPW request\n" +
      "###############################################################################\n";
    const body = Object.keys(cfg).map((k) => {
      const v = cfg[k];
      return (v && typeof v === "object") ? `${k}:${toYaml(v, 1)}` : `${k}: ${scalar(v)}`;
    }).join("\n");
    const blob = new Blob([head + "\n" + body + "\n"], { type: "text/yaml" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `config_${(g("rqName").value.trim() || "request")}.yaml`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }
})();
