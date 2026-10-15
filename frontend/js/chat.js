/**
 * ChatClient: transporte con AeroBot.
 *
 * Usa WebSocket (/ws/chat) para recibir eventos en vivo — el mapa cambia en
 * cuanto el LLM llama a trigger_map_layer_update, antes de la respuesta
 * final. Si el WS no está disponible, cae a REST (/api/chat) y aplica los
 * comandos de mapa que vienen en la respuesta.
 */
export class ChatClient {
  constructor({ getMapState, onEvent, onStatus }) {
    this.getMapState = getMapState;
    this.onEvent = onEvent;       // (event) => void   (tool_start, tool_end, map_command, final, error)
    this.onStatus = onStatus;     // (state, label) => void
    this.sessionId = sessionStorageGet("aerobot_session");
    this.ws = null;
    this.pending = null;          // { resolve, reject }
    this.retryMs = 1000;
    this.connect();
  }

  get wsReady() { return this.ws && this.ws.readyState === WebSocket.OPEN; }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      this.ws = new WebSocket(`${proto}://${location.host}/ws/chat`);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => { this.retryMs = 1000; };
    this.ws.onmessage = (ev) => this._handle(JSON.parse(ev.data));
    this.ws.onclose = () => {
      this.onStatus("offline", "Sin WebSocket · usando REST");
      if (this.pending) { this.pending.reject(new Error("Conexión cerrada")); this.pending = null; }
      this._scheduleReconnect();
    };
    this.ws.onerror = () => {};
  }

  _scheduleReconnect() {
    setTimeout(() => this.connect(), this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, 15000);
  }

  _handle(ev) {
    if (ev.type === "ready") {
      this.onStatus("online", ev.mode === "mock" ? "En línea · modo demo (LLM simulado)" : "En línea · Claude");
      return;
    }
    if (ev.type === "final") {
      this.sessionId = ev.session_id;
      sessionStorageSet("aerobot_session", ev.session_id);
    }
    // Los comandos de mapa ya llegaron como eventos 'map_command' en vivo.
    this.onEvent({ ...ev, live: true });
    if (ev.type === "final" || ev.type === "error") {
      const p = this.pending;
      this.pending = null;
      if (p) ev.type === "final" ? p.resolve(ev) : p.reject(new Error(ev.detail));
    }
  }

  async send(message) {
    const payload = { message, session_id: this.sessionId, map_state: this.getMapState() };
    if (this.wsReady && !this.pending) {
      return new Promise((resolve, reject) => {
        this.pending = { resolve, reject };
        this.ws.send(JSON.stringify(payload));
      });
    }
    return this._sendRest(payload);
  }

  async _sendRest(payload) {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    this.sessionId = data.session_id;
    sessionStorageSet("aerobot_session", data.session_id);
    // En REST no hubo eventos en vivo: se emiten ahora en orden.
    for (const t of data.tool_trace) this.onEvent({ type: "tool_end", ...t });
    for (const c of data.map_commands) this.onEvent({ type: "map_command", command: c });
    const final = { type: "final", ...data, live: false };
    this.onEvent(final);
    return final;
  }
}

// sessionStorage puede lanzar en contextos restringidos (iframes, modo privado).
function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* sin persistencia */ } }

/* ------------------------------------------------------------------------
 * Renderizado mínimo y seguro de Markdown (negritas, código, listas, citas)
 * con resaltado de líneas [ALERTA].
 * ---------------------------------------------------------------------- */
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

export function renderMarkdown(text) {
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } };

  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const alert = line.match(/^\s*(?:[-*]\s*)?\[ALERTA\]\s*(.*)$/);
    if (alert) {
      closeList();
      out.push(`<div class="alert-block"><span class="tag">ALERTA</span><span>${inline(alert[1])}</span></div>`);
    } else if (/^\s*[-*•]\s+/.test(line)) {
      (list ||= []).push(`<li>${inline(line.replace(/^\s*[-*•]\s+/, ""))}</li>`);
    } else if (/^>\s?/.test(line)) {
      closeList();
      out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`);
    } else if (/^#{1,4}\s+/.test(line)) {
      closeList();
      out.push(`<strong>${inline(line.replace(/^#{1,4}\s+/, ""))}</strong><br>`);
    } else if (!line.trim()) {
      closeList();
      out.push("<br>");
    } else {
      closeList();
      out.push(`${inline(line)}<br>`);
    }
  }
  closeList();
  return out.join("").replace(/(<br>)+$/, "");
}
