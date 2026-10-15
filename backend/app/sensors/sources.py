"""
Fuentes de sensores de datos libres.

- METAR (NOAA AviationWeather): red aeronáutica global, sin key, con historial
  de 6 h para detectar valores pegados y saltos.
- openSenseMap: estaciones ciudadanas abiertas (densas en Europa, escasas en
  Sudamérica). Temperatura, humedad y presión; se descartan las inactivas.
- Sensores propios (custom): cualquier equipo que haga POST a la API.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from ..services.http import UpstreamError, get_json
from .models import Reading, Sensor

log = logging.getLogger("aerobot.sensors.sources")

AWC_METAR = "https://aviationweather.gov/api/data/metar"
OSM_BOXES = "https://api.opensensemap.org/boxes"


def _rh(temp: float | None, dew: float | None) -> float | None:
    """Humedad relativa por Magnus."""
    if temp is None or dew is None:
        return None
    a, b = 17.625, 243.04
    return round(100 * math.exp(a * dew / (b + dew)) / math.exp(a * temp / (b + temp)), 1)


def _vis_m(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("+", "")) * 1609.34
    except ValueError:
        return None


async def metar_sensors(south: float, west: float, north: float, east: float) -> list[Sensor]:
    data = await get_json(AWC_METAR, ttl=300, params={
        "bbox": f"{south:.3f},{west:.3f},{north:.3f},{east:.3f}", "format": "json", "hours": 6})
    sensors: dict[str, Sensor] = {}
    for m in data or []:
        sid = m.get("icaoId")
        if not sid or m.get("lat") is None or m.get("obsTime") is None:
            continue
        s = sensors.setdefault(sid, Sensor(
            id=f"metar:{sid}", name=f"{sid} · {(m.get('name') or '').split(',')[0]}", network="metar",
            lat=m["lat"], lon=m["lon"], elevation_m=m.get("elev")))
        vals = {
            "temp_c": m.get("temp"), "dewpoint_c": m.get("dewp"), "rh_pct": _rh(m.get("temp"), m.get("dewp")),
            "pressure_hpa": m.get("altim"), "wind_kt": m.get("wspd") if isinstance(m.get("wspd"), (int, float)) else None,
            "wind_dir": m.get("wdir") if isinstance(m.get("wdir"), (int, float)) else None,
            "gust_kt": m.get("wgst"),
        }
        s.readings.append(Reading(datetime.fromtimestamp(m["obsTime"], tz=timezone.utc),
                                  {k: float(v) for k, v in vals.items() if v is not None}))
    for s in sensors.values():
        s.readings.sort(key=lambda r: r.time)
        # Un METAR repetido (SPECI + METAR misma hora) no debe contarse como "valor pegado".
        dedup: dict[datetime, Reading] = {r.time: r for r in s.readings}
        s.readings = sorted(dedup.values(), key=lambda r: r.time)
    return list(sensors.values())


def _osm_variable(title: str, unit: str) -> str | None:
    t, u = (title or "").lower(), (unit or "").lower()
    if "temp" in t and ("°" in u or u in ("c", "celsius")):
        return "temp_c"
    if u == "%" and any(k in t for k in ("feucht", "hum", "rh")):
        return "rh_pct"
    if u in ("hpa", "pa", "mbar") and any(k in t for k in ("druck", "pres", "luft")):
        return "station_pressure"
    return None


async def opensensemap_sensors(south: float, west: float, north: float, east: float,
                               max_age: timedelta = timedelta(hours=2)) -> list[Sensor]:
    data = await get_json(OSM_BOXES, ttl=600, params={
        "bbox": f"{west:.3f},{south:.3f},{east:.3f},{north:.3f}", "full": "true", "exposure": "outdoor"})
    now = datetime.now(timezone.utc)
    out = []
    for b in data or []:
        coords = (b.get("currentLocation") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        vals, newest = {}, None
        for s in b.get("sensors") or []:
            lm = s.get("lastMeasurement") or {}
            var = _osm_variable(s.get("title"), s.get("unit"))
            if not var or lm.get("value") in (None, ""):
                continue
            t = datetime.fromisoformat(lm["createdAt"].replace("Z", "+00:00"))
            if now - t > max_age:
                continue
            try:
                v = float(lm["value"])
            except ValueError:
                continue
            if var == "station_pressure" and (s.get("unit") or "").lower() == "pa":
                v /= 100
            vals[var] = v
            newest = max(newest or t, t)
        if not vals:
            continue
        out.append(Sensor(id=f"osm:{b['_id']}", name=(b.get("name") or "openSenseMap")[:60], network="opensensemap",
                          lat=coords[1], lon=coords[0], elevation_m=coords[2] if len(coords) > 2 else None,
                          readings=[Reading(newest, vals)]))
    return out


class CustomSensorStore:
    """Sensores propios que envían lecturas por la API (en memoria, 48 lecturas por sensor)."""

    def __init__(self, max_readings: int = 48):
        self._sensors: dict[str, Sensor] = {}
        self.max_readings = max_readings

    def register(self, slug: str, name: str, lat: float, lon: float, elevation_m: float | None,
                 poi_id: str | None) -> Sensor:
        s = Sensor(id=f"custom:{slug}", name=name, network="custom", lat=lat, lon=lon,
                   elevation_m=elevation_m, poi_id=poi_id)
        old = self._sensors.get(s.id)
        if old:
            s.readings = old.readings
        self._sensors[s.id] = s
        return s

    def add_reading(self, sensor_id: str, reading: Reading) -> Sensor:
        s = self._sensors[sensor_id]
        s.readings = sorted(s.readings + [reading], key=lambda r: r.time)[-self.max_readings:]
        return s

    def get(self, sensor_id: str) -> Sensor | None:
        return self._sensors.get(sensor_id)

    def in_bbox(self, south, west, north, east) -> list[Sensor]:
        return [s for s in self._sensors.values() if south <= s.lat <= north and west <= s.lon <= east]


async def gather_sensors(south, west, north, east, custom: CustomSensorStore) -> tuple[list[Sensor], list[str]]:
    sensors, gaps = list(custom.in_bbox(south, west, north, east)), []
    for name, fn in (("METAR", metar_sensors), ("openSenseMap", opensensemap_sensors)):
        try:
            sensors += await fn(south, west, north, east)
        except UpstreamError as exc:
            gaps.append(f"{name} no disponible ({str(exc)[:80]})")
    return sensors, gaps
