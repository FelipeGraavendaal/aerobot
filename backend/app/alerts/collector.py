"""
Recolector de telemetría para los Lugares de Interés.

Diseñado para cuidar la cuota gratuita de Open-Meteo: TODO el escaneo usa
dos peticiones multi-ubicación, sin importar cuántos POI existan:

  1) Centros de los POI: condición actual + pronóstico horario (24 h).
  2) Anillos de radar: 8 puntos por POI en su radio de vigilancia
     (precipitación actual -> dBZ estimado con la relación Z-R).

Además, para aeropuertos se consulta el METAR (observación real).
Si una fuente falla, NO se inventan datos: se registra en `data_gaps`.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..services.aviation import fetch_aviation_weather
from ..services.geo import destination
from ..services.http import UpstreamError, get_json
from ..services.models import _bulk_shear
from ..services.radar import rain_rate_to_dbz
from .pois import POI
from .rules import Telemetry

log = logging.getLogger("aerobot.alerts.collector")

HOURLY = [
    "temperature_2m", "precipitation", "snowfall", "weather_code", "visibility", "cape",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "wind_speed_700hPa", "wind_direction_700hPa", "wind_speed_500hPa", "wind_direction_500hPa",
]
CURRENT = ["temperature_2m", "precipitation", "snowfall", "weather_code", "wind_gusts_10m", "wind_speed_10m"]
THUNDER_CODES = {95, 96, 99}
HAIL_CODES = {96, 99}
RING_BEARINGS = list(range(0, 360, 45))


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _om_params(lats: list[float], lons: list[float], **extra) -> dict:
    p = {"latitude": ",".join(f"{x:.4f}" for x in lats), "longitude": ",".join(f"{x:.4f}" for x in lons),
         "timezone": "UTC", **extra}
    if settings.open_meteo_api_key:
        p["apikey"] = settings.open_meteo_api_key
    return p


def _as_list(data) -> list[dict]:
    return data if isinstance(data, list) else [data]


def forecast_snapshots(poi: POI, block: dict, now: datetime) -> list[Telemetry]:
    """Convierte la respuesta Open-Meteo de un POI en instantáneas horarias."""
    snaps: list[Telemetry] = []
    cur = block.get("current") or {}
    if cur:
        interval = cur.get("interval") or 900
        code = cur.get("weather_code")
        snaps.append(Telemetry(
            valid_at=now, observed=False, source="Open-Meteo (análisis actual)",
            gust_kt=cur.get("wind_gusts_10m"), wind_kt=cur.get("wind_speed_10m"),
            precip_mm_h=(cur.get("precipitation") or 0) * 3600 / interval,
            snow_cm_h=(cur.get("snowfall") or 0) * 3600 / interval,
            temp_c=cur.get("temperature_2m"), thunder=code in THUNDER_CODES, hail=code in HAIL_CODES,
        ))

    h = block.get("hourly") or {}
    times = h.get("time") or []
    g = lambda var, i: (h.get(var) or [None] * len(times))[i]  # noqa: E731
    cumulative = 0.0
    for i, ts in enumerate(times):
        valid = _parse_utc(ts)
        if valid < now - timedelta(minutes=30):
            continue
        precip = g("precipitation", i) or 0.0
        cumulative += precip
        code = g("weather_code", i)
        snaps.append(Telemetry(
            valid_at=max(valid, now), observed=False, source="pronóstico Open-Meteo",
            gust_kt=g("wind_gusts_10m", i), wind_kt=g("wind_speed_10m", i),
            precip_mm_h=precip,
            precip_24h_mm=cumulative if (cumulative >= 30 and precip > 0) else None,
            snow_cm_h=g("snowfall", i), temp_c=g("temperature_2m", i), visibility_m=g("visibility", i),
            cape_jkg=g("cape", i),
            shear_kt=_bulk_shear(g("wind_speed_10m", i), g("wind_direction_10m", i),
                                 g("wind_speed_500hPa", i), g("wind_direction_500hPa", i)),
            thunder=code in THUNDER_CODES, hail=False,  # granizo solo se acepta observado o por radar
        ))
    return snaps


def steering_wind(block: dict) -> tuple[float | None, float | None]:
    """Viento de 700 hPa de la primera hora: aproxima el desplazamiento de las celdas."""
    h = block.get("hourly") or {}
    try:
        return h["wind_speed_700hPa"][0], h["wind_direction_700hPa"][0]
    except (KeyError, IndexError, TypeError):
        return None, None


def cell_eta_min(distance_km: float, cell_bearing: float, steer_kt: float | None, steer_from: float | None) -> float | None:
    """
    Minutos hasta que una celda ubicada en `cell_bearing` (desde el POI) llegue al POI.
    La celda se aproxima si el viento rector SOPLA DESDE su dirección (±45°).
    """
    if not steer_kt or steer_from is None or steer_kt < 5:
        return None
    diff = abs((steer_from - cell_bearing + 180) % 360 - 180)
    if diff > 45:
        return None
    closing_kmh = steer_kt * 1.852 * max(0.5, math.cos(math.radians(diff)))  # componente hacia el POI
    return round(distance_km / closing_kmh * 60)


def radar_snapshot(poi: POI, ring: list[dict], center_block: dict, now: datetime) -> Telemetry | None:
    """Celda más intensa del anillo -> telemetría de radar (dBZ, distancia, ETA)."""
    best = None
    for brg, blk in zip(RING_BEARINGS, ring):
        cur = (blk or {}).get("current") or {}
        rate = (cur.get("precipitation") or 0) * 3600 / (cur.get("interval") or 900)
        code = cur.get("weather_code")
        dbz = rain_rate_to_dbz(rate, convective=code in {80, 81, 82, 95, 96, 99})
        if dbz is not None and (best is None or dbz > best[0]):
            best = (dbz, brg, code)
    if best is None:
        return None
    dbz, brg, code = best
    spd, frm = steering_wind(center_block)
    return Telemetry(
        valid_at=now, observed=True, source="radar (dBZ estimado Z-R)",
        dbz=dbz, cell_distance_km=poi.radius_km, cell_eta_min=cell_eta_min(poi.radius_km, brg, spd, frm),
        thunder=False, hail=code in HAIL_CODES,
    )


def metar_snapshot(res: dict, now: datetime) -> Telemetry | None:
    m = (res or {}).get("metar")
    dist = m.get("distance_km") if m else None
    if not m or res.get("source") == "mock" or dist is None or dist > 30:
        return None
    wx = " ".join(m.get("significant_weather") or [])
    vis_sm = m.get("visibility_sm")
    return Telemetry(
        valid_at=now, observed=True, source=f"METAR {m['station']}",
        gust_kt=m.get("wind_gust_kt"), wind_kt=m.get("wind_speed_kt"),
        visibility_m=vis_sm * 1609.34 if vis_sm is not None else None,
        ceiling_ft=m.get("ceiling_ft"), temp_c=m.get("temperature_c"),
        thunder="TS" in wx, hail="GR" in wx or "GS" in wx,
    )


async def collect(pois: list[POI], now: datetime | None = None) -> tuple[dict[str, list[Telemetry]], dict[str, list[str]]]:
    """Telemetría real por POI + brechas de datos por POI."""
    now = now or datetime.now(timezone.utc)
    snaps: dict[str, list[Telemetry]] = {p.id: [] for p in pois}
    gaps: dict[str, list[str]] = {p.id: [] for p in pois}
    if not pois:
        return snaps, gaps

    ring_pts = [destination(p.lat, p.lon, b, p.radius_km) for p in pois for b in RING_BEARINGS]
    forecast_req = get_json(settings.open_meteo_base, ttl=600, params=_om_params(
        [p.lat for p in pois], [p.lon for p in pois],
        hourly=",".join(HOURLY), current=",".join(CURRENT), forecast_hours=24, wind_speed_unit="kn"))
    ring_req = get_json(settings.open_meteo_base, ttl=300, params=_om_params(
        [x[0] for x in ring_pts], [x[1] for x in ring_pts], current="precipitation,weather_code"))
    airports = [p for p in pois if p.type == "aeropuerto"]
    metar_reqs = [fetch_aviation_weather(p.lat, p.lon, radius_km=30) for p in airports]

    forecast, ring, *metars = await asyncio.gather(forecast_req, ring_req, *metar_reqs, return_exceptions=True)

    blocks = None
    if isinstance(forecast, Exception):
        for p in pois:
            gaps[p.id].append(f"pronóstico no disponible ({type(forecast).__name__})")
    else:
        blocks = _as_list(forecast)
        for p, blk in zip(pois, blocks):
            snaps[p.id].extend(forecast_snapshots(p, blk, now))

    if isinstance(ring, Exception):
        for p in pois:
            gaps[p.id].append("radar no disponible")
    else:
        ring = _as_list(ring)
        for i, p in enumerate(pois):
            t = radar_snapshot(p, ring[i * 8:(i + 1) * 8], blocks[i] if blocks else {}, now)
            if t:
                snaps[p.id].append(t)

    for p, res in zip(airports, metars):
        if isinstance(res, Exception):
            gaps[p.id].append("METAR no disponible")
            continue
        t = metar_snapshot(res, now)
        if t:
            snaps[p.id].append(t)
        else:
            gaps[p.id].append("sin METAR real a ≤30 km")
    return snaps, gaps


__all__ = ["collect", "cell_eta_min", "forecast_snapshots", "UpstreamError"]
