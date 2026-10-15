/**
 * Punto de entrada: conecta mapa <-> chat <-> AeroBot.
 */
import { MapController } from "./map.js";
import { ChatClient, renderMarkdown } from "./chat.js";
import { AlertsController } from "./alerts.js";
import { SensorsController } from "./sensors.js";
import { OpsController } from "./ops.js";

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const input = $("#chat-input");
const sendBtn = $("#send-btn");
const selectedEl = $("#selected-point");

const TOOL_LABELS = {
  fetch_aviation_weather: "METAR/TAF",
  analyze_radar_echoes: "Radar dBZ",
  compare_atmospheric_models: "GFS vs ECMWF",
  trigger_map_layer_update: "Control de mapa",
  dispatch_risk_alert: "Despachador de riesgos",
  inspect_sensor: "Validación de sensor",
  calculate_flight_envelope: "Go/No-Go UAV",
  run_fire_spread_vector: "Propagación de incendio",
  calculate_runoff_isotherm: "Escorrentía e isoterma",
  audit_historical_weather: "Auditor climático",
  evaluate_supply_chain_risk: "Mitigación 72 h",
  trigger_supply_chain_webhook: "Webhook logístico",
};

// ----------------------------------------------------------------- Mapa
const mapCtrl = new MapController({
  container: "map",
  windCanvas: $("#wind-canvas"),
  legendEl: $("#legend"),
  statusEl: $("#layer-status"),
  onLayerChange: (type) => {
    document.querySelectorAll(".layer-bar button").forEach((b) => b.classList.toggle("active", b.dataset.layer === type));
  },
  onPointSelected: (pt) => {
    alerts?.setSelectedPoint(pt);
    selectedEl.hidden = !pt;
    if (pt) {
      selectedEl.innerHTML = `<span>Punto de análisis: ${pt.lat.toFixed(3)}, ${pt.lon.toFixed(3)}</span><button type="button" aria-label="Quitar punto">✕</button>`;
      selectedEl.querySelector("button").onclick = () => mapCtrl.clearSelectedPoint();
    }
  },
});

document.querySelectorAll(".layer-bar button").forEach((btn) =>
  btn.addEventListener("click", () => mapCtrl.setLayer(btn.dataset.layer)));

// ---------------------------------------------------------------- Alertas
const alerts = new AlertsController({
  mapCtrl,
  els: {
    scanBtn: $("#scan-btn"), scenarioSelect: $("#scenario-select"), scanMeta: $("#scan-meta"),
    alertList: $("#alert-list"), poiList: $("#poi-list"), poiForm: $("#poi-form"),
    poiName: $("#poi-name"), poiType: $("#poi-type"), poiHint: $("#poi-hint"), toast: $("#toast"),
  },
  onCountChange: (n) => {
    const b = $("#alert-count");
    b.hidden = n === 0;
    b.textContent = n;
  },
});

const sensors = new SensorsController({
  mapCtrl,
  els: {
    scanBtn: $("#sensor-scan"), demoBtn: $("#sensor-demo"), meta: $("#sensor-meta"), list: $("#sensor-list"),
    listWrap: $("#sensor-list-wrap"), detail: $("#sensor-detail"), detailBody: $("#sensor-detail-body"), back: $("#sensor-back"),
  },
});
let sensorsLoaded = false;
const ops = new OpsController({ mapCtrl, root: $("#ops-view") });
let opsLoaded = false;

function showTab(tab) {
  const panel = $("#chat-panel");
  panel.dataset.tab = tab;
  $("#alerts-view").hidden = tab !== "alerts";
  $("#sensors-view").hidden = tab !== "sensors";
  $("#ops-view").hidden = tab !== "ops";
  if (tab === "sensors" && !sensorsLoaded) { sensorsLoaded = true; sensors.load(); }
  if (tab === "ops" && !opsLoaded) { opsLoaded = true; ops.renderMitigationState().catch(() => {}); }
  document.querySelectorAll(".tabs button").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  });
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#toast").addEventListener("click", () => showTab("alerts"));

// --------------------------------------------------------------- Mensajes
let current = null; // burbuja del bot en curso { el, trace, chips }

function addUserMessage(text) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="bubble"></div>`;
  div.querySelector(".bubble").textContent = text;
  messagesEl.appendChild(div);
  scroll();
}

function startBotMessage() {
  const div = document.createElement("div");
  div.className = "msg bot";
  div.innerHTML = `<div class="bubble"><span class="typing"><span></span><span></span><span></span></span></div><div class="trace"></div>`;
  messagesEl.appendChild(div);
  current = { el: div, bubble: div.querySelector(".bubble"), trace: div.querySelector(".trace"), chips: [] };
  scroll();
}

function upsertChip(name, state, extra = "") {
  if (!current) return;
  let chip = current.chips.find((c) => c.name === name && c.state === "running");
  if (!chip) {
    const el = document.createElement("span");
    el.className = "chip";
    current.trace.appendChild(el);
    chip = { name, el, state };
    current.chips.push(chip);
  }
  chip.state = state;
  chip.el.className = `chip ${state}`;
  chip.el.textContent = `${TOOL_LABELS[name] || name}${extra}`;
}

function showError(text) {
  const span = document.createElement("span");
  span.style.color = "var(--alert)";
  span.textContent = text;
  current.bubble.replaceChildren(span);
}

function scroll() { messagesEl.scrollTop = messagesEl.scrollHeight; }

// ------------------------------------------------------------------- Chat
const chat = new ChatClient({
  getMapState: () => mapCtrl.getState(),
  onStatus: (state, label) => {
    const el = $("#conn-status");
    el.className = `conn ${state}`;
    el.querySelector(".label").textContent = label;
  },
  onEvent: (ev) => {
    switch (ev.type) {
      case "tool_start":
        upsertChip(ev.name, "running", " …");
        break;
      case "tool_end": {
        const tag = ev.is_error ? " ✕" : ev.source === "mock" ? " · simulado" : ` · ${ev.duration_ms} ms`;
        upsertChip(ev.name, ev.is_error ? "error" : ev.source === "mock" ? "mock" : "done", tag);
        break;
      }
      case "map_command":
        // El evento llega mientras el LLM sigue trabajando: el mapa reacciona al instante.
        mapCtrl.applyCommand(ev.command);
        break;
      case "final":
        if (current) current.bubble.innerHTML = renderMarkdown(ev.reply);
        scroll();
        break;
      case "error":
        if (current) showError(ev.detail);
        break;
    }
  },
});

async function send(text) {
  text = text.trim();
  if (!text || sendBtn.disabled) return;
  addUserMessage(text);
  input.value = "";
  sendBtn.disabled = true;
  startBotMessage();
  try {
    await chat.send(text);
  } catch (err) {
    if (current) showError(`Error: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    current = null;
    input.focus();
  }
}

$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
$("#suggestions").addEventListener("click", (e) => {
  if (e.target.tagName === "BUTTON") send(e.target.textContent);
});
$("#toggle-panel").addEventListener("click", () => {
  const panel = $("#chat-panel");
  panel.classList.toggle("collapsed");
  $("#toggle-panel").textContent = panel.classList.contains("collapsed") ? "⌃" : "⌄";
  setTimeout(() => mapCtrl.map.resize(), 50);
});

// Health inicial (por si el WS tarda o está bloqueado por un proxy).
fetch("/api/health").then((r) => r.json()).then((h) => {
  if (!chat.wsReady) {
    const el = $("#conn-status");
    el.className = "conn online";
    el.querySelector(".label").textContent = h.mode === "mock" ? "En línea · modo demo" : "En línea · Claude";
  }
}).catch(() => {});

window.aerobot = { mapCtrl, chat, alerts, sensors, ops }; // útil para depurar desde la consola
