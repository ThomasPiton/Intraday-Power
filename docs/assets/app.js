/* Intraday Power v2 front-end: vanilla JS + ECharts, reads docs/data/*.json and docs/content/*.json.
   No build step. Pages are rendered into <main id="page"> from the hash route. */
(() => {
  "use strict";

  const TZ = "Europe/Paris";
  const PAGES = [
    { id: "today", label: "Today", icon: "M3 12l9-8 9 8v8a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" },
    { id: "demand", label: "Demand", short: "Demand", icon: "M14 14.8V4a2 2 0 1 0-4 0v10.8a4 4 0 1 0 4 0zM12 2v0" },
    { id: "supply", label: "Supply", icon: "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8z" },
    { id: "drivers", label: "Price Drivers", short: "Prices", icon: "M3 17l5-6 4 3 6-8 3 3M3 21h18" },
    { id: "data", label: "Data Hub", short: "Data", icon: "M4 6h16M4 12h16M4 18h10" },
    { id: "flex", label: "Flexibility", short: "Flex", icon: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4" },
    { id: "learn", label: "Learn", icon: "M4 5a2 2 0 0 1 2-2h12v16H6a2 2 0 0 0-2 2zM8 7h6" },
  ];
  const BOTTOM = ["today", "demand", "supply", "drivers", "data", "flex"];
  const ZN = { FR: "France", "DE-LU": "Germany" };
  const ZS = { FR: "FR", "DE-LU": "DE" };

  const S = { country: "FR", page: null, charts: {}, cache: {}, ui: {} };
  try { S.country = localStorage.getItem("ip-country") || "FR"; } catch (e) { /* private mode */ }
  if (!ZN[S.country]) S.country = "FR";

  // ------------------------------------------------------------------ formatting
  const nf = (d) => new Intl.NumberFormat("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);
  const fmt = (x, d = 0) => (isNum(x) ? nf(d).format(x).replace("-", "−") : "–");
  const signed = (x, d = 0) => (isNum(x) ? (x > 0 ? "+" : x < 0 ? "−" : "±") + nf(d).format(Math.abs(x)) : "–");
  const chgHTML = (x, d = 0, unit = "") => (isNum(x) ? `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${signed(x, d)}${unit ? " " + unit : ""}</span>` : "");
  const pct = (x, d = 0) => (isNum(x) ? nf(d).format(100 * x) + "%" : "–");
  const tf = (iso, o) => (iso ? new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...o }).format(new Date(iso)) : "–");
  const hm = (iso) => tf(iso, { hour: "2-digit", minute: "2-digit" });
  const dmy = (iso) => tf(iso, { day: "numeric", month: "short", year: "numeric" });
  const dshort = (iso) => tf(iso, { weekday: "short", day: "numeric", month: "short" });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const ago = (iso) => {
    if (!iso) return "–";
    const m = (Date.now() - new Date(iso)) / 60000;
    if (m < 0) return "until " + dshort(iso);
    if (m < 90) return Math.round(m) + " min ago";
    if (m < 48 * 60) return Math.round(m / 60) + " h ago";
    return dmy(iso);
  };
  const zip = (a, b) => a.map((x, i) => [x, b ? b[i] : null]);

  // ------------------------------------------------------------------ data
  function load(path) {
    if (!S.cache[path]) {
      const v = S.version || Date.now();
      S.cache[path] = fetch(`${path}?v=${encodeURIComponent(v)}`, { cache: "no-cache" })
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    }
    return S.cache[path];
  }
  const data = (n) => load(`data/${n}.json`);
  const content = (n) => load(`content/${n}.json`);

  // ------------------------------------------------------------------ theme tokens for charts
  function tok() {
    const cs = getComputedStyle(document.documentElement);
    const g = (k) => cs.getPropertyValue(k).trim();
    return { text: g("--text"), muted: g("--muted"), muted2: g("--muted-2"), line: g("--line"), panel: g("--panel"),
             panel2: g("--panel-2"), accent: g("--accent"), pos: g("--pos"), neg: g("--neg"),
             s1: g("--s1"), s2: g("--s2"), s3: g("--s3"), s4: g("--s4"), s5: g("--s5") };
  }
  const alpha = (hex, a) => {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };

  function chart(id, opt, group) {
    const el = document.getElementById(id);
    if (!el || typeof echarts === "undefined") return null;
    let c = S.charts[id];
    if (!c || c.isDisposed()) { el.innerHTML = ""; c = echarts.init(el, null, { renderer: "canvas" }); S.charts[id] = c; }
    c.setOption(opt, true);
    if (group) { c.group = group; echarts.connect(group); }
    return c;
  }
  function disposeAll() { Object.values(S.charts).forEach((c) => c && !c.isDisposed() && c.dispose()); S.charts = {}; }
  window.addEventListener("resize", () => Object.values(S.charts).forEach((c) => c && !c.isDisposed() && c.resize()));

  function base(t, extra = {}) {
    return {
      animation: false,
      textStyle: { fontFamily: "Inter, system-ui, sans-serif", color: t.muted, fontSize: 11 },
      grid: { left: 4, right: 12, top: 14, bottom: 4, containLabel: true },
      tooltip: { trigger: "axis", confine: true, backgroundColor: t.panel, borderColor: t.line, borderWidth: 1,
                 padding: [8, 10], textStyle: { color: t.text, fontSize: 12 }, extraCssText: "border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.12);",
                 axisPointer: { type: "line", lineStyle: { color: t.muted, width: 1 } } },
      ...extra,
    };
  }
  const xTime = (t, o = {}) => ({ type: "time", splitNumber: window.innerWidth < 600 ? 3 : 6, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false },
    axisLabel: { color: t.muted, hideOverlap: true, ...(o.label || {}) }, ...o.extra });
  const yVal = (t, o = {}) => ({ type: "value", scale: !!o.scale, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: t.muted, formatter: o.fmt }, min: o.min, max: o.max, name: o.name, nameTextStyle: { color: t.muted, align: "left" } });
  const grad = (c) => new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: alpha(c, 0.22) }, { offset: 1, color: alpha(c, 0) }]);
  const line = (name, d, color, o = {}) => ({
    type: "line", name, data: d, showSymbol: false, connectNulls: false, step: o.step, z: o.z || 2,
    lineStyle: { width: o.width || 2, color, type: o.dash ? "dashed" : "solid", opacity: o.opacity ?? 1 },
    itemStyle: { color }, areaStyle: o.area ? { color: grad(color) } : undefined, emphasis: { disabled: true },
    markLine: o.markLine, endLabel: o.label ? { show: true, formatter: o.label, color: t0().muted2, fontSize: 11 } : undefined,
  });
  function band(name, lo, hi, color) {
    // P10-P90 band as one filled polygon per run of valid points (a custom series: stacking would
    // stack along x when both axes are numeric).
    const idx = lo.map((p, i) => (isNum(p[1]) && isNum(hi[i][1]) ? i : -1));
    const runs = [];
    let run = [];
    idx.forEach((i) => { if (i >= 0) run.push(i); else if (run.length) { runs.push(run); run = []; } });
    if (run.length) runs.push(run);
    const x0 = (p) => (typeof p[0] === "string" ? new Date(p[0]).getTime() : p[0]);
    return [{
      type: "custom", name, silent: true, tooltip: { show: false }, z: 1,
      data: [...lo, ...hi].filter((p, i) => idx[i % lo.length] >= 0).map((p) => [x0(p), p[1]]),   // both bounds, for the axis extent
      renderItem: (params, api) => {
        if (params.dataIndex !== 0) return null;
        return { type: "group", children: runs.map((r) => ({ type: "polygon", silent: true,
          shape: { points: [...r.map((i) => api.coord([x0(hi[i]), hi[i][1]])), ...r.slice().reverse().map((i) => api.coord([x0(lo[i]), lo[i][1]]))] },
          style: { fill: alpha(color, 0.16) } })) };
      },
    }];
  }
  let _t = null;
  const t0 = () => (_t || (_t = tok()));
  const nowLine = (t) => ({ symbol: "none", silent: true, label: { show: false }, lineStyle: { color: t.muted, type: "dashed", width: 1 },
                            data: [{ xAxis: new Date().toISOString() }] });

  // Tooltip: value first (strong), series name second; short line key in the series colour.
  function tip(units, decimals = 1, header = (p) => tf(p, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })) {
    return (params) => {
      const ps = (Array.isArray(params) ? params : [params]).filter((p) => p.seriesName && !p.seriesName.endsWith("_lo") && p.seriesType !== "custom");
      if (!ps.length) return "";
      const x = ps[0].value && Array.isArray(ps[0].value) ? ps[0].value[0] : ps[0].axisValue;
      const rows = ps.map((p) => {
        const v = Array.isArray(p.value) ? p.value[1] : p.value;
        const u = typeof units === "function" ? units(p) : units;
        return `<div style="display:flex;align-items:center;gap:8px;margin-top:3px"><span style="display:inline-block;width:12px;border-top:2px solid ${p.color}"></span><b>${fmt(v, decimals)}</b> <span style="opacity:.6">${esc(u)}</span> <span style="opacity:.75">${esc(p.seriesName)}</span></div>`;
      }).join("");
      return `<div style="opacity:.7;font-size:11px">${esc(header(x))}</div>${rows}`;
    };
  }
  const legend = (items) => `<div class="legend">${items.map(([label, color, kind]) =>
    `<span style="color:${color}"><i class="${kind || ""}"></i><span style="color:var(--muted-2)">${esc(label)}</span></span>`).join("")}</div>`;
  const noData = (msg = "No data yet.") => `<div class="nodata">${esc(msg)}</div>`;

  // ------------------------------------------------------------------ building blocks
  let EXPLAIN = {};
  function explain(id) {
    const e = EXPLAIN[id];
    if (!e) return "";
    const block = (title, html) => html ? `<details><summary>${title}</summary><div class="body">${html}</div></details>` : "";
    return `<div class="explain">${block("What am I looking at?", e.what)}${block("Why does a trader care?", e.why)}${block("How is it computed?", e.how)}</div>`;
  }
  const card = (o) => `<section class="card ${o.cls || ""}" ${o.id ? `id="${o.id}"` : ""}>
    <header><h2>${esc(o.title)}</h2>${o.sub ? `<span class="sub">${o.sub}</span>` : ""}${o.tools ? `<div class="tools">${o.tools}</div>` : ""}</header>
    ${o.body || ""}${o.explain ? explain(o.explain) : ""}</section>`;
  const seg = (id, opts, cur) => `<div class="seg" id="${id}" role="group">${opts.map(([v, l]) =>
    `<button data-v="${esc(v)}" aria-pressed="${String(v) === String(cur)}">${esc(l)}</button>`).join("")}</div>`;
  function onSeg(id, fn) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (!b) return;
      $$("button", el).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      fn(b.dataset.v);
    });
  }
  const head = (title, lede) => `<div class="page-head"><h1>${esc(title)}</h1>${lede ? `<p class="lede">${lede}</p>` : ""}</div>`;
  const tile = (k, v, s = "") => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  const hero = (label, big, unit, chg) => `<div class="hero"><div class="label">${label}</div><div class="big">${big}<small>${unit || ""}</small></div>${chg ? `<div class="chg">${chg}</div>` : ""}</div>`;

  // ================================================================== TODAY
  async function pageToday(root) {
    const [d] = await Promise.all([data("today")]);
    const z = S.country;
    const c = d && d.countries ? d.countries[z] || {} : {};
    const p = c.price;
    root.innerHTML = head("Today", `Day-ahead prices, demand and renewables in ${ZN[z]}, with a short rule-based brief for both countries.`) +
      (p ? hero(`${ZN[z]} day-ahead average today`, fmt(p.avg, 0), " €/MWh",
        isNum(p.yday_avg) ? `${chgHTML(p.avg - p.yday_avg, 0, "€/MWh")} <span class="muted">vs yesterday</span>` : "") : hero(`${ZN[z]} day-ahead average today`, "–", "", "")) +
      `<div class="grid two">` +
      card({ title: "Market brief", sub: "generated by rules, not by a language model", body: briefHTML(d), explain: "today-brief", cls: "span2" }) +
      card({ title: "Day-ahead price", sub: "€/MWh, quarter-hours, today and tomorrow when published", body: `<div id="c-price" class="chart h180"></div>`, explain: "today-price", cls: "span2" }) +
      `<div class="span2">${tilesToday(c)}</div>` +
      card({ title: "Consumption vs forecast", sub: "GW, today and tomorrow", body: legend([["Actual", t0().s1], ["Day-ahead forecast", t0().s2, "dash"]]) + `<div id="c-load" class="chart h180"></div>`, explain: "today-load" }) +
      card({ title: "Wind and solar", sub: "GW, today and tomorrow", body: legend([["Wind", t0().s1], ["Solar", t0().s4]]) + `<div id="c-res" class="chart h180"></div>`, explain: "today-res" }) +
      `</div>`;
    const t = t0();
    if (p && p.curve && p.curve.ts.length) {
      chart("c-price", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("€/MWh", 2) } }), xAxis: xTime(t), yAxis: yVal(t, { scale: true }),
        series: [line("DA price", zip(p.curve.ts, p.curve.v), t.accent, { area: true, step: "end", markLine: nowLine(t) })] });
    } else $("#c-price").innerHTML = noData("Day-ahead prices not available.");
    const L = c.load;
    if (L && L.ts.length) chart("c-load", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: xTime(t), yAxis: yVal(t, { scale: true }),
      series: [line("Actual", zip(L.ts, L.load), t.s1, { area: true, markLine: nowLine(t) }), line("Day-ahead forecast", zip(L.ts, L.fc_da), t.s2, { dash: true })] });
    else $("#c-load").innerHTML = noData();
    const R = c.res;
    if (R && R.ts.length) chart("c-res", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: xTime(t), yAxis: yVal(t, { min: 0 }),
      series: [line("Wind", zip(R.ts, R.wind), t.s1, { area: true, markLine: nowLine(t) }), line("Solar", zip(R.ts, R.solar), t.s4, { area: true })] });
    else $("#c-res").innerHTML = noData();
  }
  function briefHTML(d) {
    if (!d || !d.brief || !d.brief.length) return noData("No brief yet.");
    const items = d.brief.slice().sort((a, b) => (a.zone === S.country ? -1 : 0) - (b.zone === S.country ? -1 : 0));
    return `<ul class="brief">${items.map((b) => `<li><span class="tag">${b.zone ? ZS[b.zone] : "FR·DE"}</span><span>${esc(b.text)}</span></li>`).join("")}</ul>`;
  }
  function tilesToday(c) {
    const p = c.price || {};
    const ln = c.load_now;
    const tt = c.temp_today;
    return `<div class="tiles">` +
      tile("Peak hour", p.peak_hour ? `${hm(p.peak_hour)}` : "–", isNum(p.peak_hour_price) ? `${fmt(p.peak_hour_price, 0)} €/MWh` : "") +
      tile("Lowest quarter-hour", `${fmt(p.min, 0)}<small>€/MWh</small>`, p.trough_hour ? `around ${hm(p.trough_hour)}` : "") +
      tile("Negative quarter-hours", fmt(p.neg_qh), "today, day-ahead") +
      tile("Tomorrow average", isNum(p.tomorrow_avg) ? `${fmt(p.tomorrow_avg, 0)}<small>€/MWh</small>` : "–", isNum(p.tomorrow_avg) ? chgHTML(p.tomorrow_avg - p.avg, 0) + " vs today" : "published ~12:45 CET") +
      tile("Consumption now", ln ? `${fmt(ln.gw, 1)}<small>GW</small>` : "–", ln && isNum(ln.fc_da_gw) ? `${chgHTML(ln.gw - ln.fc_da_gw, 1, "GW")} vs D-1 forecast · ${hm(ln.ts)}` : "") +
      tile("Wind + solar share", pct(c.res_share_today), "of consumption so far today") +
      tile("Temperature today", tt ? `${fmt(tt.temp, 1)}<small>°C</small>` : "–", tt ? `${chgHTML(tt.temp - tt.normal, 1, "°C")} vs normal` : "") +
      tile("Next 7 days", isNum(c.next7_anomaly) ? `${signed(c.next7_anomaly, 1)}<small>°C</small>` : "–", "vs normal, forecast") +
      `</div>`;
  }

  // ================================================================== DEMAND
  async function pageDemand(root) {
    const d = await data("demand");
    const z = S.country;
    const c = d && d.countries ? d.countries[z] || {} : {};
    const sh = c.slope_heating;
    const t = t0();
    root.innerHTML = head("Weather → Demand", `How temperature moves electricity consumption in ${ZN[z]}, and how that compares with the official forecasts.`) +
      hero(`Heating sensitivity, ${ZN[z]}`, sh ? fmt(-sh.slope, 2) : "–", " GW per °C",
        sh ? `<span class="muted">Each degree colder adds about ${fmt(-sh.slope, 1)} GW on working days below ${d.heating_below_c} °C · R² ${fmt(sh.r2, 2)} · ${fmt(sh.n)} days</span>` : `<span class="muted">Needs at least 30 cold working days of history.</span>`) +
      `<div class="grid">` +
      card({ title: "Temperature vs consumption", sub: "daily means, working days", body:
        legend([["Winter", t.s1, "dot"], ["Spring/Autumn", t.s3, "dot"], ["Summer", t.s2, "dot"], ["Linear fit", t.muted2]]) + `<div id="c-scatter" class="chart h320"></div>`, explain: "demand-scatter" }) +
      card({ title: "Consumption vs official forecasts", sub: z === "FR" ? "GW, hourly · RTE day-ahead (J-1) and intraday (J)" : "GW, hourly · TSOs' day-ahead forecast (SMARD)",
        tools: seg("dm-range", [["2", "2D"], ["7", "1W"], ["14", "2W"]], S.ui.dmRange || "7"),
        body: legend([["Actual", t.s1], ["Day-ahead forecast", t.s2, "dash"], ...(z === "FR" ? [["Intraday forecast", t.s3, "dash"]] : [])]) +
          `<div id="c-fc" class="chart h260"></div><div class="stack"><div class="lab">Temperature, °C (population-weighted)</div><div id="c-fct" class="chart h100"></div></div>` +
          `<p class="note">${errText(c)}</p>`, explain: "demand-forecast" }) +
      card({ title: "Temperature outlook vs normal", sub: "daily mean °C, next 15 days", body:
        legend([["Forecast (deterministic)", t.s1], ["Normal", t.muted2, "dash"], ["Ensemble P10–P90", t.s1, "box"]]) + `<div id="c-anom" class="chart h260"></div>` +
          `<div class="stack"><div class="lab">Indicative effect on consumption, GW <span class="muted">(${esc((c.anomaly || {}).effect_basis || "n/a")})</span></div><div id="c-eff" class="chart h140"></div></div>`, explain: "demand-anomaly" }) +
      `</div>`;
    drawScatter(c, d);
    const drawFc = () => drawForecast(c);
    drawFc();
    onSeg("dm-range", (v) => { S.ui.dmRange = v; drawFc(); });
    drawAnomaly(c);
  }
  function errText(c) {
    const e = c.err_load_fc_da, j = c.err_load_fc_id;
    if (!e) return "Forecast error statistics need 2 days of data.";
    let s = `Last 30 days, day-ahead forecast: mean absolute error ${fmt(e.mae_gw, 2)} GW (${pct(e.mape, 1)}), bias ${signed(e.bias_gw, 2)} GW (positive = forecast too high).`;
    if (j) s += ` Intraday forecast: ${fmt(j.mae_gw, 2)} GW (${pct(j.mape, 1)}).`;
    return s;
  }
  function drawScatter(c, d) {
    const t = t0();
    const s = c.scatter;
    if (!s || !s.date.length) { $("#c-scatter").innerHTML = noData("Temperature history is still loading."); return; }
    const groups = { Winter: t.s1, "Spring/Autumn": t.s3, Summer: t.s2 };
    const series = Object.entries(groups).map(([g, col]) => ({
      type: "scatter", name: g, symbolSize: 6, itemStyle: { color: alpha(col, 0.7), borderColor: t.panel, borderWidth: 0.5 },
      data: s.date.map((dt, i) => s.season[i] === g ? [s.temp[i], s.load_gw[i], dt] : null).filter(Boolean),
    }));
    for (const fit of [c.slope_heating, c.slope_cooling]) {
      if (!fit) continue;
      series.push({ type: "line", name: "Linear fit", showSymbol: false, silent: true, lineStyle: { color: t.muted2, width: 2 }, tooltip: { show: false },
        data: [[fit.x_min, fit.intercept + fit.slope * fit.x_min], [fit.x_max, fit.intercept + fit.slope * fit.x_max]] });
    }
    chart("c-scatter", { ...base(t, { tooltip: { ...base(t).tooltip, trigger: "item",
        formatter: (p) => p.value && p.value[2] ? `<div style="opacity:.7;font-size:11px">${esc(p.value[2])} · ${esc(p.seriesName)}</div><b>${fmt(p.value[1], 1)} GW</b> at <b>${fmt(p.value[0], 1)} °C</b>` : "" } }),
      xAxis: { ...yVal(t, { scale: true }), name: "°C", nameLocation: "end" }, yAxis: { ...yVal(t, { scale: true }), name: "GW" }, series });
  }
  function drawForecast(c) {
    const t = t0();
    const v = c.vs_forecast;
    if (!v || !v.ts.length) { $("#c-fc").innerHTML = noData(); return; }
    const days = Number(S.ui.dmRange || 7);
    const cut = Date.now() - days * 864e5;
    const idx = v.ts.map((x, i) => (new Date(x) >= cut ? i : -1)).filter((i) => i >= 0);
    const pick = (a) => idx.map((i) => [v.ts[i], a[i]]);
    const series = [line("Actual", pick(v.load), t.s1, { area: true }), line("Day-ahead forecast", pick(v.fc_da), t.s2, { dash: true })];
    if (S.country === "FR") series.push(line("Intraday forecast", pick(v.fc_id), t.s3, { dash: true, width: 1.5 }));
    chart("c-fc", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: xTime(t), yAxis: yVal(t, { scale: true }), series }, "dm");
    chart("c-fct", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("°C", 1) } }), xAxis: xTime(t, { label: { show: false } }), yAxis: yVal(t, { scale: true }),
      series: [line("Temperature", pick(v.temp), t.muted2, { width: 1.5 })] }, "dm");
  }
  function drawAnomaly(c) {
    const t = t0();
    const a = c.anomaly;
    if (!a || !a.date || !a.date.length) { $("#c-anom").innerHTML = noData("Forecast not fetched yet."); $("#c-eff").closest(".stack").hidden = true; return; }
    const x = a.date.map((d) => d + "T12:00:00Z");
    const series = [];
    if (a.ens_p10) series.push(...band("Ensemble P10–P90", zip(x, a.ens_p10), zip(x, a.ens_p90), t.s1));
    series.push(line("Forecast", zip(x, a.temp), t.s1, { z: 3 }), line("Normal", zip(x, a.normal), t.muted2, { dash: true }));
    if (c.anomaly_past) {
      const px = c.anomaly_past.date.map((d) => d + "T12:00:00Z");
      series.push(line("Observed", zip(px, c.anomaly_past.temp), t.muted, { width: 1.5 }), line("Normal ", zip(px, c.anomaly_past.normal), t.muted2, { dash: true }));
    }
    chart("c-anom", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("°C", 1, (v) => dshort(v)) } }), xAxis: xTime(t), yAxis: yVal(t, { scale: true }), series }, "an");
    if (a.effect_gw) {
      chart("c-eff", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1, (v) => dshort(v)) } }), xAxis: xTime(t, { label: { show: false } }), yAxis: yVal(t),
        series: [{ type: "bar", name: "Effect vs normal", barMaxWidth: 18, data: zip(x, a.effect_gw),
          itemStyle: { color: (p) => (p.value[1] >= 0 ? t.s1 : t.s2), borderRadius: 4 } }] }, "an");
    } else $("#c-eff").innerHTML = noData("No effect shown outside the heating season.");
  }

  // ================================================================== SUPPLY
  async function pageSupply(root) {
    const d = await data("supply");
    const z = S.country;
    const c = d && d.countries ? d.countries[z] || {} : {};
    const t = t0();
    const o = c.outlook || {};
    const lastIdx = (arr) => { if (!arr) return -1; for (let i = arr.length - 1; i >= 0; i--) if (isNum(arr[i])) return i; return -1; };
    const iw = lastIdx(o.wind100_obs), ia = lastIdx(o.wind_actual_gw);
    root.innerHTML = head("Weather → Supply", `How wind and sunshine turn into wind and solar output in ${ZN[z]}, and what the weather models say for the coming week.`) +
      hero(`Wind output now, ${ZN[z]}`, ia >= 0 ? fmt(o.wind_actual_gw[ia], 1) : "–", " GW",
        iw >= 0 ? `<span class="muted">100 m wind index ${fmt(o.wind100_obs[iw], 1)} m/s · ${ia >= 0 ? hm(o.ts[ia]) : ""}</span>` : "") +
      `<div class="grid two">` +
      card({ title: "Observed wind power curve", sub: "100 m wind (grid mean) vs wind capacity factor, hourly", body:
        legend([["Median", t.s1], ["P10–P90", t.s1, "box"], ["Hours (sample)", t.muted, "dot"]]) + `<div id="c-wcurve" class="chart h320"></div>`, explain: "supply-wind" }) +
      card({ title: "Sunshine vs solar output", sub: "GHI (grid mean) vs solar capacity factor, daylight hours", body:
        legend([["Median", t.s4], ["P10–P90", t.s4, "box"], ["Hours (sample)", t.muted, "dot"]]) + `<div id="c-scurve" class="chart h320"></div>`, explain: "supply-solar" }) +
      card({ title: "Sunny day vs cloudy day", sub: "solar output by hour, GW · last 60 days", body: sunHTML(c) + `<div id="c-sun" class="chart h260"></div>`, explain: "supply-sunny", cls: "span2" }) +
      card({ title: "Next 7 days", sub: "weather index with ECMWF ensemble band, next to the official production forecast",
        tools: seg("sp-var", [["wind", "Wind"], ["solar", "Solar"]], S.ui.spVar || "wind"),
        body: `<div id="sp-out"></div>`, explain: "supply-outlook", cls: "span2" }) +
      card({ title: "Weather map", sub: "wind at 100 m and cloud cover on the observation grid · forecast",
        body: `<div id="c-map" class="chart h380"></div><input type="range" id="map-t" min="0" max="16" value="0" aria-label="Forecast time"><p class="note" id="map-note"></p>`, explain: "supply-map", cls: "span2" }) +
      `</div>`;
    drawCurve("c-wcurve", c.wind_curve, t.s1, "m/s", 1);
    drawCurve("c-scurve", c.solar_curve, t.s4, "W/m²", 0);
    drawSun(c);
    const drawOut = () => drawOutlook(o);
    drawOut();
    onSeg("sp-var", (v) => { S.ui.spVar = v; drawOut(); });
    drawMap(d && d.map);
  }
  function drawCurve(id, cv, color, xunit, xd) {
    const t = t0();
    if (!cv || !cv.bins || !cv.bins.length) { $("#" + id).innerHTML = noData("Weather grid history is still loading."); return; }
    const pts = [];
    for (let i = 0; i + 1 < cv.sample.length; i += 2) pts.push([cv.sample[i], cv.sample[i + 1] * 100]);
    const b = cv.bins;
    chart(id, { ...base(t, { tooltip: { ...base(t).tooltip, trigger: "axis", axisPointer: { type: "line" },
        formatter: (ps) => {
          const p = ps.find((q) => q.seriesName === "Median"); if (!p) return "";
          const bin = b[p.dataIndex];
          return `<div style="opacity:.7;font-size:11px">${fmt(bin.x, xd)} ${xunit} · ${fmt(bin.n)} hours</div><b>${fmt(bin.p50 * 100, 0)}%</b> median <span style="opacity:.7">(P10 ${fmt(bin.p10 * 100, 0)}%, P90 ${fmt(bin.p90 * 100, 0)}%)</span>`;
        } } }),
      xAxis: { ...yVal(t), name: xunit, min: 0 }, yAxis: { ...yVal(t, { fmt: (v) => v + "%" }), min: 0 },
      series: [
        { type: "scatter", name: "Hours", data: pts, symbolSize: 3, itemStyle: { color: alpha(t.muted, 0.25) }, silent: true, tooltip: { show: false }, large: true },
        ...band("P10–P90", b.map((q) => [q.x, q.p10 * 100]), b.map((q) => [q.x, q.p90 * 100]), color),
        { type: "line", name: "Median", data: b.map((q) => [q.x, q.p50 * 100]), showSymbol: false, smooth: 0.2, lineStyle: { color, width: 2.5 }, itemStyle: { color } },
      ] });
  }
  function sunHTML(c) {
    const s = c.sun_vs_cloud;
    if (!s) return "";
    const t = t0();
    return legend([[`Sunny: ${dmy(s.sunny.date + "T12:00Z")} · ${fmt(s.sunny.energy_gwh, 0)} GWh`, t.s2], [`Cloudy: ${dmy(s.cloudy.date + "T12:00Z")} · ${fmt(s.cloudy.energy_gwh, 0)} GWh`, t.s1]]);
  }
  function drawSun(c) {
    const t = t0();
    const s = c.sun_vs_cloud;
    if (!s) { $("#c-sun").innerHTML = noData(); return; }
    const hrs = [...Array(24).keys()];
    const at = (d) => hrs.map((h) => { const i = d.hours.indexOf(h); return i >= 0 ? d.solar_gw[i] : null; });
    chart("c-sun", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1, (h) => `${String(h).padStart(2, "0")}:00`) } }),
      xAxis: { type: "category", data: hrs, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.muted, formatter: (h) => `${h}h`, interval: 2 } },
      yAxis: yVal(t, { min: 0 }),
      series: [line("Sunny day", at(s.sunny), t.s2, { area: true }), line("Cloudy day", at(s.cloudy), t.s1, { area: true })] });
  }
  function drawOutlook(o) {
    const t = t0();
    const el = $("#sp-out");
    const v = S.ui.spVar || "wind";
    if (!o || !o.ts || !o.ts.length) { el.innerHTML = noData("Forecast not fetched yet."); return; }
    const k = v === "wind" ? "wind100" : "ghi";
    const unit = v === "wind" ? "m/s" : "W/m²";
    const col = v === "wind" ? t.s1 : t.s4;
    el.innerHTML = `<div class="lab muted small">${v === "wind" ? "Wind speed at 100 m, grid mean" : "Solar radiation (GHI), grid mean"} · ${o.ens_model ? esc(o.ens_model) : "no ensemble"}</div>` +
      legend([["Observed", t.muted2], ["Forecast", col], ["Ensemble P10–P90", col, "box"]]) + `<div id="c-out1" class="chart h260"></div>` +
      `<div class="lab muted small" style="margin-top:8px">${v === "wind" ? "Wind" : "Solar"} output, GW: official day-ahead forecast (TSOs, via Energy-Charts) and actual</div>` +
      legend([["Actual", t.muted2], ["Official forecast", col, "dash"]]) + `<div id="c-out2" class="chart h180"></div>`;
    const s1 = [];
    if (o[k + "_p10"]) s1.push(...band("Ensemble P10–P90", zip(o.ts, o[k + "_p10"]), zip(o.ts, o[k + "_p90"]), col));
    s1.push(line("Forecast", zip(o.ts, o[k + "_fc"]), col, { z: 3, markLine: nowLine(t) }), line("Observed", zip(o.ts, o[k + "_obs"]), t.muted2, { z: 4 }));
    chart("c-out1", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip(unit, v === "wind" ? 1 : 0) } }), xAxis: xTime(t), yAxis: yVal(t, { min: 0 }), series: s1 }, "out");
    const off = o[v + "_official_gw"] || [];
    chart("c-out2", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: xTime(t), yAxis: yVal(t, { min: 0 }),
      series: [line("Actual", zip(o.ts, o[v + "_actual_gw"]), t.muted2, { markLine: nowLine(t) }), line("Official forecast", zip(o.ts, off), col, { dash: true })] }, "out");
  }
  async function drawMap(m) {
    const el = $("#c-map");
    if (!m || !m.points || !m.points.length) { el.innerHTML = noData("Forecast not fetched yet."); $("#map-t").hidden = true; return; }
    if (!S.geo) {
      try {
        const topo = await load("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-50m.json");
        const keep = new Set(["250", "276", "056", "528", "442", "756", "040", "203", "616", "208", "380", "724", "826"]);
        const fc = topojson.feature(topo, topo.objects.countries);
        fc.features = fc.features.filter((f) => keep.has(String(f.id)));
        echarts.registerMap("eu", fc);
        S.geo = true;
      } catch (e) { el.innerHTML = noData("Map outlines could not be loaded."); return; }
    }
    const t = t0();
    const slider = $("#map-t");
    slider.max = String(m.times.length - 1);
    const draw = (k) => {
      const wind = m.points.filter((p) => p.kind === "wind");
      const cloud = m.points.filter((p) => p.kind === "cloud");
      chart("c-map", {
        animation: false, textStyle: { fontFamily: "Inter, system-ui, sans-serif" },
        tooltip: { trigger: "item", backgroundColor: t.panel, borderColor: t.line, textStyle: { color: t.text, fontSize: 12 },
          formatter: (p) => p.data ? `<div style="opacity:.7;font-size:11px">${esc(p.data.name)}</div><b>${fmt(p.data.value[2], p.seriesName === "Wind" ? 1 : 0)}</b> ${p.seriesName === "Wind" ? "m/s" : "% cloud"}` : "" },
        geo: { map: "eu", roam: false, silent: true, boundingCoords: [[-5.6, 55.4], [15.6, 41.6]],
          itemStyle: { areaColor: t.panel2, borderColor: t.line, borderWidth: 1 },
          regions: [{ name: "France", itemStyle: { areaColor: alpha(t.accent, 0.06) } }, { name: "Germany", itemStyle: { areaColor: alpha(t.accent, 0.06) } }] },
        series: [
          { type: "scatter", name: "Cloud", coordinateSystem: "geo", symbol: "circle", symbolSize: 16,
            data: cloud.map((p) => ({ name: p.id.slice(2), value: [p.lon, p.lat, p.v[k]], itemStyle: { color: alpha(t.muted, 0.12 + 0.6 * ((p.v[k] ?? 0) / 100)) } })) },
          { type: "scatter", name: "Wind", coordinateSystem: "geo", symbol: "arrow",
            data: wind.map((p) => ({ name: p.id.slice(2), value: [p.lon, p.lat, p.v[k]], symbolRotate: -((p.dir[k] ?? 0) + 180),
              symbolSize: [7, 8 + 1.6 * (p.v[k] ?? 0)], itemStyle: { color: t.s1 } })) },
        ],
      });
      $("#map-note").innerHTML = `${dshort(m.times[k])} ${hm(m.times[k])} · arrows: wind direction, length ∝ speed · grey discs: cloud cover (darker = cloudier) · model run ${hm(m.issued)} ${dshort(m.issued)}`;
    };
    draw(0);
    slider.oninput = () => draw(Number(slider.value));
  }

  // ================================================================== PRICE DRIVERS
  async function pageDrivers(root) {
    const z = S.country;
    const [d, days] = await Promise.all([data("drivers"), data("days_" + ZS[z])]);
    const c = d && d.countries ? d.countries[z] || {} : {};
    const t = t0();
    const neg = c.negative || {};
    root.innerHTML = head("Price Drivers", `From weather to price in ${ZN[z]}: one day at a time, then the merit order and two detectors.`) +
      hero(`Negative-price hours, last 12 months, ${ZN[z]}`, fmt(neg.total_hours_12m, 0), " h", `<span class="muted">day-ahead, quarter-hours counted as ¼ h</span>`) +
      `<div class="grid two">` +
      card({ title: "A day in the market", sub: "hourly, local time", cls: "span2", explain: "drivers-day",
        tools: `<input type="date" id="day-pick" aria-label="Day">`,
        body: `<div class="chips" id="day-chips"></div><div id="day-sum" class="note"></div>
          <div class="stack"><div class="lab">① Weather</div><div class="minis"><div><div class="t">Temperature °C</div><div id="c-d-temp" class="chart h100"></div></div><div><div class="t">Wind 100 m, m/s</div><div id="c-d-wind" class="chart h100"></div></div><div><div class="t">Sunshine, W/m²</div><div id="c-d-ghi" class="chart h100"></div></div></div>
          <div class="lab">② Demand and renewables, GW</div>${legend([["Consumption", t.s1], ["Wind", t.s3], ["Solar", t.s4]])}<div id="c-d-dem" class="chart h180"></div>
          <div class="lab">③ Residual load = consumption − wind − solar, GW</div><div id="c-d-res" class="chart h140"></div>
          <div class="lab">④ Day-ahead price, €/MWh</div><div id="c-d-px" class="chart h140"></div></div>` }) +
      card({ title: "Residual load vs price", sub: "hourly, last 12 months", explain: "drivers-merit",
        body: legend([["Hours", t.s1, "dot"], ["Median price", t.text], ["P10–P90", t.muted2, "box"]]) + `<div id="c-merit" class="chart h320"></div><p class="note">${c.merit && isNum(c.merit.corr) ? `Correlation ${fmt(c.merit.corr, 2)} over ${fmt(c.merit.n)} hours.` : ""}</p>` }) +
      card({ title: "Negative prices", sub: "hours per month, day-ahead", explain: "drivers-negative",
        body: `<div id="c-neg" class="chart h180"></div>${negTable(neg)}` }) +
      card({ title: "Dunkelflaute detector", sub: `days when wind + solar produce below ${pct(d && d.dunkel_cf)} of installed capacity`, cls: "span2", explain: "drivers-dunkel",
        body: `<div id="c-dunkel" class="chart h180"></div>${dunkelTable(c.dunkelflaute)}` }) +
      `</div>`;
    drawMerit(c.merit);
    drawNeg(neg);
    drawDunkel(c.dunkelflaute, d && d.dunkel_cf);
    setupDay(days, c.remarkable || []);
  }
  function setupDay(days, remarkable) {
    const pick = $("#day-pick");
    if (!days || !days.ts || !days.ts.length) { $("#day-sum").innerHTML = noData(); return; }
    const dayKey = (iso) => tf(iso, { year: "numeric", month: "2-digit", day: "2-digit" }).split("/").reverse().join("-");
    const keys = [...new Set(days.ts.map(dayKey))];
    const today = dayKey(new Date().toISOString());
    pick.min = keys[0]; pick.max = keys[keys.length - 1];
    const chips = [{ date: today, title: "Today", why: "" }, ...remarkable].filter((r) => keys.includes(r.date));
    $("#day-chips").innerHTML = chips.map((r) => `<button class="chip" data-d="${r.date}" aria-pressed="false" title="${esc(r.why)}">${esc(r.title)}<span class="d">${dshort(r.date + "T12:00:00Z")}</span></button>`).join("");
    const show = (k) => {
      if (!keys.includes(k)) return;
      pick.value = k;
      $$("#day-chips .chip").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.d === k)));
      drawDay(days, k, dayKey, remarkable.find((r) => r.date === k));
    };
    $("#day-chips").onclick = (e) => { const b = e.target.closest(".chip"); if (b) show(b.dataset.d); };
    pick.onchange = () => show(pick.value);
    show(S.ui.day && keys.includes(S.ui.day) ? S.ui.day : keys.includes(today) ? today : keys[keys.length - 1]);
  }
  function drawDay(days, k, dayKey, rem) {
    S.ui.day = k;
    const t = t0();
    const idx = days.ts.map((x, i) => (dayKey(x) === k ? i : -1)).filter((i) => i >= 0);
    const P = (a) => idx.map((i) => [days.ts[i], a[i]]);
    const hourAxis = (show = true) => xTime(t, { label: { show, formatter: (v) => hm(new Date(v).toISOString()) } });
    const mini = (id, key, col, unit, d) => chart(id, { ...base(t, { grid: { left: 2, right: 6, top: 6, bottom: 2, containLabel: true }, tooltip: { ...base(t).tooltip, formatter: tip(unit, d) } }),
      xAxis: hourAxis(false), yAxis: { ...yVal(t, { scale: true }), splitNumber: 2 }, series: [line(unit, P(days[key]), col, { width: 1.8 })] }, "day");
    mini("c-d-temp", "temp", t.s2, "°C", 1);
    mini("c-d-wind", "wind100", t.s1, "m/s", 1);
    mini("c-d-ghi", "ghi", t.s4, "W/m²", 0);
    chart("c-d-dem", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: hourAxis(), yAxis: yVal(t, { min: 0 }),
      series: [line("Consumption", P(days.load), t.s1, { width: 2.2 }), line("Wind", P(days.wind), t.s3, { area: true }), line("Solar", P(days.solar), t.s4, { area: true })] }, "day");
    chart("c-d-res", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("GW", 1) } }), xAxis: hourAxis(), yAxis: yVal(t, { scale: true }),
      series: [line("Residual load", P(days.residual), t.text, { area: true })] }, "day");
    chart("c-d-px", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("€/MWh", 1) } }), xAxis: hourAxis(), yAxis: yVal(t, { scale: true }),
      series: [line("Price", P(days.price), t.accent, { area: true, step: "end", markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: t.muted, type: "dotted" }, data: [{ yAxis: 0 }] } })] }, "day");
    const vals = (a) => idx.map((i) => a[i]).filter(isNum);
    const avg = (a) => { const v = vals(a); return v.length ? v.reduce((x, y) => x + y, 0) / v.length : null; };
    const px = vals(days.price);
    $("#day-sum").innerHTML = `${rem ? `<b>${esc(rem.title)}</b>: ${esc(rem.why)}. ` : ""}${dshort(k + "T12:00:00Z")}: average price <b>${fmt(avg(days.price), 0)} €/MWh</b>` +
      (px.length ? ` (min ${fmt(Math.min(...px), 0)}, max ${fmt(Math.max(...px), 0)})` : "") +
      ` · mean temperature ${fmt(avg(days.temp), 1)} °C · wind+solar ${fmt((avg(days.wind) || 0) + (avg(days.solar) || 0), 1)} GW on average · peak residual load ${fmt(Math.max(...vals(days.residual)), 1)} GW.`;
  }
  function drawMerit(m) {
    const t = t0();
    if (!m || !m.bins || !m.bins.length) { $("#c-merit").innerHTML = noData(); return; }
    const pts = m.residual_gw.map((x, i) => [x, m.price[i]]);
    chart("c-merit", { ...base(t, { tooltip: { ...base(t).tooltip, trigger: "axis",
        formatter: (ps) => { const p = ps.find((q) => q.seriesName === "Median"); if (!p) return ""; const b = m.bins[p.dataIndex];
          return `<div style="opacity:.7;font-size:11px">residual ${fmt(b.x, 1)} GW · ${fmt(b.n)} hours</div><b>${fmt(b.p50, 0)} €/MWh</b> median <span style="opacity:.7">(P10 ${fmt(b.p10, 0)}, P90 ${fmt(b.p90, 0)})</span>`; } } }),
      xAxis: { ...yVal(t, { scale: true }), name: "GW" }, yAxis: { ...yVal(t, { scale: true }), name: "€/MWh" },
      series: [
        { type: "scatter", name: "Hours", data: pts, symbolSize: 3, large: true, itemStyle: { color: alpha(t.s1, 0.22) }, silent: true, tooltip: { show: false } },
        ...band("P10–P90", m.bins.map((b) => [b.x, b.p10]), m.bins.map((b) => [b.x, b.p90]), t.muted2),
        { type: "line", name: "Median", data: m.bins.map((b) => [b.x, b.p50]), showSymbol: false, lineStyle: { color: t.text, width: 2.5 }, itemStyle: { color: t.text } },
      ] });
  }
  function drawNeg(n) {
    const t = t0();
    if (!n || !n.monthly) { $("#c-neg").innerHTML = noData(); return; }
    chart("c-neg", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("h", 1, (v) => v) } }),
      xAxis: { type: "category", data: n.monthly.month, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.muted, formatter: (m) => m.slice(2).replace("-", "/") } },
      yAxis: yVal(t, { min: 0 }), series: [{ type: "bar", name: "Negative hours", data: n.monthly.hours, barMaxWidth: 14, itemStyle: { color: t.accent, borderRadius: [4, 4, 0, 0] } }] });
  }
  const negTable = (n) => !n || !n.recent_days || !n.recent_days.length ? "" :
    `<div class="table-wrap"><table class="t"><thead><tr><th>Recent days</th><th class="n">Negative QH</th><th class="n">Min €/MWh</th></tr></thead><tbody>` +
    n.recent_days.slice(0, 6).map((r) => `<tr><td>${dshort(r.date + "T12:00:00Z")}</td><td class="n">${r.qh}</td><td class="n">${fmt(r.min, 2)}</td></tr>`).join("") + `</tbody></table></div>`;
  function drawDunkel(dk, th) {
    const t = t0();
    if (!dk || !dk.daily || !dk.daily.date.length) { $("#c-dunkel").innerHTML = noData(); return; }
    chart("c-dunkel", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: (ps) => { const p = ps[0]; return `<div style="opacity:.7;font-size:11px">${esc(p.axisValue)}</div><b>${pct(p.value, 1)}</b> wind+solar capacity factor`; } } }),
      xAxis: { type: "category", data: dk.daily.date, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.muted, formatter: (d) => d.slice(5) } },
      yAxis: yVal(t, { min: 0, fmt: (v) => Math.round(v * 100) + "%" }),
      series: [{ type: "bar", name: "RES CF", data: dk.daily.res_cf, barCategoryGap: "10%",
        itemStyle: { color: (p) => (p.value < th ? t.s2 : alpha(t.s1, 0.55)), borderRadius: [2, 2, 0, 0] },
        markLine: { symbol: "none", silent: true, label: { formatter: `${pct(th)} threshold`, color: t.muted, position: "insideEndTop" }, lineStyle: { color: t.s2, type: "dashed" }, data: [{ yAxis: th }] } }] });
  }
  const dunkelTable = (dk) => !dk || !dk.episodes || !dk.episodes.length ? `<p class="note">No episode in the data.</p>` :
    `<div class="table-wrap"><table class="t"><thead><tr><th>Episode</th><th class="n">Days</th><th class="n">Wind+solar CF</th><th class="n">Residual load</th><th class="n">Avg price</th></tr></thead><tbody>` +
    dk.episodes.slice(0, 8).map((e) => `<tr><td>${dmy(e.start + "T12:00:00Z")} → ${dshort(e.end + "T12:00:00Z")}</td><td class="n">${e.days}</td><td class="n">${pct(e.res_cf, 1)}</td><td class="n">${fmt(e.residual_gw, 1)} GW</td><td class="n">${fmt(e.price, 0)} €/MWh</td></tr>`).join("") +
    `</tbody></table></div>`;

  // ================================================================== DATA HUB
  async function pageData(root) {
    const d = await data("datahub");
    if (!d) { root.innerHTML = head("Data Hub") + noData(); return; }
    const cats = d.categories;
    const cur = S.ui.cat || "all";
    root.innerHTML = head("Data Hub", "Every series behind the site: latest value, 24-hour change, the last 30 days, and how fresh it is. Tap a row for details.") +
      `<div class="hub-tools"><input type="search" id="hub-q" placeholder="Search a series, a country, a source…" aria-label="Search" value="${esc(S.ui.q || "")}">
       <div class="chips" id="hub-cats">${[["all", "All"], ...cats.map((c) => [c.id, c.label])].map(([id, l]) => `<button class="chip" data-c="${id}" aria-pressed="${id === cur}">${esc(l)}</button>`).join("")}</div></div>
       <div class="card" id="hub-list"></div>` +
      `<div class="grid" style="margin-top:16px">${card({ title: "Sources and APIs", sub: "all free, no key", body: sourcesTable(d.sources), explain: "hub" })}</div>`;
    const draw = () => {
      const q = (S.ui.q || "").toLowerCase();
      const c = S.ui.cat || "all";
      let html = "";
      for (const cat of cats) {
        if (c !== "all" && c !== cat.id) continue;
        if (cat.id === "gaps") {
          if (q && !"data gaps".includes(q)) continue;
          html += `<div class="cat-h">${esc(cat.label)}</div><ul class="rows">` + d.gaps.map((g) => `<li class="rowi" style="cursor:default;grid-template-columns:1fr"><div><div class="nm">${esc(g.label)}</div><div class="meta" style="white-space:normal">${esc(g.why)}</div></div></li>`).join("") + `</ul>`;
          continue;
        }
        const rows = d.rows.filter((r) => r.cat === cat.id && (!q || `${r.label} ${r.country} ${r.source} ${r.desc}`.toLowerCase().includes(q)));
        if (!rows.length) continue;
        html += `<div class="cat-h">${esc(cat.label)}</div><ul class="rows">${rows.map(rowHTML).join("")}</ul>`;
      }
      $("#hub-list").innerHTML = html || noData("Nothing matches.");
    };
    draw();
    $("#hub-q").oninput = (e) => { S.ui.q = e.target.value; draw(); };
    $("#hub-cats").onclick = (e) => { const b = e.target.closest(".chip"); if (!b) return; S.ui.cat = b.dataset.c; $$("#hub-cats .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); draw(); };
    $("#hub-list").onclick = (e) => { const r = e.target.closest(".rowi[data-id]"); if (r) openRow(d.rows.find((x) => x.id === r.dataset.id)); };
    $("#hub-list").onkeydown = (e) => { if (e.key === "Enter") { const r = e.target.closest(".rowi[data-id]"); if (r) openRow(d.rows.find((x) => x.id === r.dataset.id)); } };
  }
  function spark(v) {
    const xs = (v || []).map((x, i) => [i, x]).filter((p) => isNum(p[1]));
    if (xs.length < 2) return `<svg viewBox="0 0 84 28" aria-hidden="true"></svg>`;
    const ys = xs.map((p) => p[1]);
    const lo = Math.min(...ys), hi = Math.max(...ys), n = v.length - 1 || 1;
    const pts = xs.map(([i, y]) => `${(i / n * 82 + 1).toFixed(1)},${(26 - (hi === lo ? 12 : (y - lo) / (hi - lo) * 24)).toFixed(1)}`).join(" ");
    return `<svg viewBox="0 0 84 28" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }
  function rowHTML(r) {
    const stale = r.ts && !r.forecast && (Date.now() - new Date(r.ts)) > 3 * 864e5 && !/weekly|daily/.test(r.freq);
    return `<li class="rowi" data-id="${esc(r.id)}" tabindex="0" role="button" aria-label="${esc(r.label)} ${esc(r.country)}">
      <div><div class="nm">${esc(r.label)} <span class="tag">${esc(r.country)}</span></div>
        <div class="meta ${stale ? "fresh-old" : ""}">${esc(r.source)} · ${r.ts ? (r.forecast && new Date(r.last_ts) > Date.now() ? "to " + dshort(r.last_ts) : ago(r.ts)) : "no data"}</div></div>
      ${spark(r.spark)}
      <div class="val">${fmt(r.value, r.decimals)} <small>${esc(r.unit)}</small><span class="c">${isNum(r.change) ? chgHTML(r.change, r.decimals) : "<span class='muted'>–</span>"}</span></div></li>`;
  }
  async function openRow(r) {
    if (!r) return;
    const t = t0();
    const body = $("#sheet-body");
    body.innerHTML = `<div class="muted small">${esc(r.country)} · ${esc(r.source)}</div><h2 id="sheet-title" style="font-size:20px;margin:2px 36px 6px 0">${esc(r.label)}</h2>
      ${hero(r.ts ? (r.forecast ? "Value at " : "Latest, ") + dshort(r.ts) + " " + hm(r.ts) : "No data", fmt(r.value, r.decimals), " " + esc(r.unit), isNum(r.change) ? `${chgHTML(r.change, r.decimals, r.unit)} <span class="muted">vs ${esc(r.change_vs)}</span>` : "")}
      <div style="display:flex;justify-content:flex-end">${seg("hub-p", [["1W", "1W"], ["1M", "1M"], ["1Y", "1Y"]], S.ui.hubP || "1M")}</div>
      <div id="c-hub" class="chart h260"></div>
      <p class="note">${esc(r.desc)}</p>
      <dl class="kv"><dt>Source</dt><dd><a href="${esc(r.url)}" rel="noopener">${esc(r.source)}</a></dd><dt>Licence</dt><dd>${esc(r.license)}</dd>
      <dt>Native frequency</dt><dd>${esc(r.freq)}</dd><dt>Unit</dt><dd>${esc(r.unit)}</dd><dt>Last value</dt><dd>${r.last_ts ? dmy(r.last_ts) + " " + hm(r.last_ts) : "–"}</dd>
      <dt>Series id</dt><dd><code>${esc(r.id)}</code></dd></dl>`;
    openSheet();
    const det = await load(`data/hub/${r.id}.json`);
    const draw = (p) => {
      if (!det) { $("#c-hub").innerHTML = noData(); return; }
      let pts;
      if (p === "1W") pts = zip(det.recent.ts, det.recent.v);
      else { const n = p === "1M" ? 31 : 366; pts = zip(det.daily.date.slice(-n).map((d) => d + "T12:00:00Z"), det.daily.v.slice(-n)); }
      if (!pts.length) { $("#c-hub").innerHTML = noData(); return; }
      chart("c-hub", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip(r.unit, r.decimals, p === "1W" ? undefined : (v) => dshort(v)) } }),
        xAxis: xTime(t), yAxis: yVal(t, { scale: true }), series: [line(p === "1W" ? "Hourly" : "Daily mean", pts, t.accent, { area: true })] });
    };
    draw(S.ui.hubP || "1M");
    onSeg("hub-p", (v) => { S.ui.hubP = v; draw(v); });
  }
  const sourcesTable = (src) => `<div class="table-wrap"><table class="t"><thead><tr><th>Source</th><th>Licence</th><th class="n">Rows</th></tr></thead><tbody>` +
    src.map((s) => `<tr><td><a href="${esc(s.url)}" rel="noopener">${esc(s.name)}</a></td><td>${esc(s.license)}</td><td class="n">${s.rows}</td></tr>`).join("") + `</tbody></table></div>`;
  function openSheet() {
    const w = $("#sheet");
    w.hidden = false;
    S.lastFocus = document.activeElement;
    $(".sheet-close", w).focus();
  }
  function closeSheet() {
    const w = $("#sheet");
    if (w.hidden) return;
    w.hidden = true;
    if (S.charts["c-hub"]) { S.charts["c-hub"].dispose(); delete S.charts["c-hub"]; }
    if (S.lastFocus) S.lastFocus.focus();
  }
  $("#sheet").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

  // ================================================================== FLEXIBILITY (v1 bots + battery)
  const BOT = { rule: "Rule bot", ml: "ML bot" };
  async function pageFlex(root) {
    const [bots, bat] = await Promise.all([data("bots"), data("battery")]);
    const t = t0();
    const BC = { rule: t.s1, ml: t.s2 };
    const live = bots && bots.live_start;
    const tot = bots && bots.stats ? Object.fromEntries(Object.entries(bots.stats).map(([b, s]) => [b, s.all])) : {};
    root.innerHTML = head("Flexibility", "Two paper-trading bots on the Belgian imbalance price, and what a 1 MW / 2 MWh battery could have earned on day-ahead prices. Carried over from v1; the live journal is never reset.") +
      hero("ML bot, total P&L", tot.ml ? fmt(tot.ml.total_pnl, 0) : "–", " €", live ? `<span class="muted">live since ${dmy(live)}</span>` : `<span class="muted">backtest; live record starts at deployment</span>`) +
      `<div class="grid two">` +
      card({ title: "Cumulative P&L", sub: "€, 1 MW, Belgium, day-ahead vs imbalance price", cls: "span2", explain: "flex-bots",
        body: legend([[BOT.rule, BC.rule], [BOT.ml, BC.ml]]) + `<div id="c-cum" class="chart h320"></div>` }) +
      card({ title: "Statistics", tools: seg("st-seg", [["backtest", "Backtest"], ["live", "Live"], ["all", "All"]], S.ui.stSeg || "backtest"), body: `<div id="stats" class="table-wrap"></div>`, explain: "flex-stats" }) +
      card({ title: "P&L by hour of day", sub: "€, Europe/Paris", body: legend([[BOT.rule, BC.rule, "box"], [BOT.ml, BC.ml, "box"]]) + `<div id="c-hour" class="chart h260"></div>` }) +
      card({ title: "Last trades", sub: live ? `${bots.pending || 0} pending settlement` : "backtest", body: `<div id="trades" class="table-wrap" style="max-height:360px;overflow-y:auto"></div>` }) +
      card({ title: "ML feature importance", sub: "gain share, latest weekly model", body: `<div id="c-fi" class="chart h320"></div>` }) +
      card({ title: "Battery League", sub: "1 MW / 2 MWh · perfect foresight on day-ahead", cls: "span2", explain: "flex-battery",
        tools: seg("lg-seg", [["today", "Today"], ["d7", "7d"], ["d30", "30d"], ["total", "Total"]], S.ui.lg || "d30"), body: `<div id="league" class="table-wrap"></div>` }) +
      card({ title: "Optimal dispatch", sub: "latest complete day", cls: "span2", explain: "flex-dispatch",
        tools: seg("bz-seg", Object.keys((bat && bat.profiles) || {}).map((z) => [z, z]), S.ui.bz || "FR"),
        body: `<div class="stack"><div class="lab">Day-ahead price, €/MWh</div><div id="c-bp" class="chart h140"></div>
               <div class="lab">Charge (−) / discharge (+), MW</div>${legend([["Discharge", t.s1, "box"], ["Charge", t.s2, "box"]])}<div id="c-bd" class="chart h140"></div>
               <div class="lab">State of charge, MWh</div><div id="c-bs" class="chart h100"></div></div><p class="note" id="bp-note"></p>` }) +
      `</div>`;
    if (!bots || !bots.cum || !Object.keys(bots.cum).length) { ["#c-cum", "#stats", "#c-hour", "#trades", "#c-fi"].forEach((s) => { $(s).innerHTML = noData(); }); }
    else {
      const marks = [];
      if (live) marks.push({ xAxis: live.slice(0, 10), label: { formatter: "LIVE", color: t.pos }, lineStyle: { color: t.pos } });
      chart("c-cum", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("€", 0, (v) => dmy(v)) } }), xAxis: xTime(t), yAxis: yVal(t, { fmt: (v) => Math.abs(v) >= 1000 ? fmt(v / 1000, 0) + "k" : fmt(v, 0) }),
        dataZoom: [{ type: "inside" }],
        series: Object.entries(bots.cum).map(([b, s], i) => line(BOT[b], s.date.map((d, k) => [d, s.cum[k]]), BC[b],
          { markLine: i === 0 && marks.length ? { symbol: "none", silent: true, data: marks } : undefined })) });
      const drawStats = () => renderStats(bots, BC);
      drawStats();
      onSeg("st-seg", (v) => { S.ui.stSeg = v; drawStats(); });
      const tr = bots.trades || [];
      $("#trades").innerHTML = tr.length ? `<table class="t"><thead><tr><th>Bot</th><th>Quarter-hour</th><th class="n">Pos</th><th class="n">DA</th><th class="n">Imb.</th><th class="n">P&amp;L €</th></tr></thead><tbody>` +
        tr.map((x) => `<tr><td>${BOT[x.bot]}</td><td>${dshort(x.qh)} ${hm(x.qh)}<div class="muted small">${x.segment === "live" ? esc(x.status) : "backtest"}</div></td><td class="n">${x.position > 0 ? "+1" : x.position < 0 ? "−1" : "0"}</td>
          <td class="n">${fmt(x.da, 1)}</td><td class="n">${fmt(x.imb, 1)}</td><td class="n">${isNum(x.pnl) ? chgHTML(x.pnl, 1) : "–"}</td></tr>`).join("") + "</tbody></table>" : noData();
      const fi = bots.feature_importance;
      if (fi) { const items = fi.items.slice(0, 12).reverse();
        chart("c-fi", { ...base(t, { grid: { left: 4, right: 16, top: 4, bottom: 4, containLabel: true }, tooltip: { ...base(t).tooltip, trigger: "item", formatter: (p) => `${esc(p.name)}<br><b>${pct(p.value, 1)}</b>` } }),
          xAxis: { ...yVal(t, { fmt: (v) => Math.round(v * 100) + "%" }) }, yAxis: { type: "category", data: items.map((x) => x[0]), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.muted2 } },
          series: [{ type: "bar", data: items.map((x) => x[1]), barMaxWidth: 12, itemStyle: { color: t.accent, borderRadius: [0, 4, 4, 0] } }] });
      } else $("#c-fi").innerHTML = noData("No model yet.");
    }
    if (!bat || !bat.league || !bat.league.length) { $("#league").innerHTML = noData(); $("#c-bp").innerHTML = noData(); return; }
    const drawLeague = () => {
      const w = S.ui.lg || "d30";
      const L = bat.league.slice().sort((a, b) => (b[w] ?? -1e18) - (a[w] ?? -1e18));
      $("#league").innerHTML = `<table class="t"><thead><tr><th>#</th><th>Zone</th><th class="n">Today €</th><th class="n">7d €</th><th class="n">30d €</th><th class="n">Total €</th><th class="n">€/day</th><th class="n">Cycles/day</th></tr></thead><tbody>` +
        L.map((r, i) => `<tr><td>${isNum(r[w]) ? i + 1 : "–"}</td><td><b>${esc(r.zone)}</b></td><td class="n">${fmt(r.today, 0)}</td><td class="n">${fmt(r.d7, 0)}</td><td class="n">${fmt(r.d30, 0)}</td><td class="n">${fmt(r.total, 0)}</td><td class="n">${fmt(r.days ? r.total / r.days : null, 0)}</td><td class="n">${fmt(r.cycles_30d, 2)}</td></tr>`).join("") +
        `</tbody></table><p class="note">Totals since ${esc(L[0].since || "–")}. Tomorrow, when day-ahead prices are published: ${L.filter((r) => isNum(r.tomorrow)).map((r) => `${esc(r.zone)} ${fmt(r.tomorrow, 0)} €`).join(" · ") || "–"}.</p>`;
    };
    drawLeague();
    onSeg("lg-seg", (v) => { S.ui.lg = v; drawLeague(); });
    const drawProfile = () => {
      const p = bat.profiles[S.ui.bz || "FR"];
      if (!p) { ["#c-bp", "#c-bd", "#c-bs"].forEach((s) => { $(s).innerHTML = noData(); }); return; }
      $("#bp-note").textContent = `${p.zone} · ${p.date} · revenue ${fmt(p.revenue_eur, 0)} € · ${fmt(p.cycles, 2)} cycles`;
      chart("c-bp", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("€/MWh", 1) } }), xAxis: xTime(t, { label: { show: false } }), yAxis: yVal(t, { scale: true }),
        series: [line("DA price", zip(p.ts, p.price), t.text, { step: "end" })] }, "bat");
      chart("c-bd", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("MW", 2) } }), xAxis: xTime(t, { label: { show: false } }), yAxis: yVal(t, { min: -1, max: 1 }),
        series: [{ type: "bar", name: "Discharge", stack: "d", data: zip(p.ts, p.discharge), itemStyle: { color: t.s1 }, barCategoryGap: "15%" },
                 { type: "bar", name: "Charge", stack: "d", data: p.ts.map((x, i) => [x, -p.charge[i]]), itemStyle: { color: t.s2 } }] }, "bat");
      chart("c-bs", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("MWh", 2) } }), xAxis: xTime(t), yAxis: yVal(t, { min: 0, max: 2 }),
        series: [line("State of charge", zip(p.ts, p.soc), t.muted2, { area: true })] }, "bat");
    };
    drawProfile();
    onSeg("bz-seg", (v) => { S.ui.bz = v; drawProfile(); });
  }
  function renderStats(bots, BC) {
    const t = t0();
    const seg_ = S.ui.stSeg || "backtest";
    const st = bots.stats;
    const rows = [
      ["Total P&L", (s) => chgHTML(s.total_pnl, 0, "€")], ["P&L per trade", (s) => chgHTML(s.pnl_per_trade, 2, "€")],
      ["Hit ratio", (s) => pct(s.hit_ratio, 1)], ["Sharpe (daily, ann.)", (s) => (isNum(s.sharpe) ? signed(s.sharpe, 2) : "–")],
      ["Max drawdown", (s) => chgHTML(s.max_drawdown, 0, "€")], ["Exposure", (s) => pct(s.exposure, 1)], ["Trades", (s) => fmt(s.trades)],
      ["Period", (s) => (s.first ? `${tf(s.first, { month: "short", year: "numeric" })} → ${tf(s.last, { month: "short", year: "numeric" })}` : "–")],
    ];
    const bk = ["rule", "ml"].filter((b) => st[b]);
    const empty = bk.every((b) => !st[b][seg_] || !st[b][seg_].decided_qh);
    $("#stats").innerHTML = empty ? noData(seg_ === "live" ? "No live trades yet: the live record starts at deployment." : "No data.") :
      `<table class="t"><thead><tr><th></th>${bk.map((b) => `<th class="n">${BOT[b]}</th>`).join("")}</tr></thead><tbody>` +
      rows.map(([k, f]) => `<tr><td>${k}</td>${bk.map((b) => `<td class="n">${f(st[b][seg_])}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
    chart("c-hour", { ...base(t, { tooltip: { ...base(t).tooltip, formatter: tip("€", 0, (h) => `${h}:00`) } }),
      xAxis: { type: "category", data: [...Array(24).keys()], axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.muted, interval: 3 } },
      yAxis: yVal(t), series: bk.map((b) => ({ type: "bar", name: BOT[b], data: bots.by_hour[b][seg_], itemStyle: { color: BC[b], borderRadius: 3 }, barGap: "10%" })) });
  }

  // ================================================================== LEARN
  async function pageLearn(root) {
    const [g, l, hub] = await Promise.all([content("glossary"), content("learn"), data("datahub")]);
    const terms = (g && g.terms) || [];
    root.innerHTML = head("Learn", "A glossary of the words used on this site, how every number is computed, and where the data comes from.") +
      `<div class="grid two"><section class="card prose">${(l && l.sections || []).map((s) => `<h2 id="${esc(s.id)}">${esc(s.title)}</h2>${s.html}`).join("")}</section>
       <section class="card"><header><h2>Glossary</h2><span class="sub">${terms.length} terms</span></header>
       <input type="search" id="gl-q" placeholder="Search the glossary" aria-label="Search the glossary"><dl class="gloss" id="gl"></dl></section>
       ${hub ? card({ title: "Sources and licences", body: sourcesTable(hub.sources), cls: "span2" }) : ""}</div>`;
    const draw = (q = "") => {
      const ql = q.toLowerCase();
      $("#gl").innerHTML = terms.filter((x) => !ql || (x.term + " " + x.def).toLowerCase().includes(ql))
        .map((x) => `<dt id="g-${esc(x.term.toLowerCase().replace(/[^a-z0-9]+/g, "-"))}">${esc(x.term)}</dt><dd>${esc(x.def)}</dd>`).join("") || `<dd class="muted">No match.</dd>`;
    };
    draw();
    $("#gl-q").oninput = (e) => draw(e.target.value);
  }

  // ================================================================== router & shell
  const RENDER = { today: pageToday, demand: pageDemand, supply: pageSupply, drivers: pageDrivers, data: pageData, flex: pageFlex, learn: pageLearn };
  const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;
  $("#nav-top").innerHTML = PAGES.map((p) => `<a href="#${p.id}" data-p="${p.id}">${p.label}</a>`).join("");
  $("#nav-bottom").innerHTML = PAGES.filter((p) => BOTTOM.includes(p.id)).map((p) => `<a href="#${p.id}" data-p="${p.id}">${svg(p.icon)}<span>${p.short || p.label}</span></a>`).join("");

  async function route() {
    let id = location.hash.replace("#", "") || "today";
    if (!RENDER[id]) id = "today";
    S.page = id;
    $$("[data-p]").forEach((a) => (a.dataset.p === id ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
    document.title = `${PAGES.find((p) => p.id === id).label} · Intraday Power`;
    disposeAll();
    const root = $("#page");
    try { await RENDER[id](root); } catch (e) { console.error(e); root.innerHTML += noData("Something went wrong while drawing this page."); }
  }
  function setCountry(z) {
    S.country = z;
    try { localStorage.setItem("ip-country", z); } catch (e) { /* ignore */ }
    $$("#country button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === z)));
    route();
  }
  $("#country").addEventListener("click", (e) => { const b = e.target.closest("button[data-v]"); if (b) setCountry(b.dataset.v); });
  $$("#country button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === S.country)));
  $("#theme").addEventListener("click", () => {
    const dark = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() === "#000000";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ip-theme", document.documentElement.dataset.theme); } catch (e) { /* ignore */ }
    _t = null;
    route();
  });
  window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });
  if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { _t = null; route(); });

  async function boot() {
    const meta = await fetch(`data/meta.json?t=${Date.now()}`, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    S.version = meta ? meta.last_update_utc : Date.now();
    EXPLAIN = (await content("explain")) || {};
    if (meta) {
      $("#updated").textContent = `updated ${dmy(meta.last_update_utc)} ${hm(meta.last_update_utc)} Paris`;
      const age = (Date.now() - new Date(meta.last_update_utc)) / 60000;
      if (age > (meta.stale_after_min || 45)) { const b = $("#stale"); b.hidden = false; b.textContent = `Data is ${Math.round(age)} min old. The GitHub Actions schedule may be delayed.`; }
    }
    route();
  }
  boot();
  setInterval(async () => {
    const m = await fetch(`data/meta.json?t=${Date.now()}`, { cache: "no-cache" }).then((r) => r.json()).catch(() => null);
    if (m && S.version && m.last_update_utc !== S.version && $("#sheet").hidden) { S.cache = {}; S.version = m.last_update_utc; route(); }
  }, 5 * 60 * 1000);
})();
