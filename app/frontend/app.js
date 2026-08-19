const MAX_PANELS = 4;
const SAT_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const SAT_ATTR = "Tiles &copy; Esri";
const DIAMETER_BREAKS = [3, 5, 7, 9];
const DIAMETER_COLORS = ["#2166ac", "#67a9cf", "#a1d76a", "#f4a582", "#d6604d"];

let META = null;
let AREA_HIERARCHY = [];
let panels = [];
let nextPanelUid = 1;

const GRID_LAYOUT = {
  1: { columns: "1fr", rows: "1fr" },
  2: { columns: "1fr 1fr", rows: "1fr" },
  3: { columns: "1fr 1fr 1fr", rows: "1fr" },
  4: { columns: "1fr 1fr", rows: "1fr 1fr" },
};

let pieChart, growthChart;

function diameterColor(d) {
  if (d == null) return "#888";
  for (let i = 0; i < DIAMETER_BREAKS.length; i++) {
    if (d < DIAMETER_BREAKS[i]) return DIAMETER_COLORS[i];
  }
  return DIAMETER_COLORS[DIAMETER_COLORS.length - 1];
}

function statusColor(status) {
  return (META && META.status_colors[status]) || "#888";
}

function statusLabel(status) {
  return (META && META.status_labels[status]) || status;
}

function periodeLabel(p) {
  const m = /^(\d{4}) (R\d)$/.exec(p || "");
  return m ? `Survey ${m[2]} - ${m[1]}` : p;
}

function periodeShort(p) {
  const m = /^(\d{4}) (R\d)$/.exec(p || "");
  return m ? `${m[2]} - ${m[1]}` : p;
}

function panelHeaderText(panel) {
  return periodeShort(panel.periode);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function buildQuery(params) {
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") usp.set(k, v);
  });
  return usp.toString();
}

// ---------------------------------------------------------------------
// Map panels
// ---------------------------------------------------------------------
function createPanelDom(panel) {
  const el = document.createElement("div");
  el.className = "map-panel";
  el.id = `panel-${panel.uid}`;
  const rname = `tematik-${panel.uid}`;
  const periodOptions = META.geo_periods
    .map((p) => `<option value="${p}" ${p === panel.periode ? "selected" : ""}>${periodeLabel(p)}</option>`)
    .join("");

  const regions = uniquePreserveOrder(AREA_HIERARCHY.map((a) => a.region));
  const defaultRegion = regions[0] || "";
  const estatesInRegion = uniquePreserveOrder(areasInRegion(defaultRegion).map((a) => a.estate));
  const defaultEstate = estatesInRegion[0] || "";
  const bloksInEstate = areasInEstate(defaultRegion, defaultEstate);
  panel.bloks = bloksInEstate.map((b) => b.id_blok);

  const regionOptions = regions.map((r) => `<option value="${r}">${r}</option>`).join("");
  const estateOptions = estatesInRegion.map((e) => `<option value="${e}">${e}</option>`).join("");
  const blokOptions =
    `<option value="">Semua Blok</option>` +
    bloksInEstate.map((b) => `<option value="${b.id_blok}">${b.nama_blok || b.id_blok}</option>`).join("");

  el.innerHTML = `
    <div class="map-panel-map" id="map-${panel.uid}"></div>
    <span class="periode-badge">${panelHeaderText(panel)}</span>
    <div class="panel-overlay-controls">
      <button class="zoom-btn" title="Zoom ke layer">&#128269;</button>
      <button class="layer-btn" title="Layer">&#9776;</button>
      <button class="expand-btn" title="Perbesar panel">&#9974;</button>
    </div>
    <div class="layer-panel" id="layerpanel-${panel.uid}">
      <div class="layer-panel-title">Layers</div>
      <div class="layer-field-row">
        <label>Region</label>
        <select class="layer-region-select">${regionOptions}</select>
      </div>
      <div class="layer-field-row">
        <label>Estate</label>
        <select class="layer-estate-select">${estateOptions}</select>
      </div>
      <div class="layer-field-row">
        <label>Blok</label>
        <select class="layer-blok-select">${blokOptions}</select>
      </div>
      <div class="layer-field-row layer-field-row-last">
        <label>Periode Survey</label>
        <select class="layer-periode-select">${periodOptions}</select>
      </div>
      <label class="layer-item">
        <input type="checkbox" data-layer="base" checked /> Drone (Citra Satelit)
      </label>
      <label class="layer-item">
        <input type="checkbox" data-layer="markers" checked /> Peta Tematik
      </label>
      <div class="layer-sub">
        <label class="layer-radio">
          <input type="radio" name="${rname}" value="health" checked /> Canopy Health
        </label>
        <label class="layer-radio">
          <input type="radio" name="${rname}" value="diameter" /> Diameter Kanopi
        </label>
      </div>
    </div>
  `;
  return el;
}

function initPanelMap(panel) {
  const map = L.map(`map-${panel.uid}`, {
    zoomControl: true,
    preferCanvas: true,
  }).setView([-2.2, 111.7], 13);

  const sat = L.tileLayer(SAT_URL, { attribution: SAT_ATTR, maxZoom: 19 }).addTo(map);
  const markersLayer = L.layerGroup().addTo(map);

  panel.map = map;
  panel.satLayer = sat;
  panel.markersLayer = markersLayer;
}

async function loadPanelData(panel) {
  const q = buildQuery({ periode: panel.periode, bloks: (panel.bloks || []).join(",") });
  let data;
  try {
    data = await fetchJSON(`/api/canopy?${q}`);
  } catch (e) {
    console.error("Gagal load canopy", panel.uid, e);
    return;
  }

  panel.markersLayer.clearLayers();

  const layer = L.geoJSON(data, {
    pointToLayer: (feature, latlng) => {
      const props = feature.properties;
      const color =
        panel.tematik === "diameter" ? diameterColor(props.DIAMETER) : statusColor(props.CANOPY);
      return L.circleMarker(latlng, {
        radius: 3,
        color,
        fillColor: color,
        fillOpacity: 0.85,
        weight: 0,
      });
    },
    onEachFeature: (feature, lyr) => {
      const p = feature.properties;
      lyr.bindPopup(
        `<b>${p.Blok_ID}</b><br/>Status: ${statusLabel(p.CANOPY)}<br/>Diameter: ${
          p.DIAMETER != null ? p.DIAMETER.toFixed(2) + " m" : "-"
        }<br/>Kelas: ${p.KELAS || "-"}`
      );
    },
  });
  layer.addTo(panel.markersLayer);

  if (data.features.length && panel.firstLoad !== false) {
    panel.map.fitBounds(layer.getBounds(), { padding: [10, 10], maxZoom: 15 });
    panel.firstLoad = false;
  }

  const badge = document.querySelector(`#panel-${panel.uid} .periode-badge`);
  if (badge) badge.textContent = panelHeaderText(panel);
}

function zoomPanelToLayer(panel) {
  if (!panel || !panel.map || !panel.markersLayer) return;

  const layers = panel.markersLayer.getLayers();
  if (!layers.length) return;

  const bounds = L.featureGroup(layers).getBounds();
  if (!bounds || !bounds.isValid()) return;

  panel.map.fitBounds(bounds.pad(0.12), { padding: [10, 10], maxZoom: 16 });
}

function wirePanelControls(panel) {
  const root = document.getElementById(`panel-${panel.uid}`);
  root.querySelector(".zoom-btn").addEventListener("click", () => {
    zoomPanelToLayer(panel);
  });

  root.querySelector(".expand-btn").addEventListener("click", () => {
    const isExpanded = root.classList.contains("expanded");
    document.querySelectorAll(".map-panel").forEach((p) => {
      p.classList.remove("expanded");
      p.classList.remove("hidden-panel");
    });
    if (!isExpanded) {
      root.classList.add("expanded");
      document.querySelectorAll(".map-panel").forEach((p) => {
        if (p !== root) p.classList.add("hidden-panel");
      });
    }
    requestAnimationFrame(() => {
      setTimeout(() => panels.forEach((p) => p.map && p.map.invalidateSize()), 50);
    });
  });

  wireLayerPanel(panel);
}

function wireLayerPanel(panel) {
  const root = document.getElementById(`panel-${panel.uid}`);
  const layerBtn = root.querySelector(".layer-btn");
  const layerPanelEl = root.querySelector(".layer-panel");

  layerBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = !layerPanelEl.classList.contains("open");
    closeAllLayerPanels();
    if (willOpen) {
      layerPanelEl.classList.add("open");
      layerBtn.classList.add("active");
    }
  });
  layerPanelEl.addEventListener("click", (e) => e.stopPropagation());

  layerPanelEl.querySelector(".layer-region-select").addEventListener("change", async () => {
    refreshPanelEstateOptions(layerPanelEl);
    refreshPanelBlokOptions(layerPanelEl);
    await applyPanelAreaFilter(panel);
  });
  layerPanelEl.querySelector(".layer-estate-select").addEventListener("change", async () => {
    refreshPanelBlokOptions(layerPanelEl);
    await applyPanelAreaFilter(panel);
  });
  layerPanelEl.querySelector(".layer-blok-select").addEventListener("change", async () => {
    await applyPanelAreaFilter(panel);
  });

  layerPanelEl.querySelector(".layer-periode-select").addEventListener("change", async (e) => {
    panel.periode = e.target.value;
    panel.firstLoad = true;
    await loadPanelData(panel);
    updateGrowthChartHighlights();
    if (panels[0] === panel) {
      refreshPieChart();
      refreshInsights();
    }
  });

  layerPanelEl.querySelector('[data-layer="base"]').addEventListener("change", (e) => {
    panel.showBase = e.target.checked;
    if (panel.showBase) {
      panel.satLayer.addTo(panel.map);
    } else {
      panel.map.removeLayer(panel.satLayer);
    }
  });

  layerPanelEl.querySelector('[data-layer="markers"]').addEventListener("change", (e) => {
    panel.showMarkers = e.target.checked;
    if (panel.showMarkers) {
      panel.markersLayer.addTo(panel.map);
    } else {
      panel.map.removeLayer(panel.markersLayer);
    }
  });

  layerPanelEl.querySelectorAll('input[type="radio"]').forEach((radio) => {
    radio.addEventListener("change", async (e) => {
      if (!e.target.checked) return;
      panel.tematik = e.target.value;
      await loadPanelData(panel);
    });
  });
}

function closeAllLayerPanels() {
  document.querySelectorAll(".layer-panel.open").forEach((p) => p.classList.remove("open"));
  document.querySelectorAll(".layer-btn.active").forEach((b) => b.classList.remove("active"));
}

function applyGridLayout(count) {
  const grid = document.getElementById("mapsGrid");
  const layout = GRID_LAYOUT[count] || GRID_LAYOUT[1];
  grid.style.gridTemplateColumns = layout.columns;
  grid.style.gridTemplateRows = layout.rows;
}

function updateExpandButtonsVisibility() {
  document.querySelectorAll(".map-panel .expand-btn").forEach((btn) => {
    btn.style.display = panels.length <= 1 ? "none" : "inline-block";
  });
}

function defaultPeriodeForIndex(i) {
  const periods = META.geo_periods;
  const idx = periods.length - 1 - (i % periods.length);
  return periods[idx];
}

async function addPanel() {
  const grid = document.getElementById("mapsGrid");
  const panel = {
    uid: nextPanelUid++,
    periode: defaultPeriodeForIndex(panels.length),
    tematik: "health",
    showBase: true,
    showMarkers: true,
  };
  panels.push(panel);

  grid.appendChild(createPanelDom(panel));
  initPanelMap(panel);
  wirePanelControls(panel);
  await loadPanelData(panel);
}

function removeLastPanel() {
  const panel = panels.pop();
  if (!panel) return;
  if (panel.map) panel.map.remove();
  const dom = document.getElementById(`panel-${panel.uid}`);
  if (dom) dom.remove();
}

async function setPanelCount(n) {
  n = Math.max(1, Math.min(MAX_PANELS, n));
  document.querySelectorAll(".map-panel").forEach((p) => {
    p.classList.remove("expanded");
    p.classList.remove("hidden-panel");
  });

  // Terapkan layout grid final SEBELUM bikin peta baru - Leaflet membaca ukuran
  // container saat init, kalau grid masih ukuran lama hasil fitBounds-nya salah.
  applyGridLayout(n);

  while (panels.length < n) {
    await addPanel();
  }
  while (panels.length > n) {
    removeLastPanel();
  }

  updateGrowthChartHighlights();
  updateExpandButtonsVisibility();
  requestAnimationFrame(() => {
    setTimeout(() => panels.forEach((p) => p.map && p.map.invalidateSize()), 50);
  });
}

async function initPanels() {
  applyGridLayout(1);
  await setPanelCount(1);
}

// ---------------------------------------------------------------------
// Settings panel
// ---------------------------------------------------------------------
function buildSettingsPanel() {
  const countSelect = document.getElementById("panelCountSelect");
  countSelect.value = String(panels.length);
}

function wireSettingsPanel() {
  const button = document.getElementById("btnSettings");
  const settingsPanel = document.getElementById("settingsPanel");
  const close = () => {
    settingsPanel.classList.remove("open");
    button.classList.remove("active");
  };

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = !settingsPanel.classList.contains("open");
    closeAllLayerPanels();
    close();
    if (willOpen) {
      buildSettingsPanel();
      settingsPanel.classList.add("open");
      button.classList.add("active");
    }
  });
  settingsPanel.addEventListener("click", (e) => e.stopPropagation());

  document.getElementById("panelCountSelect").addEventListener("change", async (e) => {
    await setPanelCount(Number(e.target.value));
    buildSettingsPanel();
  });

  document.addEventListener("click", () => {
    closeAllLayerPanels();
    close();
  });

}

// ---------------------------------------------------------------------
// Filter area per panel (Region / Estate / Blok) - hidup di dalam layer panel
// ---------------------------------------------------------------------
function uniquePreserveOrder(arr) {
  return [...new Set(arr)];
}

function areasInRegion(region) {
  return AREA_HIERARCHY.filter((a) => a.region === region);
}

function areasInEstate(region, estate) {
  return AREA_HIERARCHY.filter((a) => a.region === region && a.estate === estate);
}

function populateSelect(select, options, formatFn) {
  select.innerHTML = options.map((o) => `<option value="${o}">${formatFn ? formatFn(o) : o}</option>`).join("");
}

function refreshPanelEstateOptions(layerPanelEl) {
  const region = layerPanelEl.querySelector(".layer-region-select").value;
  const estates = uniquePreserveOrder(areasInRegion(region).map((a) => a.estate));
  populateSelect(layerPanelEl.querySelector(".layer-estate-select"), estates);
}

function refreshPanelBlokOptions(layerPanelEl) {
  const region = layerPanelEl.querySelector(".layer-region-select").value;
  const estate = layerPanelEl.querySelector(".layer-estate-select").value;
  const bloks = areasInEstate(region, estate);
  const sel = layerPanelEl.querySelector(".layer-blok-select");
  sel.innerHTML =
    `<option value="">Semua Blok</option>` +
    bloks.map((b) => `<option value="${b.id_blok}">${b.nama_blok || b.id_blok}</option>`).join("");
}

function computePanelBloks(layerPanelEl) {
  const region = layerPanelEl.querySelector(".layer-region-select").value;
  const estate = layerPanelEl.querySelector(".layer-estate-select").value;
  const blokVal = layerPanelEl.querySelector(".layer-blok-select").value;
  if (blokVal) return [blokVal];
  return areasInEstate(region, estate).map((b) => b.id_blok);
}

async function applyPanelAreaFilter(panel) {
  const root = document.getElementById(`panel-${panel.uid}`);
  const layerPanelEl = root.querySelector(".layer-panel");
  panel.bloks = computePanelBloks(layerPanelEl);
  panel.firstLoad = true;
  await loadPanelData(panel);

  if (panels[0] === panel) {
    await Promise.all([refreshPieChart(), refreshGrowthChart(), refreshInsights()]);
  }
}

// ---------------------------------------------------------------------
// Analytics sidebar
// ---------------------------------------------------------------------
function chartColors(statuses) {
  return statuses.map(statusColor);
}

const pieLabelsPlugin = {
  id: "pieLabels",
  afterDatasetsDraw(chart) {
    const meta = chart.getDatasetMeta(0);
    const values = chart.data.datasets[0].data;
    const { ctx } = chart;
    ctx.save();
    ctx.font = "700 12px 'Segoe UI', sans-serif";
    ctx.fillStyle = "#ffffff";
    ctx.strokeStyle = "rgba(4, 10, 20, 0.65)";
    ctx.lineWidth = 3;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    meta.data.forEach((arc, i) => {
      const val = values[i];
      if (!val) return;
      const pos = arc.tooltipPosition();
      const label = `${Math.round(val)}%`;
      ctx.strokeText(label, pos.x, pos.y);
      ctx.fillText(label, pos.x, pos.y);
    });
    ctx.restore();
  },
};

async function refreshPieChart() {
  const periode = panels[0] ? panels[0].periode : META.geo_periods[META.geo_periods.length - 1];
  const bloks = panels[0] ? panels[0].bloks || [] : [];
  document.getElementById("pieTitle").textContent = `Homogenitas Kanopi (${periodeLabel(periode)})`;
  const data = await fetchJSON(`/api/area?${buildQuery({ periode, bloks: bloks.join(",") })}`);
  const labels = data.map((d) => statusLabel(d.status));
  const values = data.map((d) => d.persen);
  const colors = chartColors(data.map((d) => d.status));

  if (pieChart) {
    pieChart.data.labels = labels;
    pieChart.data.datasets[0].data = values;
    pieChart.data.datasets[0].backgroundColor = colors;
    pieChart.update();
    return;
  }

  const ctx = document.getElementById("pieChart");
  pieChart = new Chart(ctx, {
    type: "pie",
    data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
    plugins: [pieLabelsPlugin],
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "right", labels: { color: "#e5e9f0", boxWidth: 10, font: { size: 10 } } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${c.formattedValue}%` } },
      },
    },
  });
}

const GROWTH_POINT_COLOR = "#2dd4bf";
const GROWTH_ACTIVE_COLOR = "#f97316";

function activePanelPeriodes() {
  return panels.map((p) => p.periode);
}

function growthPointStyles(labels) {
  const active = activePanelPeriodes();
  return {
    colors: labels.map((l) => (active.includes(l) ? GROWTH_ACTIVE_COLOR : GROWTH_POINT_COLOR)),
    radii: labels.map((l) => (active.includes(l) ? 6 : 3)),
  };
}

function updateGrowthChartHighlights() {
  if (!growthChart) return;
  const { colors, radii } = growthPointStyles(growthChart.data.labels);
  growthChart.data.datasets[0].pointBackgroundColor = colors;
  growthChart.data.datasets[0].pointBorderColor = colors;
  growthChart.data.datasets[0].pointRadius = radii;
  growthChart.update();
}

async function refreshGrowthChart() {
  const bloks = panels[0] ? panels[0].bloks || [] : [];
  const data = await fetchJSON(`/api/growth?${buildQuery({ bloks: bloks.join(",") })}`);
  const labels = data.map((d) => d.periode);
  const values = data.map((d) => d.avg_diameter);
  const { colors, radii } = growthPointStyles(labels);

  if (growthChart) {
    growthChart.data.labels = labels;
    growthChart.data.datasets[0].data = values;
    growthChart.data.datasets[0].pointBackgroundColor = colors;
    growthChart.data.datasets[0].pointBorderColor = colors;
    growthChart.data.datasets[0].pointRadius = radii;
    growthChart.update();
    return;
  }

  const ctx = document.getElementById("growthChart");
  growthChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          data: values,
          borderColor: GROWTH_POINT_COLOR,
          backgroundColor: "rgba(45,212,191,0.15)",
          fill: true,
          tension: 0.3,
          pointRadius: radii,
          pointBackgroundColor: colors,
          pointBorderColor: colors,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8b9ab5", font: { size: 8 } }, grid: { display: false } },
        y: { ticks: { color: "#8b9ab5", font: { size: 9 } }, grid: { color: "#1f2a44" } },
      },
    },
  });
}

// ---------------------------------------------------------------------
// Keterangan / Report - analisis singkat dari data yang sedang tampil
// ---------------------------------------------------------------------
function fmtNum(n, digits = 2) {
  return n.toLocaleString("id-ID", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function buildInsightLines(growthData, areaData, currentPeriode) {
  const lines = [];

  if (areaData && areaData.length) {
    const total = areaData.reduce((s, d) => s + d.jumlah, 0);
    const abnormal = areaData.find((d) => d.status === "abnormal canopy");
    const well = areaData.find((d) => d.status === "well canopy");
    if (total > 0) {
      lines.push(
        `Kondisi terkini (${periodeLabel(currentPeriode)}): dari <b>${total.toLocaleString("id-ID")} pohon</b> terdeteksi, ` +
          `${abnormal ? fmtNum(abnormal.persen, 1) + "%" : "0%"} berstatus Abnormal dan ` +
          `${well ? fmtNum(well.persen, 1) + "%" : "0%"} berstatus Well.`
      );
    }
  }

  if (growthData.length >= 2) {
    const values = growthData.map((d) => d.avg_diameter);
    const avgAll = values.reduce((a, b) => a + b, 0) / values.length;
    lines.push(`Rata-rata diameter kanopi sepanjang periode tercatat: <b>${fmtNum(avgAll)} m</b>.`);

    const first = growthData[0];
    const last = growthData[growthData.length - 1];
    const totalGrowth = last.avg_diameter - first.avg_diameter;
    const avgGrowthPerPeriode = totalGrowth / (growthData.length - 1);
    const trendWord = totalGrowth >= 0 ? "meningkat" : "menurun";
    lines.push(
      `Diameter kanopi ${trendWord} dari ${periodeLabel(first.periode)} (${fmtNum(first.avg_diameter)} m) ke ${periodeLabel(last.periode)} (${fmtNum(last.avg_diameter)} m), total ${totalGrowth >= 0 ? "+" : ""}${fmtNum(totalGrowth)} m.`
    );
    lines.push(
      `Rata-rata pertumbuhan kanopi: <b>${avgGrowthPerPeriode >= 0 ? "+" : ""}${fmtNum(avgGrowthPerPeriode)} m per periode</b>.`
    );

    let maxDelta = -Infinity;
    let maxIdx = -1;
    for (let i = 1; i < growthData.length; i++) {
      const delta = growthData[i].avg_diameter - growthData[i - 1].avg_diameter;
      if (delta > maxDelta) {
        maxDelta = delta;
        maxIdx = i;
      }
    }
    if (maxIdx > 0 && maxDelta > 0) {
      lines.push(
        `Peningkatan kanopi tertinggi terjadi menjelang periode <b>${periodeLabel(growthData[maxIdx].periode)}</b> (+${fmtNum(maxDelta)} m dibanding periode sebelumnya).`
      );
    }
  }

  return lines;
}

async function refreshInsights() {
  const panel = panels[0];
  const list = document.getElementById("insightsList");
  if (!panel) {
    list.innerHTML = `<li class="insights-empty">Belum ada peta untuk dianalisis.</li>`;
    return;
  }

  const bloks = panel.bloks || [];
  let growthData = [];
  let areaData = [];
  try {
    [growthData, areaData] = await Promise.all([
      fetchJSON(`/api/growth?${buildQuery({ bloks: bloks.join(",") })}`),
      fetchJSON(`/api/area?${buildQuery({ periode: panel.periode, bloks: bloks.join(",") })}`),
    ]);
  } catch (e) {
    console.error("Gagal load data keterangan", e);
    list.innerHTML = `<li class="insights-empty">Gagal memuat analisis.</li>`;
    return;
  }

  const lines = buildInsightLines(growthData, areaData, panel.periode);
  list.innerHTML = lines.length
    ? lines.map((l) => `<li>${l}</li>`).join("")
    : `<li class="insights-empty">Data belum cukup untuk dianalisis.</li>`;
}

// ---------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------
async function main() {
  META = await fetchJSON("/api/meta");
  AREA_HIERARCHY = await fetchJSON("/api/areas");

  await initPanels();
  wireSettingsPanel();

  const analyticsSidebar = document.getElementById("analytics-sidebar");
  if (analyticsSidebar) {
    analyticsSidebar.classList.remove("hidden");
  }
  await Promise.all([refreshPieChart(), refreshGrowthChart(), refreshInsights()]);
}

main().catch((err) => {
  console.error(err);
  document.body.insertAdjacentHTML(
    "beforeend",
    `<div style="position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#0b1220;color:#e53935;font-family:sans-serif;">Gagal memuat dashboard: ${err.message}</div>`
  );
});
