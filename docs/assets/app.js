/* Intraday Power front-end: vanilla JS + ECharts, reads docs/data/*.json (no build step). */
(() => {
  "use strict";

  const TZ = "Europe/Paris";
  const C = { amber: "#ffb000", green: "#00c176", red: "#ff3b30", blue: "#4aa3ff", text: "#d6dde4",
              muted: "#7d8a97", line: "#222b35", panel: "#11161c" };
  const ZONE_COLORS = { FR: "#4aa3ff", "DE-LU": "#ffb000", BE: "#e85d75", NL: "#00c176", DK1: "#b48cff" };
  const BOT_COLORS = { rule: "#4aa3ff", ml: "#ffb000" };
  const BOT_NAMES = { rule: "Rule bot", ml: "ML bot" };
  const state = { data: {}, charts: {}, rendered: {}, statsSeg: "backtest", leagueWin: "today",
                  zone: "BE", bzone: "BE" };

  // ------------------------------------------------------------------ formatting
  const nf = (d) => new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmt = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x)) ? "–" : nf(d).format(x);
  const signed = (x, d = 0) => {
    if (x === null || x === undefined || Number.isNaN(x)) return "–";
    const s = x > 0 ? "+" : x < 0 ? "−" : "±";
    return s + nf(d).format(Math.abs(x));
  };
  const arrow = (x) => (x > 0 ? "▲" : x < 0 ? "▼" : "■");
  const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
  const pnlHTML = (x, d = 0) => `<span class="${cls(x)}">${x === null || x === undefined ? "–" : arrow(x) + " " + signed(x, d)}</span>`;
  const pct = (x, d = 1) => (x === null || x === undefined) ? "–" : nf(d).format(100 * x) + "%";
  const tfmt = (iso, opts) => iso ? new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...opts }).format(new Date(iso)) : "–";
  const hm = (iso) => tfmt(iso, { hour: "2-digit", minute: "2-digit" });
  const dhm = (iso) => tfmt(iso, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const $ = (sel) => document.querySelector(sel);
  const noData = (el, msg = "no data") => { el.innerHTML = `<div class="nodata">${esc(msg)}</div>`; };

  // ------------------------------------------------------------------ data
  async function getJSON(name, v) {
    try {
      const r = await fetch(`data/${name}.json?v=${encodeURIComponent(v)}`, { cache: "no-cache" });
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch (e) {
      console.warn("missing", name, e);
      return null;
    }
  }

  // ------------------------------------------------------------------ charts
  const baseOpt = () => ({
    backgroundColor: "transparent",
    textStyle: { fontFamily: "JetBrains Mono, monospace", color: C.text, fontSize: 11 },
    grid: { left: 52, right: 16, top: 28, bottom: 28, containLabel: false },
    tooltip: { trigger: "axis", backgroundColor: "#0b0e11", borderColor: C.line, textStyle: { color: C.text, fontSize: 11 } },
    legend: { top: 0, textStyle: { color: C.muted }, icon: "rect", itemWidth: 10, itemHeight: 3 },
  });
  const axisStyle = { axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.muted },
                      splitLine: { lineStyle: { color: "#1a212a" } } };
  const timeAxis = (extra = {}) => ({ type: "time", ...axisStyle, splitLine: { show: false },
    axisLabel: { color: C.muted, formatter: (v) => tfmt(new Date(v).toISOString(), { hour: "2-digit", minute: "2-digit" }) }, ...extra });

  function chart(id) {
    const el = document.getElementById(id);
    if (!el || typeof echarts === "undefined") return null;
    if (!state.charts[id]) { el.innerHTML = ""; state.charts[id] = echarts.init(el, null, { renderer: "canvas" }); }
    return state.charts[id];
  }
  window.addEventListener("resize", () => Object.values(state.charts).forEach((c) => c.resize()));

  // ------------------------------------------------------------------ header / ticker
  function renderHeader(meta) {
    const el = $("#last-update");
    if (!meta) { el.textContent = "no data"; return; }
    el.textContent = tfmt(meta.last_update_utc, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) + " Paris";
    const ageMin = (Date.now() - new Date(meta.last_update_utc)) / 60000;
    if (ageMin > (meta.stale_after_min || 45)) {
      $("#stale").hidden = false;
      const b = $("#stale-banner");
      b.hidden = false;
      b.textContent = `⚠ Data is ${Math.round(ageMin)} min old (threshold ${meta.stale_after_min} min). The GitHub Actions schedule may be delayed or disabled.`;
    }
  }

  function renderTicker(desk) {
    const el = $("#ticker");
    if (!desk || !desk.ticker) { el.innerHTML = '<span class="muted">no data</span>'; return; }
    const item = (t) => {
      if (t.price === null || t.price === undefined) return `<span class="tick"><b>${t.zone}</b><span class="muted">no data</span></span>`;
      const d = t.prev !== null && t.prev !== undefined ? t.price - t.prev : null;
      return `<span class="tick"><b>${t.zone}</b><span class="px">${fmt(t.price, 2)}</span>` +
        (d !== null ? ` <span class="${cls(d)}">${arrow(d)} ${signed(d, 2)}</span>` : "") +
        `<small>QH ${hm(t.qh)} · day ${fmt(t.day_min, 0)}/${fmt(t.day_avg, 0)}/${fmt(t.day_max, 0)} · next ${fmt(t.next, 2)}</small></span>`;
    };
    const html = desk.ticker.map(item).join("");
    el.innerHTML = html + html; // duplicated for seamless scroll
  }

  // ------------------------------------------------------------------ DESK
  function renderDesk(desk, meta) {
    if (!desk) { ["#bot-cards", "#be-rt", "#alerts"].forEach((s) => noData($(s))); noData($("#ch-strip")); return; }
    // price strip
    const s = desk.strip || {};
    const c = chart("ch-strip");
    if (c && s.ts && s.ts.length) {
      const zones = Object.keys(s).filter((k) => k !== "ts");
      c.setOption({
        ...baseOpt(),
        xAxis: timeAxis(),
        yAxis: { type: "value", ...axisStyle, name: "EUR/MWh", nameTextStyle: { color: C.muted } },
        tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => fmt(v, 2) },
        series: zones.map((z) => ({ name: z, type: "line", step: "start", showSymbol: false,
          lineStyle: { width: 1.4, color: ZONE_COLORS[z] }, itemStyle: { color: ZONE_COLORS[z] },
          data: s.ts.map((t, i) => [t, s[z][i]]),
          markLine: z === zones[0] ? { symbol: "none", silent: true, label: { color: C.muted, formatter: "now" },
            lineStyle: { color: C.muted, type: "dashed" }, data: [{ xAxis: new Date().toISOString() }] } : undefined })),
      });
    } else noData($("#ch-strip"));

    // bot cards
    const bt = desk.bots_today || {};
    $("#bot-cards").innerHTML = ["rule", "ml"].map((b) => {
      const x = bt[b];
      if (!x || !x.has_live) {
        return `<div class="card"><div class="lbl">${BOT_NAMES[b]}</div><div class="big muted">–</div><div class="sub">live not started yet</div></div>`;
      }
      const pos = x.last_position;
      return `<div class="card"><div class="lbl">${BOT_NAMES[b]} · today</div><div class="big">${pnlHTML(x.pnl, 2)} <small class="muted">EUR</small></div>
        <div class="sub">${x.trades} trades settled · ${x.pending} pending · last ${x.last_qh ? hm(x.last_qh) : "–"}
        pos ${pos === null ? "–" : (pos > 0 ? "▲ LONG" : pos < 0 ? "▼ SHORT" : "■ FLAT")}</div></div>`;
    }).join("");

    // Belgium real time
    const be = desk.be || {};
    const rows = [];
    if (be.si_min) rows.push(["System imbalance", `<span class="${cls(be.si_min.si)}">${arrow(be.si_min.si)} ${signed(be.si_min.si, 0)} MW</span> <span class="muted">${be.si_min.si < 0 ? "short" : "long"} · ${hm(be.si_min.ts)}</span>`]);
    if (be.imb_qh) rows.push(["Imbalance price (QH)", `${fmt(be.imb_qh.price, 2)} <span class="muted">QH ${hm(be.imb_qh.ts)}</span>`]);
    if (be.imb_qh && be.imb_qh.da !== null) rows.push(["Imbalance − DA", pnlHTML(be.imb_qh.price - be.imb_qh.da, 2)]);
    if (be.imb_min) rows.push(["Imbalance price (min)", `${fmt(be.imb_min.price, 2)} <span class="muted">${hm(be.imb_min.ts)}</span>`]);
    if (be.si_fc) rows.push(["Elia SI forecast next QH", `${signed(be.si_fc.si_fc, 0)} MW <span class="muted">QH ${hm(be.si_fc.target)} · P(short&lt;−200) ${pct(be.si_fc.p_short, 0)}</span>`]);
    $("#be-rt").innerHTML = rows.length ? rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") : '<div class="nodata">no data</div>';
    $("#be-rt").setAttribute("role", "list");
    const cs = chart("ch-si");
    if (cs && be.si_spark && be.si_spark.ts.length) {
      cs.setOption({ ...baseOpt(), grid: { left: 44, right: 8, top: 8, bottom: 20 }, legend: { show: false },
        xAxis: timeAxis(), yAxis: { type: "value", ...axisStyle, splitNumber: 2 },
        tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => fmt(v, 0) + " MW" },
        series: [{ name: "SI", type: "line", showSymbol: false, lineStyle: { width: 1, color: C.amber }, itemStyle: { color: C.amber },
          areaStyle: { color: C.amber, opacity: 0.08 },
          markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: C.muted, type: "dashed" }, data: [{ yAxis: 0 }] },
          data: be.si_spark.ts.map((t, i) => [t, be.si_spark.si[i]]) }] });
    }

    // alerts
    const al = desk.alerts || [];
    $("#alert-legend").textContent = meta && meta.thresholds && meta.thresholds.computed_at ? "thresholds: percentiles of history since 2024" : "";
    $("#alerts").innerHTML = al.length ? al.map((a) =>
      `<li class="lv-${a.level}"><span class="t">${hm(a.ts)}</span><span class="ty">${esc(a.type)}</span><span class="m">${esc(a.msg)}</span></li>`).join("")
      : '<li class="muted">No active alert.</li>';
  }

  // ------------------------------------------------------------------ REVISIONS
  function renderRevisions(rev) {
    if (!rev) { ["#ch-heat", "#ch-zone", "#ch-scatter"].forEach((s) => noData($(s))); return; }
    const c = chart("ch-heat");
    if (c && rev.heatmap.length) {
      const vals = rev.heatmap.map((d) => Math.abs(d[2])).sort((a, b) => a - b);
      const vmax = Math.max(100, vals[Math.floor(vals.length * 0.98)] || 100);
      const labels = rev.grid.map((t) => tfmt(t, { weekday: "short", hour: "2-digit", minute: "2-digit" }));
      c.setOption({
        ...baseOpt(), grid: { left: 52, right: 16, top: 10, bottom: 60 },
        tooltip: { position: "top", backgroundColor: "#0b0e11", borderColor: C.line, textStyle: { color: C.text, fontSize: 11 },
          formatter: (p) => `${rev.zones[p.value[1]]} · ${labels[p.value[0]]}<br>${signed(p.value[2], 0)} MW` },
        xAxis: { type: "category", data: labels, ...axisStyle, splitArea: { show: false },
          axisLabel: { color: C.muted, interval: 15, formatter: (v) => v.replace(/^\w+ /, (m) => m) } },
        yAxis: { type: "category", data: rev.zones, ...axisStyle },
        visualMap: { min: -vmax, max: vmax, calculable: true, orient: "horizontal", left: "center", bottom: 0,
          itemHeight: 140, itemWidth: 10, textStyle: { color: C.muted },
          inRange: { color: ["#ff3b30", "#5a1d1a", "#11161c", "#0f4a35", "#00c176"] }, text: ["+ more RES", "− less RES"] },
        series: [{ type: "heatmap", data: rev.heatmap, progressive: 0 }],
      });
    } else noData($("#ch-heat"), "no revision data for yesterday/today");

    // zone selector
    const seg = $("#zone-seg");
    seg.innerHTML = rev.zones.map((z) => `<button data-v="${z}" class="${z === state.zone ? "on" : ""}">${z}</button>`).join("");
    seg.onclick = (e) => { const v = e.target.dataset.v; if (!v) return; state.zone = v; renderRevisions(rev); };
    const s = rev.series[state.zone];
    const cz = chart("ch-zone");
    if (cz && s && s.ts.length) {
      const th = rev.thresholds[state.zone];
      const mk = (k) => s.ts.map((t, i) => [t, s[k][i]]);
      const big = th ? s.ts.map((t, i) => (s.id[i] !== null && s.da[i] !== null && Math.abs(s.id[i] - s.da[i]) > th) ? [t, s.id[i]] : null).filter(Boolean) : [];
      cz.setOption({
        ...baseOpt(),
        xAxis: timeAxis({ axisLabel: { color: C.muted, formatter: (v) => tfmt(new Date(v).toISOString(), { weekday: "short", hour: "2-digit" }) } }),
        yAxis: { type: "value", ...axisStyle, name: "MW", nameTextStyle: { color: C.muted } },
        tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => fmt(v, 0) + " MW" },
        series: [
          { name: "Day-ahead fc", type: "line", showSymbol: false, lineStyle: { width: 1.2, color: C.muted, type: "dashed" }, itemStyle: { color: C.muted }, data: mk("da") },
          { name: "Intraday fc", type: "line", showSymbol: false, lineStyle: { width: 1.6, color: C.amber }, itemStyle: { color: C.amber }, data: mk("id") },
          { name: "Actual", type: "line", showSymbol: false, lineStyle: { width: 1.4, color: C.green }, itemStyle: { color: C.green }, data: mk("actual") },
          { name: `|rev| > p95 (${fmt(th, 0)} MW)`, type: "scatter", symbolSize: 4, itemStyle: { color: "transparent", borderColor: C.amber }, data: big },
        ],
      }, true);
    } else {
      if (state.charts["ch-zone"]) { state.charts["ch-zone"].dispose(); delete state.charts["ch-zone"]; }
      noData($("#ch-zone"), (rev.unavailable || {})[state.zone] || `no forecast data for ${state.zone}`);
    }

    const sc = rev.be_scatter;
    const cc = chart("ch-scatter");
    if (cc && sc && sc.points.length) {
      cc.setOption({
        ...baseOpt(), legend: { show: false }, grid: { left: 56, right: 12, top: 16, bottom: 40 },
        tooltip: { trigger: "item", backgroundColor: "#0b0e11", borderColor: C.line, textStyle: { color: C.text, fontSize: 11 },
          formatter: (p) => `rev ${signed(p.value[0], 0)} MW<br>imb−DA ${signed(p.value[1], 1)} €/MWh` },
        xAxis: { type: "value", ...axisStyle, name: "revision MW", nameLocation: "middle", nameGap: 24, nameTextStyle: { color: C.muted } },
        yAxis: { type: "value", ...axisStyle, name: "imb − DA €/MWh", nameTextStyle: { color: C.muted } },
        series: [{ type: "scatter", symbolSize: 3, large: true, itemStyle: { color: C.amber, opacity: 0.45 }, data: sc.points }],
      });
      $("#scatter-note").innerHTML = `Pearson ρ = <b>${fmt(sc.corr, 3)}</b> over n = ${fmt(sc.n)} QH. A negative ρ means more renewables than forecast ↔ imbalance below DA. Ex-post diagnostic only, not a bot input.`;
    } else noData($("#ch-scatter"));
  }

  // ------------------------------------------------------------------ BOTS
  function renderBots(bots) {
    if (!bots || !bots.cum || !Object.keys(bots.cum).length) {
      ["#ch-cum", "#stats", "#ch-hour", "#trades", "#ch-fi"].forEach((s) => noData($(s))); return;
    }
    const c = chart("ch-cum");
    const marks = [];
    if (bots.live_start) marks.push({ xAxis: bots.live_start.slice(0, 10), label: { formatter: `LIVE since ${bots.live_start.slice(0, 10)}`, color: C.green, position: "insideEndTop" }, lineStyle: { color: C.green, width: 1.5 } });
    const rc = bots.regime_change && bots.regime_change.slice(0, 10);
    const lastDate = Object.values(bots.cum).map((x) => x.date[x.date.length - 1]).sort().pop();
    if (rc && lastDate && rc <= lastDate) marks.push({ xAxis: rc, label: { formatter: "Elia imbalance formula change", color: C.muted }, lineStyle: { color: C.muted, type: "dashed" } });
    if (c) c.setOption({
      ...baseOpt(), grid: { left: 64, right: 16, top: 28, bottom: 28 },
      xAxis: { type: "time", ...axisStyle, splitLine: { show: false } },
      yAxis: { type: "value", ...axisStyle, name: "EUR", nameTextStyle: { color: C.muted },
        axisLabel: { color: C.muted, formatter: (v) => Math.abs(v) >= 1000 ? fmt(v / 1000, 0) + "k" : fmt(v, 0) } },
      tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => signed(v, 0) + " €" },
      dataZoom: [{ type: "inside" }],
      series: Object.entries(bots.cum).map(([b, s], i) => ({
        name: BOT_NAMES[b], type: "line", showSymbol: false, lineStyle: { width: 1.6, color: BOT_COLORS[b] }, itemStyle: { color: BOT_COLORS[b] },
        data: s.date.map((d, k) => [d, s.cum[k]]),
        markLine: i === 0 && marks.length ? { symbol: "none", silent: true, data: marks } : undefined,
      })),
    });
    renderStats(bots);
    // trades
    const tr = bots.trades || [];
    $("#trades-note").textContent = bots.live_start ? `${bots.pending || 0} pending settlement` : "live not started: showing backtest trades";
    $("#trades").innerHTML = tr.length ? `<table class="dense"><thead><tr><th>Bot</th><th>Decided (Paris)</th><th>QH</th><th class="n">Pos</th><th class="n">DA</th><th class="n">Imb.</th><th class="n">P&amp;L €</th><th>Status</th></tr></thead><tbody>` +
      tr.map((t) => `<tr><td>${BOT_NAMES[t.bot]}</td><td>${dhm(t.decided_at)}</td><td>${dhm(t.qh)}</td>
        <td class="n ${cls(t.position)}">${t.position > 0 ? "▲ +1" : t.position < 0 ? "▼ −1" : "0"}</td>
        <td class="n">${fmt(t.da, 2)}</td><td class="n">${fmt(t.imb, 2)}</td><td class="n">${t.pnl === null ? "–" : pnlHTML(t.pnl, 2)}</td>
        <td><span class="tag ${t.status === "pending" ? "pending" : t.segment === "live" ? "live" : ""}">${t.segment === "live" ? t.status : "backtest"}</span></td></tr>`).join("") +
      "</tbody></table>" : '<div class="nodata">no trades</div>';
    // feature importance
    const fi = bots.feature_importance;
    const cf = chart("ch-fi");
    if (cf && fi) {
      const items = fi.items.slice().reverse();
      $("#fi-note").textContent = `gain share · model ${fi.cutoff.slice(0, 10)} · ${fi.n_models} weekly models`;
      cf.setOption({ ...baseOpt(), legend: { show: false }, grid: { left: 130, right: 16, top: 8, bottom: 20 },
        tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => pct(v, 1) },
        xAxis: { type: "value", ...axisStyle, axisLabel: { color: C.muted, formatter: (v) => Math.round(v * 100) + "%" } },
        yAxis: { type: "category", data: items.map((x) => x[0]), ...axisStyle },
        series: [{ type: "bar", data: items.map((x) => x[1]), itemStyle: { color: C.amber }, barWidth: "60%" }] });
    } else noData($("#ch-fi"), "no model yet");
  }

  function renderStats(bots) {
    const seg = state.statsSeg;
    document.querySelectorAll("#seg-stats button").forEach((b) => b.classList.toggle("on", b.dataset.v === seg));
    const st = bots.stats;
    const rows = [
      ["Total P&L (€)", (s) => pnlHTML(s.total_pnl, 0)],
      ["P&L per trade (€)", (s) => pnlHTML(s.pnl_per_trade, 2)],
      ["Hit ratio", (s) => pct(s.hit_ratio)],
      ["Sharpe (daily, ann.)", (s) => s.sharpe === null ? "–" : signed(s.sharpe, 2)],
      ["Max drawdown (€)", (s) => pnlHTML(s.max_drawdown, 0)],
      ["Exposure", (s) => pct(s.exposure)],
      ["Trades", (s) => fmt(s.trades)],
      ["Decided QH", (s) => fmt(s.decided_qh)],
      ["Period", (s) => s.first ? `${s.first.slice(0, 10)} → ${s.last.slice(0, 10)}` : "–"],
    ];
    const bk = ["rule", "ml"].filter((b) => st[b]);
    const empty = bk.every((b) => !st[b][seg] || !st[b][seg].decided_qh);
    $("#stats").innerHTML = empty ? `<div class="nodata">no ${seg} data${seg === "live" ? " yet: live trading starts at deployment" : ""}</div>` :
      `<table class="dense"><thead><tr><th></th>${bk.map((b) => `<th class="n">${BOT_NAMES[b]}</th>`).join("")}</tr></thead><tbody>` +
      rows.map(([k, f]) => `<tr><td>${k}</td>${bk.map((b) => `<td class="n">${f(st[b][seg])}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
    const ch = chart("ch-hour");
    if (ch) ch.setOption({
      ...baseOpt(), xAxis: { type: "category", data: [...Array(24).keys()].map((h) => String(h).padStart(2, "0")), ...axisStyle },
      yAxis: { type: "value", ...axisStyle },
      tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => signed(v, 0) + " €" },
      series: bk.map((b) => ({ name: BOT_NAMES[b], type: "bar", data: bots.by_hour[b][seg], itemStyle: { color: BOT_COLORS[b] } })),
    }, true);
  }

  // ------------------------------------------------------------------ BATTERY
  function renderBattery(bat) {
    if (!bat || !bat.league || !bat.league.length) { ["#league", "#ch-profile", "#ch-bhist"].forEach((s) => noData($(s))); return; }
    const w = state.leagueWin;
    document.querySelectorAll("#seg-league button").forEach((b) => b.classList.toggle("on", b.dataset.v === w));
    const L = bat.league.slice().sort((a, b) => (b[w] ?? -1e18) - (a[w] ?? -1e18));
    const best = L[0] && L[0][w];
    $("#league").innerHTML = `<table class="dense"><thead><tr><th>#</th><th>Zone</th><th class="n">Today €</th><th class="n">7d €</th><th class="n">30d €</th><th class="n">Total €</th><th class="n">€/day (total)</th><th class="n">Cycles/day 30d</th><th class="n">Avg daily spread 30d</th></tr></thead><tbody>` +
      L.map((r, i) => `<tr><td>${r[w] === null || r[w] === undefined ? "–" : i + 1}</td><td><span style="color:${ZONE_COLORS[r.zone]}">■</span> ${r.zone}</td>
        <td class="n">${fmt(r.today, 0)}</td><td class="n">${fmt(r.d7, 0)}</td><td class="n">${fmt(r.d30, 0)}</td><td class="n">${fmt(r.total, 0)}</td>
        <td class="n">${fmt(r.days ? r.total / r.days : null, 0)}</td><td class="n">${fmt(r.cycles_30d, 2)}</td><td class="n">${fmt(r.spread_30d, 0)}</td></tr>`).join("") +
      `</tbody></table><p class="note">Ranked by ${w === "today" ? "today" : w === "total" ? "total since " + (L[0].since || "–") : w}. ${best === null || best === undefined ? "Today's prices not available yet." : ""} Tomorrow (if DA published): ${L.filter((r) => r.tomorrow !== null && r.tomorrow !== undefined).map((r) => r.zone + " " + fmt(r.tomorrow, 0) + " €").join(" · ") || "–"}</p>`;
    // history
    const h = bat.history;
    const chh = chart("ch-bhist");
    if (chh && h && h.date.length) chh.setOption({
      ...baseOpt(), xAxis: { type: "time", ...axisStyle, splitLine: { show: false } }, yAxis: { type: "value", ...axisStyle },
      tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => fmt(v, 0) + " €" },
      series: Object.keys(h).filter((k) => k !== "date").map((z) => ({ name: z, type: "line", showSymbol: false,
        lineStyle: { width: 1.1, color: ZONE_COLORS[z] }, itemStyle: { color: ZONE_COLORS[z] }, data: h.date.map((d, i) => [d, h[z][i]]) })),
    });
    // profile
    const seg = $("#bzone-seg");
    seg.innerHTML = Object.keys(bat.profiles).map((z) => `<button data-v="${z}" class="${z === state.bzone ? "on" : ""}">${z}</button>`).join("");
    seg.onclick = (e) => { const v = e.target.dataset.v; if (!v) return; state.bzone = v; renderBattery(bat); };
    const p = bat.profiles[state.bzone];
    const cp = chart("ch-profile");
    if (cp && p) {
      $("#profile-note").textContent = `${p.zone} · ${p.date} · revenue ${fmt(p.revenue_eur, 0)} € · ${fmt(p.cycles, 2)} cycles`;
      cp.setOption({
        ...baseOpt(), grid: { left: 52, right: 52, top: 30, bottom: 28 },
        xAxis: timeAxis(),
        yAxis: [{ type: "value", ...axisStyle, name: "EUR/MWh", nameTextStyle: { color: C.muted } },
                { type: "value", ...axisStyle, name: "MW / MWh", nameTextStyle: { color: C.muted }, splitLine: { show: false }, min: -1, max: 2 }],
        tooltip: { ...baseOpt().tooltip, valueFormatter: (v) => fmt(v, 2) },
        series: [
          { name: "DA price", type: "line", step: "start", showSymbol: false, lineStyle: { color: C.text, width: 1.2 }, itemStyle: { color: C.text }, data: p.ts.map((t, i) => [t, p.price[i]]) },
          { name: "Discharge ▲", type: "bar", yAxisIndex: 1, barWidth: "70%", itemStyle: { color: C.green }, data: p.ts.map((t, i) => [t, p.discharge[i]]) },
          { name: "Charge ▼", type: "bar", yAxisIndex: 1, barWidth: "70%", barGap: "-100%", itemStyle: { color: C.red }, data: p.ts.map((t, i) => [t, -p.charge[i]]) },
          { name: "SoC", type: "line", yAxisIndex: 1, showSymbol: false, lineStyle: { color: C.amber, width: 1.4, type: "dashed" }, itemStyle: { color: C.amber }, data: p.ts.map((t, i) => [t, p.soc[i]]) },
        ],
      }, true);
    } else {
      if (state.charts["ch-profile"]) { state.charts["ch-profile"].dispose(); delete state.charts["ch-profile"]; }
      noData($("#ch-profile"), `no complete DA day for ${state.bzone}`);
    }
  }

  // ------------------------------------------------------------------ METHODOLOGY numbers
  function fillMethodology(meta, bots) {
    const set = (k, v) => document.querySelectorAll(`[data-fill="${k}"]`).forEach((e) => { e.textContent = v; });
    if (meta) {
      set("lead", meta.decision_lead_min); set("lag", meta.rt_lag_qh);
      const th = (meta.thresholds || {}).revision_p95_mw || {};
      set("rev-th", Object.entries(th).map(([z, v]) => `${z} ${fmt(v)} MW`).join(", ") || "not computed yet");
    }
    if (bots) { set("T", fmt(bots.rule_threshold_mw)); set("pth", bots.ml_proba_threshold); }
  }

  // ------------------------------------------------------------------ router
  const TABS = ["desk", "revisions", "bots", "battery", "methodology"];
  function show(tab) {
    if (!TABS.includes(tab)) tab = "desk";
    document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.dataset.tab !== tab; });
    document.querySelectorAll(".tabs a").forEach((a) => a.setAttribute("aria-selected", a.dataset.tab === tab));
    $("#tab-select").value = tab;
    render(tab);
    requestAnimationFrame(() => Object.values(state.charts).forEach((c) => c.resize()));
  }
  function render(tab) {
    if (state.rendered[tab] || !state.loaded) return;
    state.rendered[tab] = true;
    const d = state.data;
    try {
      if (tab === "desk") renderDesk(d.desk, d.meta);
      if (tab === "revisions") renderRevisions(d.revisions);
      if (tab === "bots") renderBots(d.bots);
      if (tab === "battery") renderBattery(d.battery);
    } catch (e) { console.error(tab, e); }
  }
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  $("#tab-select").addEventListener("change", (e) => { location.hash = e.target.value; });
  $("#seg-stats").addEventListener("click", (e) => { const v = e.target.dataset.v; if (!v) return; state.statsSeg = v; renderStats(state.data.bots); });
  $("#seg-league").addEventListener("click", (e) => { const v = e.target.dataset.v; if (!v) return; state.leagueWin = v; renderBattery(state.data.battery); });

  // ------------------------------------------------------------------ boot
  async function boot() {
    const meta = await getJSON("meta", Date.now());
    const v = meta ? meta.last_update_utc : Date.now();
    const [desk, revisions, bots, battery] = await Promise.all(["desk", "revisions", "bots", "battery"].map((n) => getJSON(n, v)));
    state.data = { meta, desk, revisions, bots, battery };
    state.loaded = true;
    renderHeader(meta);
    renderTicker(desk);
    fillMethodology(meta, bots);
    show(location.hash.slice(1) || "desk");
  }
  boot();
  // refresh every 5 minutes if a newer export exists
  setInterval(async () => {
    const m = await getJSON("meta", Date.now());
    if (m && state.data.meta && m.last_update_utc !== state.data.meta.last_update_utc) location.reload();
  }, 5 * 60 * 1000);
})();
