"""
AlertService: orquesta POIs -> telemetría -> reglas -> despachador.

- scan():        escanea todos los POI con datos reales (lo usa el bucle de fondo).
- evaluate():    evalúa telemetría externa contra un POI (sensores, integraciones).
- simulate():    ejecuta un simulacro predefinido.
- latest():      último estado (alertas reales + simulacros vigentes).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .collector import collect
from .dispatcher import Dispatcher
from .pois import POI, POIStore
from .rules import LEVELS, Assessment, Telemetry, assess
from .scenarios import SCENARIOS, build

log = logging.getLogger("aerobot.alerts")

LAYER_FOR = {"granizo": "radar", "tormenta": "radar", "precipitacion": "radar", "rafagas": "wind_particles",
             "convectivo": "satellite_ir", "nieve": "satellite_ir", "techo": "satellite_ir", "visibilidad": "satellite_ir"}
SIMULATION_TTL = timedelta(minutes=30)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


class AlertService:
    def __init__(self, store: POIStore, dispatcher: Dispatcher, scan_minutes: float = 15):
        self.store = store
        self.dispatcher = dispatcher
        self.scan_minutes = scan_minutes
        self._latest: dict[str, Any] = {"generated_at": None, "alerts": [], "pois": [], "data_gaps": {}}
        self._simulations: list[dict] = []
        self._sensor_alerts: list[dict] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ serialización
    async def _to_alert(self, a: Assessment) -> dict:
        message, engine = await self.dispatcher.dispatch(a)
        lv = LEVELS[a.level]
        win = a.window
        primary = a.primary
        alert_id = hashlib.sha1(self.dispatcher.signature(a).encode()).hexdigest()[:12]
        return {
            "id": alert_id,
            "poi": {k: v for k, v in asdict(a.poi).items() if k in ("id", "name", "lat", "lon", "type")},
            "level": a.level, "level_label": lv["label"], "color": lv["color"], "emoji": lv["emoji"],
            "message": message, "lines": message.splitlines(), "dispatcher": engine,
            "simulated": a.simulated, "generated_at": _iso(a.generated_at),
            "window": {"start": _iso(win[0]), "end": _iso(win[1])} if win else None,
            "hazards": [{"kind": h.kind, "level": h.level, "detail": h.detail, "observed": h.observed,
                         "source": h.source, "start": _iso(h.start), "end": _iso(h.end), "eta_min": h.eta_min}
                        for h in a.hazards],
            "sources": a.sources, "data_gaps": a.data_gaps,
            "map_hint": {"layer_type": LAYER_FOR.get(primary.kind, "none") if primary else "none",
                         "focus": {"lat": a.poi.lat, "lon": a.poi.lon},
                         "zoom_level": 10 if a.poi.type == "aeropuerto" else 9},
        }

    # ----------------------------------------------------------------- casos
    async def evaluate(self, poi: POI, telemetry: list[Telemetry], data_gaps: list[str] | None = None) -> dict:
        return await self._to_alert(assess(poi, telemetry, data_gaps=data_gaps))

    async def evaluate_live(self, poi: POI) -> dict:
        snaps, gaps = await collect([poi])
        return await self.evaluate(poi, snaps[poi.id], gaps[poi.id])

    async def simulate(self, scenario_id: str) -> dict:
        if scenario_id not in SCENARIOS:
            raise KeyError(scenario_id)
        poi_id, telemetry = build(scenario_id)
        poi = self.store.get(poi_id)
        if poi is None:
            raise KeyError(f"POI {poi_id} no existe")
        alert = await self.evaluate(poi, telemetry)
        alert["scenario"] = scenario_id
        self._simulations = [s for s in self._simulations if s["poi"]["id"] != poi_id] + [alert]
        return alert

    async def scan(self) -> dict:
        async with self._lock:
            pois = self.store.list()
            snaps, gaps = await collect(pois)
            alerts, statuses = [], []
            for poi in pois:
                a = assess(poi, snaps[poi.id], data_gaps=gaps[poi.id])
                statuses.append({"id": poi.id, "name": poi.name, "lat": poi.lat, "lon": poi.lon, "type": poi.type,
                                 "custom": poi.custom, "level": a.level, "data_gaps": gaps[poi.id],
                                 "telemetry_points": len(snaps[poi.id])})
                if a.level > 0:
                    alerts.append(await self._to_alert(a))
            alerts.sort(key=lambda x: -x["level"])
            self._latest = {"generated_at": _iso(datetime.now(timezone.utc)), "alerts": alerts, "pois": statuses,
                            "data_gaps": {k: v for k, v in gaps.items() if v}}
            log.info("Escaneo de alertas: %d POI, %d alertas", len(pois), len(alerts))
            return self.latest()

    def add_sensor_alert(self, alert: dict) -> None:
        """Alerta originada por un sensor propio validado (vigente 2 h, una por lugar)."""
        alert["origin"] = "sensor"
        self._sensor_alerts = [a for a in self._sensor_alerts if a["poi"]["id"] != alert["poi"]["id"]] + [alert]

    def latest(self) -> dict:
        now = datetime.now(timezone.utc)
        self._simulations = [s for s in self._simulations
                             if datetime.fromisoformat(s["generated_at"]) > now - SIMULATION_TTL]
        self._sensor_alerts = [s for s in self._sensor_alerts
                               if datetime.fromisoformat(s["generated_at"]) > now - timedelta(hours=2)]
        out = dict(self._latest)
        out["alerts"] = sorted(self._latest["alerts"] + self._sensor_alerts + self._simulations,
                               key=lambda x: (-x["level"], x["simulated"]))
        out["scan_minutes"] = self.scan_minutes
        return out

    # ------------------------------------------------------------ bucle de fondo
    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("Fallo en el escaneo de alertas")
            await asyncio.sleep(self.scan_minutes * 60)

    def start(self) -> None:
        if self.scan_minutes > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
