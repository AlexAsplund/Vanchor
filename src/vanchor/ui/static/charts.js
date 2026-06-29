/* Vanchor-NG — charts + auto-tune module.
 *
 * The uPlot live charts (heading/target, thrust/steering, SOG, dist→anchor/XTE)
 * that fill from telemetry while the charts card is open, and the PID auto-tuner
 * panel (run a tune job, render baseline→tuned params/metrics, apply to live).
 */
"use strict";

(function () {
  const { $ } = VA.ui;

  // ===== charts (uPlot) ====================================================
  const CHART_CAP = 600;
  const chartT = [];
  const series = { heading: [], target: [], thrust: [], steering: [], sog: [], dist: [], xte: [] };
  let chartT0 = null;
  let charts = [];
  let chartsBuilt = false;
  function defNum(v) { return Number.isFinite(v) ? v : null; }

  VA.onTelemetry(function pushChartSample(t) {
    const card = $("charts-card");
    if (!card || !card.open) return;
    const now = performance.now() / 1000;
    if (chartT0 === null) chartT0 = now;
    chartT.push(now - chartT0);
    series.heading.push(defNum(t.heading_deg));
    series.target.push(defNum(t.target_heading));
    const motor = t.motor || {};
    series.thrust.push(defNum(motor.thrust));
    series.steering.push(defNum(motor.steering));
    series.sog.push(defNum(t.sog_knots));
    series.dist.push(defNum(t.distance_to_anchor_m));
    series.xte.push(defNum(t.cross_track_m));
    if (chartT.length > CHART_CAP) { chartT.shift(); for (const k in series) series[k].shift(); }
    redrawCharts();
  });

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }
  function chartTheme() {
    const dark = !document.body.classList.contains("light");
    return {
      accent: cssVar("--accent", "#1be4ff"), active: cssVar("--active", "#22d3a6"),
      text: cssVar("--text", "#e7eaee"), muted: cssVar("--muted", "#9aa3ad"),
      grid: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.10)",
      warn: "#ffb454", stop: cssVar("--stop", "#ff5a7a"),
    };
  }
  function buildCharts() {
    const host = $("charts");
    if (!host || typeof uPlot === "undefined") return;
    host.innerHTML = "";
    charts.forEach((c) => c.destroy());
    charts = [];
    const th = chartTheme();
    const W = 300, H = 96;
    const axis = (label) => ({
      stroke: th.muted, grid: { stroke: th.grid, width: 1 }, ticks: { stroke: th.grid, width: 1 },
      font: "10px Inter, system-ui, sans-serif", size: 30, label,
      labelFont: "10px Inter, system-ui, sans-serif", labelSize: 14,
    });
    const xAxis = {
      stroke: th.muted, grid: { stroke: th.grid, width: 1 }, ticks: { stroke: th.grid, width: 1 },
      font: "10px Inter, system-ui, sans-serif", size: 22,
    };
    const base = (title, seriesDefs, dataKeys) => ({
      opts: {
        title, width: W, height: H, cursor: { show: true }, legend: { show: false },
        padding: [6, 6, 2, 0], axes: [xAxis, axis()], series: [{}].concat(seriesDefs),
      },
      keys: dataKeys,
    });
    const defs = [
      base("Heading / target (°)", [{ stroke: th.accent, width: 1.4 }, { stroke: th.warn, width: 1.4, dash: [4, 3] }], ["heading", "target"]),
      base("Thrust / steering", [{ stroke: th.active, width: 1.4 }, { stroke: th.accent, width: 1.4 }], ["thrust", "steering"]),
      base("SOG (kn)", [{ stroke: th.active, width: 1.4 }], ["sog"]),
      base("Dist→anchor / XTE (m)", [{ stroke: th.accent, width: 1.4 }, { stroke: th.stop, width: 1.4 }], ["dist", "xte"]),
    ];
    charts = defs.map((d) => { const u = new uPlot(d.opts, chartData(d.keys), host); u._keys = d.keys; return u; });
    chartsBuilt = true;
  }
  function chartData(keys) { return [chartT].concat(keys.map((k) => series[k])); }
  function redrawCharts() {
    const card = $("charts-card");
    if (!card || !card.open) return;
    if (!chartsBuilt) buildCharts();
    charts.forEach((u) => u.setData(chartData(u._keys)));
  }
  const chartsCard = $("charts-card");
  if (chartsCard) chartsCard.addEventListener("toggle", () => { if (chartsCard.open && !chartsBuilt) buildCharts(); });
  $("charts-clear").addEventListener("click", () => {
    chartT.length = 0; for (const k in series) series[k].length = 0; chartT0 = null; redrawCharts();
  });

  // ===== auto-tune (PID) ===================================================
  const tuneJobBtns = document.querySelectorAll("#tune-jobs button[data-job]");
  const tuneStatus = $("tune-status");
  const tuneResult = $("tune-result");
  const tuneApply = $("tune-apply");
  let tuneBusy = false, tuneLastJob = null;
  function tuneNum(v) {
    if (v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v))) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    if (n === 0) return "0";
    const a = Math.abs(n);
    let s = (a < 0.001 || a >= 100000) ? n.toExponential(2) : n.toPrecision(4);
    if (s.indexOf("e") === -1 && s.indexOf(".") !== -1) s = s.replace(/\.?0+$/, "");
    return s;
  }
  function setTuneBusy(busy) {
    tuneBusy = busy;
    tuneJobBtns.forEach((b) => { b.disabled = busy; });
    if (tuneApply) tuneApply.disabled = busy || !tuneLastJob;
  }
  function tuneGrid(parent, baseline, tuned) {
    const grid = document.createElement("div");
    grid.className = "tune-grid";
    const keys = Object.keys(tuned || {});
    if (!keys.length) { parent.appendChild(document.createTextNode("—")); return; }
    keys.forEach((k) => {
      const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
      const bEl = document.createElement("span"); bEl.className = "base"; bEl.textContent = baseline ? tuneNum(baseline[k]) : "—";
      const tEl = document.createElement("span"); tEl.className = "tuned"; tEl.textContent = "→ " + tuneNum(tuned[k]);
      grid.append(kEl, bEl, tEl);
    });
    parent.appendChild(grid);
  }
  function tuneInfoGrid(parent, baseInfo, tunedInfo) {
    const keys = Object.keys(tunedInfo || baseInfo || {});
    if (!keys.length) return;
    const grid = document.createElement("div");
    grid.className = "tune-grid";
    keys.forEach((k) => {
      const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
      const bEl = document.createElement("span"); bEl.className = "base"; bEl.textContent = baseInfo ? tuneNum(baseInfo[k]) : "—";
      const tEl = document.createElement("span"); tEl.className = "tuned"; tEl.textContent = "→ " + tuneNum(tunedInfo ? tunedInfo[k] : undefined);
      grid.append(kEl, bEl, tEl);
    });
    parent.appendChild(grid);
  }
  function renderTuneResult(r) {
    tuneResult.innerHTML = "";
    const ph = document.createElement("div");
    ph.className = "tune-section"; ph.textContent = "Parameters (baseline → tuned)";
    tuneResult.appendChild(ph);
    tuneGrid(tuneResult, r.baseline_params, r.tuned_params);
    const bc = Number(r.baseline_cost), tc = Number(r.tuned_cost);
    const cost = document.createElement("div");
    cost.className = "tune-cost";
    cost.append(document.createTextNode("Cost "));
    const bcb = document.createElement("b"); bcb.textContent = tuneNum(bc);
    const tcb = document.createElement("b"); tcb.textContent = tuneNum(tc);
    cost.append(bcb, document.createTextNode(" → "), tcb);
    if (Number.isFinite(bc) && Number.isFinite(tc) && bc !== 0) {
      const imp = (1 - tc / bc) * 100;
      const span = document.createElement("span");
      span.className = "tune-imp " + (imp >= 0 ? "good" : "bad");
      span.textContent = "  (" + (imp >= 0 ? "−" : "+") + Math.abs(imp).toFixed(1) + "% cost)";
      cost.appendChild(span);
    }
    tuneResult.appendChild(cost);
    const evals = document.createElement("div");
    evals.className = "hint";
    evals.textContent = (Number.isFinite(Number(r.evals)) ? r.evals : "?") + " evaluations";
    tuneResult.appendChild(evals);
    if ((r.baseline_info && Object.keys(r.baseline_info).length) || (r.tuned_info && Object.keys(r.tuned_info).length)) {
      const ih = document.createElement("div");
      ih.className = "tune-section"; ih.textContent = "Metrics (baseline → tuned)";
      tuneResult.appendChild(ih);
      tuneInfoGrid(tuneResult, r.baseline_info, r.tuned_info);
    }
  }
  async function runTune(job, apply) {
    if (tuneBusy) return;
    setTuneBusy(true);
    tuneStatus.className = "hint busy";
    tuneStatus.textContent = (apply ? "applying " : "tuning ") + job + "… (a few seconds)";
    VA.logLine("» tune " + job + (apply ? " (apply)" : ""));
    try {
      const r = await VA.postJSON("/api/tune", { job, max_evals: 50, apply: !!apply });
      if (r && r.error) {
        tuneStatus.className = "hint err"; tuneStatus.textContent = "error: " + r.error;
        VA.logLine("tune error: " + r.error); return;
      }
      if (apply && r && r.applied) {
        tuneStatus.className = "hint ok"; tuneStatus.textContent = "✓ applied to the live controller";
        VA.logLine("tune applied: " + job);
      } else {
        tuneStatus.className = "hint";
        tuneStatus.textContent = "tuned " + job + " in " + (Number.isFinite(Number(r.evals)) ? r.evals : "?") + " evals";
      }
      tuneLastJob = job;
      renderTuneResult(r);
      if (tuneApply) tuneApply.hidden = false;
    } catch (err) {
      tuneStatus.className = "hint err"; tuneStatus.textContent = "request failed: " + err;
      VA.logLine("tune request failed: " + err);
    } finally { setTuneBusy(false); }
  }
  tuneJobBtns.forEach((b) => b.addEventListener("click", () => runTune(b.dataset.job, false)));
  if (tuneApply) tuneApply.addEventListener("click", () => { if (tuneLastJob) runTune(tuneLastJob, true); });
})();
