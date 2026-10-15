/**
 * SensorsController: red de sensores libres validada contra el consenso multimodelo.
 *
 * - Lista los sensores de la zona visible (METAR, openSenseMap y propios).
 * - Colorea cada uno según el veredicto de control de calidad.
 * - Muestra la comparación con cada modelo, la predicción corregida 0–12 h
 *   y, a pedido, la interpretación del agente.
 */
const STATUS = {
  consistente: { cls: "st-ok", label: "Consistente" },
  fenomeno_local: { cls: "st-local", label: "Fenómeno local" },
  desviacion: { cls: "st-dev", label: "Desviación" },
  dudoso: { cls: "st-doubt", label: "Dudoso" },
  falla_probable: { cls: "st-fail", label: "Falla probable" },
  sin_referencia: { cls: "st-none", label: "Sin referencia" },
};
const VAR_LABEL = {
  temp_c: ["Temperatura", "°C"], dewpoint_c: ["Rocío", "°C"], rh_pct: ["Humedad", "%"],
  pressure_hpa: ["Presión QNH", "hPa"], station_pressure: ["Presión est.", "hPa"],
  wind_kt: ["Viento", "kt"], wind_dir: ["Dirección", "°"], gust_kt: ["Ráfaga", "kt"], precip_mm_h: ["Lluvia", "mm/h"],
};
const NETWORK = { metar: "METAR", opensensemap: "openSenseMap", custom: "Propio" };

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

export class SensorsController {
  constructor({ mapCtrl, els }) {
    this.mapCtrl = mapCtrl;
    this.els = els;
    this.sensors = [];
    els.scanBtn.addEventListener("click", () => this.load());
    els.demoBtn.addEventListener("click", () => this.demoFault());
    els.back.addEventListener("click", () => this.closeDetail());
  }

  bbox() {
    const b = this.mapCtrl.map.getBounds();
    return {
      south: Math.max(-89, b.getSouth()), north: Math.min(89, b.getNorth()),
      west: Math.max(-180, b.getWest()), east: Math.min(180, b.getEast()),
    };
  }

  async load() {
    const { scanBtn, meta } = this.els;
    scanBtn.disabled = true;
    scanBtn.textContent = "Validando…";
    meta.textContent = "Consultando sensores y 8 modelos…";
    try {
      const res = await fetch(`/api/sensors?${new URLSearchParams(this.bbox())}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      this.sensors = data.sensors;
      const gaps = data.data_gaps.length ? ` · ${data.data_gaps.join("; ")}` : "";
      meta.textContent = `${data.sensors.length} sensores validados${gaps}`;
      this.renderList();
    } catch (err) {
      meta.textContent = `Error: ${err.message}`;
    } finally {
      scanBtn.disabled = false;
      scanBtn.textContent = "Validar sensores de la zona";
    }
  }

  renderList() {
    const list = this.els.list;
    list.replaceChildren();
    if (!this.sensors.length) {
      list.appendChild(el("div", "alert-empty", "No hay sensores con datos frescos en esta zona. Aleja el mapa o muévelo."));
    }
    for (const s of this.sensors) {
      const st = STATUS[s.status] || STATUS.sin_referencia;
      const row = el("button", "sensor-row");
      row.append(el("span", `st-dot ${st.cls}`), el("span", "sensor-name", s.sensor.name),
        el("span", "sensor-net", `${NETWORK[s.sensor.network] || s.sensor.network} · ${Math.round(s.confidence * 100)} %`));
      row.title = st.label;
      row.onclick = () => this.openDetail(s.sensor.id);
      list.appendChild(row);
    }
    this.mapCtrl.setSensors(this.sensors.map((s) => ({ ...s.sensor, cls: (STATUS[s.status] || STATUS.sin_referencia).cls,
      label: `${s.sensor.name} · ${(STATUS[s.status] || STATUS.sin_referencia).label}` })), (id) => this.openDetail(id));
  }

  closeDetail() {
    this.els.detail.hidden = true;
    this.els.listWrap.hidden = false;
  }

  async openDetail(id) {
    const { detail, listWrap } = this.els;
    listWrap.hidden = true;
    detail.hidden = false;
    const body = this.els.detailBody;
    body.replaceChildren(el("div", "hint", "Cargando validación…"));
    try {
      const res = await fetch(`/api/sensors/${encodeURIComponent(id)}/report`);
      const rep = await res.json();
      if (!res.ok) throw new Error(rep.detail || `HTTP ${res.status}`);
      this.renderDetail(rep);
    } catch (err) {
      body.replaceChildren(el("div", "hint", `Error: ${err.message}`));
    }
  }

  renderDetail(rep) {
    const body = this.els.detailBody;
    body.replaceChildren();
    const st = STATUS[rep.status] || STATUS.sin_referencia;
    const head = el("div", "sensor-head");
    head.append(el("div", "sensor-title", rep.sensor.name),
      el("div", `sensor-badge ${st.cls}`, `${st.label} · confianza ${Math.round(rep.confidence * 100)} %`));
    const sub = el("div", "hint",
      `${NETWORK[rep.sensor.network]} · ${new Date(rep.observed_at).toLocaleString("es-CL", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" })} · modelos: ${rep.consensus_sources.join(", ") || "ninguno"}`);
    body.append(head, sub);

    // Tabla: observación vs consenso
    const table = el("table", "qc-table");
    const thead = el("tr");
    ["Variable", "Sensor", "Modelos", "z", "Veredicto"].forEach((h) => thead.appendChild(el("th", "", h)));
    table.appendChild(thead);
    for (const c of rep.checks) {
      const [name, unit] = VAR_LABEL[c.variable] || [c.variable, ""];
      const tr = el("tr");
      const range = c.consensus_min != null ? ` (${c.consensus_min}–${c.consensus_max})` : "";
      tr.append(el("td", "", name), el("td", "", `${c.observed} ${unit}`),
        el("td", "", c.consensus_median != null ? `${c.consensus_median}${range}` : "—"),
        el("td", "", c.z != null ? c.z.toFixed(1) : "—"));
      const v = el("td");
      v.appendChild(el("span", `st-dot ${(STATUS[c.verdict] || STATUS.sin_referencia).cls}`));
      v.title = c.reason;
      tr.appendChild(v);
      table.appendChild(tr);
    }
    body.appendChild(table);

    // Predicción corregida (temperatura o viento)
    const pv = rep.prediction.temp_c ? "temp_c" : rep.prediction.wind_kt ? "wind_kt" : null;
    if (pv) {
      const [name, unit] = VAR_LABEL[pv];
      const p = rep.prediction[pv];
      body.appendChild(el("div", "section-title", `Predicción ${name.toLowerCase()} 0–12 h`));
      body.appendChild(this.chart(p.series, unit));
      body.appendChild(el("div", "hint", p.trusted_observation
        ? `Mediana de ${p.series[0]?.models ?? 0} modelos + sesgo observado ${p.bias_applied > 0 ? "+" : ""}${p.bias_applied} ${unit} que se disipa (τ = ${p.tau_h} h). Banda: rango entre modelos.`
        : "Sin corrección: la lectura del sensor no es confiable o no hay observación."));
    }

    // Interpretación del agente
    const box = el("div", "interp-box");
    const btn = el("button", "btn primary", "Interpretación del agente");
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "Interpretando…";
      try {
        const res = await fetch(`/api/sensors/${encodeURIComponent(rep.sensor.id)}/interpret`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        const pre = el("pre", "alert-msg", data.interpretation);
        const who = el("div", "alert-meta", data.interpreter === "claude" ? "Redactado por Claude" : "Motor de reglas (sin LLM)");
        box.replaceChildren(pre, who);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = `Reintentar (${err.message})`;
      }
    };
    box.appendChild(btn);
    const view = el("button", "btn small", "Ver en mapa");
    view.onclick = () => this.mapCtrl.applyCommand({ layer_type: this.mapCtrl.activeLayer, focus: { lat: rep.sensor.lat, lon: rep.sensor.lon }, zoom_level: 10, annotation: rep.sensor.name });
    body.append(box, view);
  }

  /** Gráfico SVG mínimo: banda de incertidumbre + línea de predicción. */
  chart(series, unit) {
    const W = 320, H = 120, P = { l: 30, r: 8, t: 8, b: 18 };
    const xs = series.map((r) => r.h), lo = Math.min(...series.map((r) => r.low)), hi = Math.max(...series.map((r) => r.high));
    const x = (h) => P.l + ((h - Math.min(...xs)) / Math.max(1, Math.max(...xs) - Math.min(...xs))) * (W - P.l - P.r);
    const y = (v) => P.t + (1 - (v - lo) / Math.max(0.1, hi - lo)) * (H - P.t - P.b);
    const band = series.map((r) => `${x(r.h)},${y(r.high)}`).concat([...series].reverse().map((r) => `${x(r.h)},${y(r.low)}`)).join(" ");
    const line = series.map((r) => `${x(r.h)},${y(r.value)}`).join(" ");
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "pred-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Predicción: ${series.map((r) => `+${r.h}h ${r.value} ${unit}`).join(", ")}`);
    const mk = (tag, attrs, text) => {
      const e = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
      if (text !== undefined) e.textContent = text;
      svg.appendChild(e);
      return e;
    };
    mk("polygon", { points: band, class: "pred-band" });
    mk("polyline", { points: line, class: "pred-line" });
    mk("text", { x: 2, y: y(hi) + 4, class: "pred-axis" }, `${hi.toFixed(0)}`);
    mk("text", { x: 2, y: y(lo), class: "pred-axis" }, `${lo.toFixed(0)}`);
    [0, 3, 6, 9, 12].forEach((h) => { if (xs.includes(h)) mk("text", { x: x(h) - 6, y: H - 4, class: "pred-axis" }, `+${h}h`); });
    return svg;
  }

  /** Demo: registra un sensor propio con lecturas erróneas y muestra cómo el agente lo detecta. */
  async demoFault() {
    const c = this.mapCtrl.map.getCenter();
    const btn = this.els.demoBtn;
    btn.disabled = true;
    try {
      await fetch("/api/sensors", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: "demo-falla", name: "Sensor de prueba (con falla)", lat: c.lat, lon: c.lng, poi_id: "stgo-centro" }),
      });
      // Lectura absurda: +12 °C sobre el consenso y ráfaga de 55 kt.
      const net = await fetch(`/api/sensors?${new URLSearchParams(this.bbox())}`).then((r) => r.json()).catch(() => ({ sensors: [] }));
      const ref = net.sensors.find((s) => s.latest?.temp_c != null);
      const base = ref ? ref.latest.temp_c : 15;
      await fetch("/api/sensors/custom:demo-falla/readings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ temp_c: base + 12, gust_kt: 55, wind_kt: 30 }),
      });
      await this.load();
      await this.openDetail("custom:demo-falla");
    } finally {
      btn.disabled = false;
    }
  }
}
