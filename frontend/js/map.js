/**
 * MapController: encapsula MapLibre GL y las capas meteorológicas.
 *
 * Capas soportadas (mismos nombres que el enum de trigger_map_layer_update):
 *   radar          -> RainViewer animado (frames vía /api/radar/frames)
 *   wind_particles -> partículas sobre canvas (malla vía /api/wind-grid)
 *   satellite_ir   -> NASA GIBS GOES-East ABI banda 13 (IR onda larga)
 *   none           -> solo mapa base
 */
import { WindParticles } from "./wind.js";

// Mapa base VECTORIAL sin API key (OpenFreeMap, datos OpenMapTiles/OSM).
// Las capas meteorológicas se insertan debajo de la primera capa de símbolos
// (etiquetas) para que los nombres de ciudades queden siempre legibles.
const BASE_STYLE_URL = "https://tiles.openfreemap.org/styles/dark";

const GIBS_IR =
  "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/GOES-East_ABI_Band13_Clean_Infrared/default/default/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png";

const LEGENDS = {
  radar: {
    title: "Reflectividad (dBZ) · RainViewer",
    gradient: "linear-gradient(90deg,#88ddee,#0099cc,#0077aa,#ffee00,#ffaa00,#ff4400,#c10000,#ff00ff)",
    ticks: ["10", "25", "35", "45", "55", "65+"],
  },
  wind_particles: {
    title: "Viento 10 m (m/s) · Open-Meteo",
    gradient: "linear-gradient(90deg,#62b6ef,#39c7a0,#f0d84c,#f59634,#e54848,#be3cc8)",
    ticks: ["0", "5", "10", "15", "20", "28+"],
  },
  satellite_ir: {
    title: "IR 10.3 µm (GOES-East B13) · NASA GIBS",
    gradient: "linear-gradient(90deg,#111,#666,#ddd,#3ad,#2a2,#ee2,#e22,#f0f)",
    ticks: ["cálido", "", "", "", "", "topes fríos"],
  },
};

export class MapController {
  constructor({ container, windCanvas, legendEl, statusEl, onLayerChange, onPointSelected }) {
    this.legendEl = legendEl;
    this.statusEl = statusEl;
    this.onLayerChange = onLayerChange || (() => {});
    this.onPointSelected = onPointSelected || (() => {});
    this.activeLayer = "none";
    this.selectedPoint = null;
    this.radar = { frames: [], idx: 0, timer: null };
    this.focusMarker = null;
    this.selectMarker = null;

    this.map = new maplibregl.Map({
      container,
      style: BASE_STYLE_URL,
      center: [-70.65, -33.45],
      zoom: 4.2,
      attributionControl: { compact: true },
      maxPitch: 0,
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    this.map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    this.labelLayerId = undefined;
    this.ready = new Promise((res) => this.map.on("load", () => {
      this.labelLayerId = this.map.getStyle().layers.find((l) => l.type === "symbol")?.id;
      res();
    }));

    this.wind = new WindParticles(this.map, windCanvas, { onStatus: (t) => this._status(t) });

    // Clic en el mapa => punto de análisis para AeroBot.
    this.map.on("click", (e) => this.selectPoint(e.lngLat.lat, e.lngLat.lng));
  }

  /** Estado que se envía al backend en cada mensaje (contexto espacial del agente). */
  getState() {
    const c = this.map.getCenter();
    return {
      lat: +c.lat.toFixed(4),
      lon: +c.lng.toFixed(4),
      zoom: +this.map.getZoom().toFixed(2),
      active_layer: this.activeLayer,
      selected_point: this.selectedPoint,
    };
  }

  selectPoint(lat, lon) {
    this.selectedPoint = { lat: +lat.toFixed(4), lon: +lon.toFixed(4) };
    if (!this.selectMarker) {
      const el = document.createElement("div");
      el.className = "select-marker";
      this.selectMarker = new maplibregl.Marker({ element: el });
    }
    this.selectMarker.setLngLat([lon, lat]).addTo(this.map);
    this.onPointSelected(this.selectedPoint);
  }

  /** Dibuja los Lugares de Interés coloreados por nivel de riesgo. */
  setPOIs(pois) {
    (this.poiMarkers || []).forEach((m) => m.remove());
    const cls = { 3: "lv-red", 2: "lv-orange", 1: "lv-yellow", 0: "lv-green" };
    this.poiMarkers = pois.map((p) => {
      const el = document.createElement("div");
      el.className = `poi-marker ${cls[p.level] || "lv-green"}`;
      el.title = p.name;
      const popup = new maplibregl.Popup({ offset: 10, closeButton: false }).setText(p.name);
      return new maplibregl.Marker({ element: el }).setLngLat([p.lon, p.lat]).setPopup(popup).addTo(this.map);
    });
  }

  /** Dibuja los sensores coloreados por veredicto de control de calidad. */
  setSensors(sensors, onClick) {
    (this.sensorMarkers || []).forEach((m) => m.remove());
    this.sensorMarkers = sensors.map((s) => {
      const el = document.createElement("button");
      el.className = `sensor-marker ${s.cls}`;
      el.title = s.label;
      el.setAttribute("aria-label", s.label);
      el.addEventListener("click", (e) => { e.stopPropagation(); onClick?.(s.id); });
      return new maplibregl.Marker({ element: el }).setLngLat([s.lon, s.lat]).addTo(this.map);
    });
  }

  clearSelectedPoint() {
    this.selectedPoint = null;
    this.selectMarker?.remove();
    this.onPointSelected(null);
  }

  /** Aplica un comando emitido por la herramienta trigger_map_layer_update. */
  async applyCommand(cmd) {
    await this.ready;
    const { lat, lon } = cmd.focus;
    this.map.flyTo({ center: [lon, lat], zoom: cmd.zoom_level, speed: 1.4, curve: 1.4, essential: true });
    this._setFocusMarker(lat, lon, cmd.annotation);
    // Esperar al fin del vuelo evita recargar la malla de viento dos veces.
    await new Promise((res) => {
      if (!this.map.isMoving()) return res();
      this.map.once("moveend", res);
      setTimeout(res, 4000);
    });
    await this.setLayer(cmd.layer_type);
  }

  async setLayer(type) {
    await this.ready;
    if (type === this.activeLayer) return;
    this._teardown();
    this.activeLayer = type;
    try {
      if (type === "radar") await this._showRadar();
      else if (type === "satellite_ir") this._showSatellite();
      else if (type === "wind_particles") await this.wind.start();
    } catch (err) {
      console.error(`Capa ${type}`, err);
      this._status(`Error cargando la capa ${type}`);
    }
    this._renderLegend(type);
    this.onLayerChange(type);
  }

  // ------------------------------------------------------------------ capas
  async _showRadar() {
    this._status("Cargando radar…");
    const res = await fetch("/api/radar/frames");
    if (!res.ok) throw new Error("radar frames");
    const meta = await res.json();
    const frames = meta.frames.slice(-6);
    if (!frames.length) throw new Error("sin frames de radar");
    this.radar.frames = frames;
    const last = frames.length - 1;

    const addFrame = (i) => {
      const id = `radar-${i}`;
      this.map.addSource(id, {
        type: "raster", tiles: [frames[i].tile_url], tileSize: 256, maxzoom: meta.max_zoom || 7,
        attribution: '<a href="https://www.rainviewer.com/">RainViewer</a>',
      });
      this.map.addLayer({
        id, type: "raster", source: id,
        paint: { "raster-opacity": i === last ? 0.75 : 0, "raster-opacity-transition": { duration: 0 }, "raster-fade-duration": 0 },
      }, this.labelLayerId);
    };

    // 1) El frame más reciente se muestra de inmediato (es el que importa al análisis).
    addFrame(last);
    this._showRadarFrame(last);
    // 2) Los frames históricos se cargan después, para no competir por ancho de banda
    //    (RainViewer limita la tasa de peticiones en su API pública).
    setTimeout(() => {
      if (this.activeLayer !== "radar" || this.radar.frames !== frames) return;
      for (let i = 0; i < last; i++) addFrame(i);
    }, 1200);

    // Animación: solo avanza por frames cuyas teselas ya cargaron; pausa en el más reciente.
    this.radar.idx = last;
    const tick = () => {
      let next = (this.radar.idx + 1) % frames.length;
      for (let guard = 0; guard < frames.length; guard++) {
        const id = `radar-${next}`;
        if (next === last || (this.map.getSource(id) && this.map.isSourceLoaded(id))) break;
        next = (next + 1) % frames.length;
      }
      this._showRadarFrame(next);
      this.radar.timer = setTimeout(tick, next === last ? 2000 : 600);
    };
    this.radar.timer = setTimeout(tick, 2500);
  }

  _showRadarFrame(i) {
    const { frames } = this.radar;
    frames.forEach((_, j) => {
      if (this.map.getLayer(`radar-${j}`)) this.map.setPaintProperty(`radar-${j}`, "raster-opacity", j === i ? 0.75 : 0);
    });
    this.radar.idx = i;
    const t = new Date(frames[i].time * 1000);
    const isLast = i === frames.length - 1;
    this._status(`Radar ${t.toISOString().slice(11, 16)} UTC ${isLast ? "· último" : ""}`);
  }

  _showSatellite() {
    this.map.addSource("sat-ir", {
      type: "raster", tiles: [GIBS_IR], tileSize: 256, maxzoom: 6,
      attribution: '<a href="https://earthdata.nasa.gov/gibs">NASA GIBS</a>',
    });
    this.map.addLayer({ id: "sat-ir", type: "raster", source: "sat-ir", paint: { "raster-opacity": 0.8 } }, this.labelLayerId);
    this._status("Satélite IR · GOES-East B13 · imagen más reciente");
  }

  _teardown() {
    clearTimeout(this.radar.timer);
    this.radar.frames.forEach((_, i) => {
      if (this.map.getLayer(`radar-${i}`)) this.map.removeLayer(`radar-${i}`);
      if (this.map.getSource(`radar-${i}`)) this.map.removeSource(`radar-${i}`);
    });
    this.radar.frames = [];
    if (this.map.getLayer("sat-ir")) this.map.removeLayer("sat-ir");
    if (this.map.getSource("sat-ir")) this.map.removeSource("sat-ir");
    this.wind.stop();
    this._status(null);
  }

  // ----------------------------------------------------------------- UI aux
  _setFocusMarker(lat, lon, annotation) {
    this.focusMarker?.remove();
    const el = document.createElement("div");
    el.className = "focus-marker";
    this.focusMarker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(this.map);
    if (annotation) {
      const popup = new maplibregl.Popup({ offset: 14, closeButton: false }).setText(annotation);
      this.focusMarker.setPopup(popup).togglePopup();
    }
  }

  _status(text) {
    if (!this.statusEl) return;
    this.statusEl.hidden = !text;
    this.statusEl.textContent = text || "";
  }

  _renderLegend(type) {
    const lg = LEGENDS[type];
    if (!lg) { this.legendEl.hidden = true; return; }
    this.legendEl.hidden = false;
    this.legendEl.innerHTML = `
      <div>${lg.title}</div>
      <div class="bar" style="background:${lg.gradient}"></div>
      <div class="ticks">${lg.ticks.map((t) => `<span>${t}</span>`).join("")}</div>`;
  }
}
