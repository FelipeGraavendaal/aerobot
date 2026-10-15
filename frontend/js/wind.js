/**
 * Capa de partículas de viento (estilo Windy) sobre un <canvas>.
 *
 * - Pide al backend una malla regular u/v (m/s) del área visible.
 * - Interpola bilinealmente el viento en cada partícula.
 * - Advecta ~1200 partículas en coordenadas de pantalla y deja estelas
 *   desvanecientes (composición "destination-in").
 */
const SPEED_COLORS = [
  [0, [98, 182, 239]], [5, [57, 199, 160]], [10, [240, 216, 76]],
  [15, [245, 150, 52]], [20, [229, 72, 72]], [28, [190, 60, 200]],
];

function speedColor(s) {
  for (let i = SPEED_COLORS.length - 1; i >= 0; i--) {
    if (s >= SPEED_COLORS[i][0]) {
      const [r, g, b] = SPEED_COLORS[i][1];
      return `rgba(${r},${g},${b},0.9)`;
    }
  }
  return "rgba(98,182,239,0.9)";
}

export class WindParticles {
  constructor(map, canvas, { count = 1200, onStatus } = {}) {
    this.map = map;
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.count = count;
    this.onStatus = onStatus || (() => {});
    this.grid = null;
    this.particles = [];
    this.running = false;
    this.paused = false;
    this.raf = null;
    this._onMoveStart = () => { this.paused = true; this._clear(); };
    this._onMoveEnd = () => this._reload();
    this._onResize = () => this._resize();
  }

  async start() {
    if (this.running) return;
    this.running = true;
    this.canvas.style.display = "block";
    this._resize();
    this.map.on("movestart", this._onMoveStart);
    this.map.on("moveend", this._onMoveEnd);
    this.map.on("resize", this._onResize);
    await this._reload();
    this._loop();
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.map.off("movestart", this._onMoveStart);
    this.map.off("moveend", this._onMoveEnd);
    this.map.off("resize", this._onResize);
    this._clear();
    this.canvas.style.display = "none";
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const { clientWidth: w, clientHeight: h } = this.map.getContainer();
    this.w = w; this.h = h;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.width = `${w}px`; this.canvas.style.height = `${h}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _clear() { this.ctx.clearRect(0, 0, this.w || 0, this.h || 0); }

  async _reload() {
    if (!this.running) return;
    const b = this.map.getBounds();
    const south = Math.max(-85, b.getSouth()), north = Math.min(85, b.getNorth());
    const west = Math.max(-180, b.getWest()), east = Math.min(180, b.getEast());
    const qs = new URLSearchParams({ south, west, north, east, nx: 14, ny: 10 });
    try {
      this.onStatus("Cargando viento 10 m…");
      const res = await fetch(`/api/wind-grid?${qs}`);
      if (!res.ok) throw new Error(await res.text());
      this.grid = await res.json();
      this.onStatus(`Viento 10 m · máx ${this.grid.max_speed_ms} m/s · ${this.grid.source}`);
    } catch (err) {
      console.error("wind-grid", err);
      this.onStatus("No se pudo cargar el viento");
      this.grid = null;
    }
    this._spawnAll();
    this.paused = false;
  }

  /** Interpolación bilineal en la malla (filas sur->norte, columnas oeste->este). */
  sample(lng, lat) {
    const g = this.grid;
    if (!g) return null;
    const [s, w, n, e] = g.bbox;
    if (lat < s || lat > n || lng < w || lng > e) return null;
    const fx = ((lng - w) / (e - w)) * (g.nx - 1);
    const fy = ((lat - s) / (n - s)) * (g.ny - 1);
    const x0 = Math.floor(fx), y0 = Math.floor(fy);
    const x1 = Math.min(x0 + 1, g.nx - 1), y1 = Math.min(y0 + 1, g.ny - 1);
    const tx = fx - x0, ty = fy - y0;
    const idx = (x, y) => y * g.nx + x;
    const lerp2 = (arr) =>
      arr[idx(x0, y0)] * (1 - tx) * (1 - ty) + arr[idx(x1, y0)] * tx * (1 - ty) +
      arr[idx(x0, y1)] * (1 - tx) * ty + arr[idx(x1, y1)] * tx * ty;
    return [lerp2(g.u), lerp2(g.v)];
  }

  _spawn(p = {}) {
    p.x = Math.random() * this.w;
    p.y = Math.random() * this.h;
    p.age = Math.floor(Math.random() * 80);
    p.maxAge = 60 + Math.random() * 60;
    return p;
  }

  _spawnAll() {
    this.particles = Array.from({ length: this.count }, () => this._spawn());
  }

  _loop() {
    if (!this.running) return;
    this.raf = requestAnimationFrame(() => this._loop());
    if (this.paused || !this.grid) return;

    const ctx = this.ctx;
    // Desvanecer estelas anteriores.
    ctx.globalCompositeOperation = "destination-in";
    ctx.fillStyle = "rgba(0,0,0,0.93)";
    ctx.fillRect(0, 0, this.w, this.h);
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 1.3;

    const k = 0.35; // píxeles por frame por m/s
    for (const p of this.particles) {
      if (++p.age > p.maxAge) { this._spawn(p); continue; }
      const ll = this.map.unproject([p.x, p.y]);
      const wv = this.sample(ll.lng, ll.lat);
      if (!wv) { this._spawn(p); continue; }
      const [u, v] = wv;
      const speed = Math.hypot(u, v);
      const nx = p.x + u * k, ny = p.y - v * k; // v positivo = hacia el norte (arriba)
      ctx.strokeStyle = speedColor(speed);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(nx, ny);
      ctx.stroke();
      p.x = nx; p.y = ny;
      if (p.x < 0 || p.y < 0 || p.x > this.w || p.y > this.h) this._spawn(p);
    }
  }
}
