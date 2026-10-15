"""
SensorService: red de sensores libres + QC multimodelo + predicción + interpretación.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..services.geo import haversine_km
from .consensus import fetch_consensus
from .interpret import interpret
from .models import VERDICT_CONFIDENCE, VERDICT_LABEL, Reading, Sensor
from .predict import predict
from .qc import check_sensor, overall
from .sources import CustomSensorStore, gather_sensors, metar_sensors, opensensemap_sensors

log = logging.getLogger("aerobot.sensors")
MAX_SENSORS = 40


def _bbox_key(b) -> tuple:
    return tuple(round(x * 2) / 2 for x in b)


class SensorService:
    def __init__(self, llm: Any | None, model: str, use_llm: bool, alert_service=None):
        self.custom = CustomSensorStore()
        self.llm, self.model, self.use_llm = llm, model, use_llm
        self.alerts = alert_service
        self._last: dict[str, dict] = {}       # sensor_id -> reporte más reciente
        self._index: dict[str, Sensor] = {}    # sensor_id -> sensor (para reportes individuales)

    # ------------------------------------------------------------- red/listado
    async def network(self, south: float, west: float, north: float, east: float) -> dict:
        sensors, gaps = await gather_sensors(south, west, north, east, self.custom)
        clat, clon = (south + north) / 2, (west + east) / 2
        sensors.sort(key=lambda s: (s.network != "custom", haversine_km(clat, clon, s.lat, s.lon)))
        sensors = sensors[:MAX_SENSORS]
        consensus, cgaps = await fetch_consensus(sensors, metno_limit=12)
        out = []
        for s in sensors:
            self._index[s.id] = s
            rep = self._report(s, sensors, consensus)
            self._last[s.id] = rep
            out.append({k: rep[k] for k in ("sensor", "status", "status_label", "confidence", "observed_at", "latest")})
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "sensors": out, "data_gaps": gaps + cgaps}

    def _report(self, s: Sensor, others: list[Sensor], consensus) -> dict:
        checks = check_sensor(s, others, consensus)
        status = overall(checks)
        cons = consensus.get(s.id)
        latest = s.latest
        return {
            "sensor": {"id": s.id, "name": s.name, "network": s.network, "lat": s.lat, "lon": s.lon,
                       "elevation_m": s.elevation_m, "poi_id": s.poi_id},
            "observed_at": latest.time.isoformat() if latest else None,
            "latest": latest.values if latest else {},
            "status": status, "status_label": VERDICT_LABEL[status],
            "confidence": round(min((VERDICT_CONFIDENCE[c.verdict] for c in checks), default=0.6), 2),
            "checks": [asdict(c) for c in checks],
            "consensus_sources": sorted(cons.members) if cons else [],
            "prediction": predict(cons, checks, latest.time if latest else datetime.now(timezone.utc)) if cons else {},
        }

    # ---------------------------------------------------------- sensor único
    async def report(self, sensor_id: str) -> dict:
        s = self._index.get(sensor_id) or self.custom.get(sensor_id)
        if s is None:
            raise KeyError(sensor_id)
        # Vecinos frescos alrededor del sensor (radio ~50 km).
        d = 0.5
        others = list(self.custom.in_bbox(s.lat - d, s.lon - d, s.lat + d, s.lon + d))
        for fn in (metar_sensors, opensensemap_sensors):
            try:
                others += await fn(s.lat - d, s.lon - d, s.lat + d, s.lon + d)
            except Exception as exc:
                log.info("Vecinos no disponibles (%s): %s", fn.__name__, exc)
        fresh = next((o for o in others if o.id == s.id), s)
        if fresh is not s:
            self._index[s.id] = fresh
        others = [o for o in others if o.id != fresh.id][:15]
        consensus, gaps = await fetch_consensus([fresh] + others, metno_limit=6)
        rep = self._report(fresh, [fresh] + others, consensus)
        rep["data_gaps"] = gaps
        self._last[s.id] = rep
        return rep

    async def interpretation(self, sensor_id: str) -> dict:
        rep = await self.report(sensor_id)
        text, engine = await interpret(rep, self.llm, self.model, self.use_llm)
        return {**rep, "interpretation": text, "interpreter": engine}

    # ----------------------------------------------------- sensores propios
    def register(self, **kw) -> Sensor:
        s = self.custom.register(**kw)
        self._index[s.id] = s
        return s

    async def ingest(self, sensor_id: str, time: datetime, values: dict[str, float]) -> dict:
        s = self.custom.add_reading(sensor_id, Reading(time, values))
        self._index[s.id] = s
        rep = await self.report(sensor_id)
        alert = None
        if s.poi_id and self.alerts and rep["confidence"] >= 0.45:
            alert = await self._alert_from_sensor(s, rep)
        rep["alert"] = alert
        return rep

    async def _alert_from_sensor(self, s: Sensor, rep: dict) -> dict | None:
        """Una lectura creíble de un sensor propio alimenta al despachador de riesgos."""
        from ..alerts.rules import Telemetry

        poi = self.alerts.store.get(s.poi_id)
        if poi is None:
            return None
        bad = {c["variable"] for c in rep["checks"] if c["verdict"] in ("falla_probable", "dudoso")}
        v = {k: val for k, val in s.latest.values.items() if k not in bad}
        t = Telemetry(valid_at=s.latest.time, observed=True,
                      source=f"sensor {s.name} (confianza {int(rep['confidence'] * 100)} %)",
                      gust_kt=v.get("gust_kt"), wind_kt=v.get("wind_kt"), temp_c=v.get("temp_c"),
                      precip_mm_h=v.get("precip_mm_h"))
        alert = await self.alerts.evaluate(poi, [t])
        if alert["level"] > 0:
            self.alerts.add_sensor_alert(alert)
        return alert
