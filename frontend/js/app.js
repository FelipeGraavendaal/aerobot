/**
 * Punto de entrada: conecta mapa <-> chat <-> AeroBot.
 */
import { MapController } from "./map.js";
import { ChatClient, renderMarkdown } from "./chat.js";

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
    selectedEl.hidden = !pt;
    if (pt) {
      selectedEl.innerHTML = `<span>Punto de análisis: ${pt.lat.toFixed(3)}, ${pt.lon.toFixed(3)}</span><button type="button" aria-label="Quitar punto">✕</button>`;
      selectedEl.querySelector("button").onclick = () => mapCtrl.clearSelectedPoint();
    }
  },
});

document.querySelectorAll(".layer-bar button").forEach((btn) =>
  btn.addEventListener("click", () => mapCtrl.setLayer(btn.dataset.layer)));

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

window.aerobot = { mapCtrl, chat }; // útil para depurar desde la consola
