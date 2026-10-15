/**
 * OpsController: módulos AeroBot Enterprise en la pestaña "Ops".
 *  1. Go / No-Go UAV        2. Incendio y aluvión
 *  3. Auditor climático     4. Mitigación (webhooks con límites)
 */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const fmtLocal = (iso) => new Date(iso).toLocaleString("es-CL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${res.status}`);
  return data;
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

export class OpsController {
  constructor({ mapCtrl, root }) {
    this.mapCtrl = mapCtrl;
    this.root = root;
    $("#uav-run", root).addEventListener("click", () => this.uav());
    $("#fire-run", root).addEventListener("click", () => this.fire());
    $("#flood-run", root).addEventListener("click", () => this.flood());
    $("#audit-run", root).addEventListener("click", () => this.audit());
    $("#mit-scan", root).addEventListener("click", () => this.mitigation("scan"));
    $("#mit-sim", root).addEventListener("click", () => this.mitigation("simulate"));
    const d = new Date(Date.now() - 86400000);
    $("#audit-date", root).value = d.toISOString().slice(0, 10);
  }

  point() {
    const s = this.mapCtrl.getState();
    return s.selected_point || { lat: s.lat, lon: s.lon };
  }

  async run(btn, out, fn) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Calculando…";
    out.replaceChildren(el("div", "hint", "Consultando datos y calculando…"));
    try {
      await fn();
    } catch (err) {
      out.replaceChildren(el("div", "ops-error", `Error: ${err.message}`));
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ------------------------------------------------------------ Módulo 1
  uav() {
    const out = $("#uav-out", this.root);
    return this.run($("#uav-run", this.root), out, async () => {
      const p = this.point();
      const q = new URLSearchParams({ lat: p.lat, lon: p.lon, max_wind_knots: $("#uav-wind", this.root).value,
        max_agl_meters: $("#uav-agl", this.root).value, hours: 24 });
      const r = await api(`/api/ops/flight-envelope?${q}`);
      const banner = el("div", `verdict ${r.go ? "go" : "nogo"}`, r.verdict);
      const lines = [r.summary.replace(r.verdict, "").trim()];
      if (r.next_hour_below_threshold) lines.push(`Próxima hora < ${r.criteria.max_wind_kt} kt: ${r.next_hour_below_threshold.local}`);
      if (r.next_window) lines.push(`Próxima ventana apta: ${r.next_window.start_local} → ${r.next_window.end_local}`);
      if (r.observation) lines.push(`Observación: ${r.observation.raw || r.observation.station}`);
      lines.push(...r.notes);
      const strip = el("div", "hour-strip");
      r.hourly.forEach((h) => {
        const c = el("span", h.go ? "ok" : "no");
        c.title = `${fmtLocal(h.time)} · ${h.critical_wind_kt?.toFixed?.(0) ?? "?"} kt · β ${h.beta_max ?? "?"}${h.reasons.length ? " · " + h.reasons.join("; ") : ""}`;
        strip.appendChild(c);
      });
      out.replaceChildren(banner, el("pre", "ops-pre", lines.join("\n")), el("div", "hint", "Próximas 24 h (verde = apta):"), strip);
      this.mapCtrl.applyCommand({ layer_type: "wind_particles", focus: p, zoom_level: 10, annotation: r.verdict });
    });
  }

  // ------------------------------------------------------------ Módulo 2
  fire() {
    const out = $("#fire-out", this.root);
    return this.run($("#fire-run", this.root), out, async () => {
      const p = this.point();
      const num = (id) => ($(id, this.root).value === "" ? null : Number($(id, this.root).value));
      const r = await post("/api/ops/fire", { lat: p.lat, lon: p.lon, fuel: $("#fire-fuel", this.root).value,
        humidity: num("#fire-rh"), wind_dir: num("#fire-wd"), wind_speed: num("#fire-ws") });
      const n = r.now;
      const txt = [
        `Avance de cabeza: ${n.ros_head_m_min} m/min (${(n.ros_head_m_min * 0.06).toFixed(2)} km/h) hacia ${n.heading_deg}°`,
        `Retaguardia: ${n.ros_back_m_min} m/min · razón largo/ancho ${n.length_to_breadth}`,
        `φ_W = ${n.phi_w} · φ_S = ${n.phi_s} · pendiente ${r.terrain.slope_pct} % (cuesta arriba ${r.terrain.upslope_bearing}°)`,
        `T ${n.temp_c} °C · HR ${n.rh_pct} % · VPD ${n.vpd_kpa ?? "s/d"} kPa · humedad combustible ${n.fuel_moisture_pct} %`,
        `Regla 30-30-30: ${r.rule_30_30_30_now ? "CUMPLIDA" : "no se cumple"} · peor hora 24 h: ${r.worst_24h.ros_head_m_min} m/min (${fmtLocal(r.worst_24h.time)})`,
        ...(r.inputs_overridden.length ? [`Datos ingresados: ${r.inputs_overridden.join(", ")}`] : []),
        ...r.notes,
      ];
      out.replaceChildren(el("pre", "ops-pre", txt.join("\n")));
      this.mapCtrl.applyCommand({ layer_type: "wind_particles", focus: p, zoom_level: r.suggested_zoom || 11, overlay: r.cone });
    });
  }

  flood() {
    const out = $("#flood-out", this.root);
    return this.run($("#flood-run", this.root), out, async () => {
      const p = this.point();
      const v = (id) => ($(id, this.root).value === "" ? null : Number($(id, this.root).value));
      const r = await post("/api/ops/runoff", { lat: p.lat, lon: p.lon, catchment_area_km2: v("#flood-area"),
        catchment_top_m: v("#flood-top"), runoff_coeff: v("#flood-c") ?? 0.5, channel_capacity_m3s: v("#flood-cap") });
      const pk = r.peak;
      const txt = [
        r.alert || "Sin escorrentía crítica en las próximas 48 h.",
        `Faena ${r.site_elevation_m?.toFixed?.(0)} m · isoterma 0 °C en el pico ${pk.freezing_level_m} m · lluvia ${pk.liquid_on_site ? "LÍQUIDA" : "sólida"} sobre la faena`,
        `Q = 0,278·C·i·A_eff = 0,278·${pk.c_eff}·${pk.intensity_mm_h}·${r.catchment.area_km2}·${pk.contributing_fraction} = ${pk.q_m3s} m³/s (${fmtLocal(pk.time)})`,
        `Precipitación total 48 h: ${r.total_precip_mm} mm`, ...r.notes,
      ];
      out.replaceChildren(el("pre", `ops-pre ${r.critical ? "crit" : ""}`, txt.join("\n")));
    });
  }

  // ------------------------------------------------------------ Módulo 3
  audit() {
    const out = $("#audit-out", this.root);
    return this.run($("#audit-run", this.root), out, async () => {
      const p = this.point();
      const val = (id) => $(id, this.root).value.trim();
      const r = await post("/api/audit", {
        lat: p.lat, lon: p.lon, date: val("#audit-date"), threshold_knots: Number(val("#audit-kt") || 38),
        start_hour: Number(val("#audit-h0") || 0), end_hour: Number(val("#audit-h1") || 24),
        station: val("#audit-icao") || null, site_name: val("#audit-site") || null, contract_ref: val("#audit-ref") || null,
      });
      const banner = el("div", `verdict ${r.exceeded ? "nogo" : "go"}`, r.exceeded ? "LÍMITE SUPERADO" : "LÍMITE NO SUPERADO");
      const links = el("div", "alert-actions");
      const pdf = el("a", "btn small", "Abrir PDF");
      pdf.href = r.pdf_url; pdf.target = "_blank"; pdf.rel = "noopener";
      const raw = el("a", "btn small", "Datos crudos (JSON)");
      raw.href = r.raw_url; raw.target = "_blank"; raw.rel = "noopener";
      links.append(pdf, raw);
      out.replaceChildren(banner, el("pre", "ops-pre", r.report_text), links);
    });
  }

  // ------------------------------------------------------------ Módulo 4
  async mitigation(kind) {
    const out = $("#mit-out", this.root);
    const btn = kind === "scan" ? $("#mit-scan", this.root) : $("#mit-sim", this.root);
    return this.run(btn, out, async () => {
      const r = await post(kind === "scan" ? "/api/mitigation/scan" : "/api/mitigation/simulate");
      const results = Array.isArray(r) ? r : [r];
      const msgs = results.filter((x) => x.message).map((x) => x.message);
      const errs = results.filter((x) => x.error).map((x) => `${x.poi_id}: ${x.error}`);
      out.replaceChildren(el("pre", "ops-pre", msgs.join("\n\n") || "Sin eventos de impacto en las próximas 72 h."));
      if (errs.length) out.appendChild(el("div", "hint", `Sin datos: ${errs.join("; ")}`));
      await this.renderMitigationState();
    });
  }

  async renderMitigationState() {
    const box = $("#mit-state", this.root);
    const st = await api("/api/mitigation");
    box.replaceChildren();
    box.appendChild(el("div", "section-title", `Pendientes de aprobación (${st.pending.length})`));
    if (!st.pending.length) box.appendChild(el("div", "hint", "Nada pendiente."));
    for (const a of st.pending) {
      const row = el("div", "mit-row");
      const amount = a.amount_clp ? ` · $${a.amount_clp.toLocaleString("es-CL")}` : "";
      row.append(el("div", "", `${a.action}: ${a.item}${amount}${a.simulated ? " · SIMULACRO" : ""}`),
        el("div", "hint", a.blockers.join("; ")));
      const actions = el("div", "alert-actions");
      const ok = el("button", "btn small primary", "Aprobar");
      const no = el("button", "btn small", "Rechazar");
      const act = async (verb) => {
        const headers = {};
        let token = null;
        try { token = localStorage.getItem("aerobot_admin_token"); } catch { /* sin storage */ }
        if (token) headers["X-Admin-Token"] = token;
        let res = await fetch(`/api/mitigation/actions/${a.id}/${verb}`, { method: "POST", headers });
        if (res.status === 401) {
          token = window.prompt("Token de administrador (AEROBOT_ADMIN_TOKEN):") || "";
          try { localStorage.setItem("aerobot_admin_token", token); } catch { /* sin storage */ }
          res = await fetch(`/api/mitigation/actions/${a.id}/${verb}`, { method: "POST", headers: { "X-Admin-Token": token } });
        }
        await this.renderMitigationState();
      };
      ok.onclick = () => act("approve");
      no.onclick = () => act("reject");
      actions.append(ok, no);
      row.appendChild(actions);
      box.appendChild(row);
    }
    box.appendChild(el("div", "section-title", "Bitácora"));
    if (!st.log.length) box.appendChild(el("div", "hint", "Sin ejecuciones."));
    for (const a of st.log.slice(0, 10)) {
      box.appendChild(el("div", "mit-log", `${a.status.toUpperCase()} · ${a.action}: ${a.item} · ${a.approved_by || ""} · ${a.response?.detail || ""}`));
    }
    box.appendChild(el("div", "hint", `Receptor de prueba: ${st.receiver_log.length} webhooks recibidos · clientes: ${st.clients.map((c) => c.name).join(", ")}`));
  }
}
