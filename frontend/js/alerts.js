/**
 * AlertsController: panel de alertas del despachador de riesgos.
 *
 * - Consulta /api/alerts cada minuto (el backend escanea los POI cada 15 min).
 * - Pinta tarjetas de 4 líneas [NIVEL][LUGAR][TIEMPO][ACCIÓN] por color.
 * - Notifica alertas nuevas rojas/naranjas con un aviso en pantalla y vibración.
 * - Gestiona Lugares de Interés (listar, agregar desde el mapa, eliminar).
 */
const LEVEL_CLASS = { 3: "lv-red", 2: "lv-orange", 1: "lv-yellow", 0: "lv-green" };
const TYPE_LABEL = {
  aeropuerto: "Aeropuerto", puerto: "Puerto", obra: "Obra", evento: "Evento",
  ruta: "Ruta", agricola: "Agrícola", urbano: "Urbano",
};

export class AlertsController {
  constructor({ mapCtrl, els, onCountChange }) {
    this.mapCtrl = mapCtrl;
    this.els = els;
    this.onCountChange = onCountChange || (() => {});
    this.seen = new Set();
    this.firstLoad = true;
    this.state = { alerts: [], pois: [] };

    els.scanBtn.addEventListener("click", () => this.scan());
    els.scenarioSelect.addEventListener("change", () => this.simulate(els.scenarioSelect.value));
    els.poiForm.addEventListener("submit", (e) => { e.preventDefault(); this.addPOI(); });
    els.toast.addEventListener("click", () => { els.toast.hidden = true; });

    this.loadScenarios();
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 60_000);
  }

  // ------------------------------------------------------------------ datos
  async refresh() {
    try {
      const res = await fetch("/api/alerts");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.apply(await res.json());
    } catch (err) {
      this.els.scanMeta.textContent = `Sin conexión con alertas (${err.message})`;
    }
  }

  async scan() {
    const btn = this.els.scanBtn;
    btn.disabled = true;
    btn.textContent = "Escaneando…";
    try {
      const res = await fetch("/api/alerts/scan", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.apply(await res.json());
    } catch (err) {
      this.els.scanMeta.textContent = `Error al escanear: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Escanear ahora";
    }
  }

  async loadScenarios() {
    try {
      const list = await (await fetch("/api/alerts/scenarios")).json();
      for (const sc of list) {
        const opt = document.createElement("option");
        opt.value = sc.id;
        opt.textContent = sc.title;
        this.els.scenarioSelect.appendChild(opt);
      }
    } catch { /* sin simulacros */ }
  }

  async simulate(id) {
    if (!id) return;
    this.els.scenarioSelect.disabled = true;
    try {
      const res = await fetch(`/api/alerts/simulate/${encodeURIComponent(id)}`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const alert = await res.json();
      await this.refresh();
      this.focus(alert);
    } catch (err) {
      this.els.scanMeta.textContent = `Error en simulacro: ${err.message}`;
    } finally {
      this.els.scenarioSelect.value = "";
      this.els.scenarioSelect.disabled = false;
    }
  }

  apply(data) {
    this.state = data;
    const gen = data.generated_at ? new Date(data.generated_at) : null;
    this.els.scanMeta.textContent = gen
      ? `Último escaneo ${gen.toLocaleTimeString("es-CL", { hour: "2-digit", minute: "2-digit" })} · cada ${data.scan_minutes} min`
      : "";
    this.renderAlerts(data.alerts || []);
    this.renderPOIs(data.pois || [], data.alerts || []);
    const active = (data.alerts || []).filter((a) => a.level >= 2).length;
    this.onCountChange(active);
    this.notifyNew(data.alerts || []);
    this.firstLoad = false;
  }

  // --------------------------------------------------------------- render
  renderAlerts(alerts) {
    const list = this.els.alertList;
    list.replaceChildren();
    if (!alerts.length) {
      const empty = document.createElement("div");
      empty.className = "alert-empty";
      empty.textContent = "Sin alertas activas en los lugares de interés.";
      list.appendChild(empty);
      return;
    }
    for (const a of alerts) list.appendChild(this.card(a));
  }

  card(a) {
    const el = document.createElement("article");
    el.className = `alert-card ${LEVEL_CLASS[a.level]}`;
    const pre = document.createElement("pre");
    pre.className = "alert-msg";
    pre.textContent = a.message; // texto plano: el mensaje es el contrato de 4 líneas
    el.appendChild(pre);

    const meta = document.createElement("div");
    meta.className = "alert-meta";
    const tags = [];
    if (a.simulated) tags.push("SIMULACRO");
    tags.push(a.dispatcher === "claude" ? "redactado por Claude" : "motor de reglas");
    tags.push(a.sources.join(" · "));
    meta.textContent = tags.join(" · ");
    el.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "alert-actions";
    const view = document.createElement("button");
    view.className = "btn small";
    view.textContent = "Ver en mapa";
    view.onclick = () => this.focus(a);
    const copy = document.createElement("button");
    copy.className = "btn small";
    copy.textContent = "Copiar";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(a.message); copy.textContent = "Copiado"; }
      catch { copy.textContent = "No se pudo copiar"; }
      setTimeout(() => { copy.textContent = "Copiar"; }, 1500);
    };
    actions.append(view, copy);
    el.appendChild(actions);
    return el;
  }

  renderPOIs(pois, alerts) {
    const list = this.els.poiList;
    list.replaceChildren();
    const sim = Object.fromEntries(alerts.filter((a) => a.simulated).map((a) => [a.poi.id, a.level]));
    for (const p of pois) {
      const level = Math.max(p.level, sim[p.id] ?? 0);
      const li = document.createElement("li");
      li.className = "poi-item";
      const dot = document.createElement("span");
      dot.className = `poi-dot ${LEVEL_CLASS[level]}`;
      const name = document.createElement("button");
      name.className = "poi-name";
      name.textContent = p.name;
      name.onclick = () => this.mapCtrl.applyCommand({ layer_type: this.mapCtrl.activeLayer, focus: { lat: p.lat, lon: p.lon }, zoom_level: 9 });
      const type = document.createElement("span");
      type.className = "poi-type";
      type.textContent = TYPE_LABEL[p.type] || p.type;
      if (p.data_gaps?.length) type.title = `Datos faltantes: ${p.data_gaps.join("; ")}`;
      li.append(dot, name, type);
      if (p.custom) {
        const del = document.createElement("button");
        del.className = "poi-del";
        del.setAttribute("aria-label", `Eliminar ${p.name}`);
        del.textContent = "✕";
        del.onclick = () => this.deletePOI(p.id);
        li.appendChild(del);
      }
      list.appendChild(li);
    }
    this.mapCtrl.setPOIs(pois.map((p) => ({ ...p, level: Math.max(p.level, sim[p.id] ?? 0) })));
  }

  // ------------------------------------------------------------ acciones
  focus(a) {
    this.mapCtrl.applyCommand({ ...a.map_hint, annotation: a.lines?.[0]?.replace(/^\[|\]$/g, "") });
  }

  notifyNew(alerts) {
    const fresh = alerts.filter((a) => a.level >= 2 && !this.seen.has(a.id));
    alerts.forEach((a) => this.seen.add(a.id));
    if (this.firstLoad || !fresh.length) return;
    const top = fresh[0];
    const t = this.els.toast;
    t.className = `toast ${LEVEL_CLASS[top.level]}`;
    t.textContent = top.message;
    t.hidden = false;
    try { navigator.vibrate?.(top.level === 3 ? [300, 100, 300] : 200); } catch { /* sin vibración */ }
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => { t.hidden = true; }, 15000);
  }

  setSelectedPoint(pt) {
    this.selected = pt;
    this.els.poiForm.hidden = !pt;
    this.els.poiHint.hidden = !!pt;
  }

  async addPOI() {
    if (!this.selected) return;
    const body = {
      name: this.els.poiName.value.trim(),
      type: this.els.poiType.value,
      lat: this.selected.lat,
      lon: this.selected.lon,
    };
    if (body.name.length < 2) return;
    const res = await fetch("/api/pois", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      this.els.scanMeta.textContent = "No se pudo guardar el lugar";
      return;
    }
    this.els.poiName.value = "";
    this.mapCtrl.clearSelectedPoint();
    await this.scan();
  }

  async deletePOI(id) {
    const res = await fetch(`/api/pois/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (res.ok) await this.scan();
  }
}
