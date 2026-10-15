"""
Análisis de ecos de radar.

RainViewer publica mosaicos de radar como imágenes (tiles PNG coloreados),
no como valores numéricos de reflectividad. Para entregar al agente un dato
CUANTITATIVO (dBZ) se hace lo siguiente:

  1. Se muestrea una malla polar (centro + 2 anillos x 8 rumbos = 17 puntos)
     dentro del radio pedido.
  2. En cada punto se obtiene la tasa de precipitación actual (nowcast de
     15 min de Open-Meteo, que asimila radar donde existe).
  3. Se convierte la tasa R (mm/h) a reflectividad equivalente con la
     relación Z-R:  Z = a * R^b   ->   dBZ = 10 * log10(Z)
       - Marshall-Palmer (estratiforme):  a=200, b=1.6
       - WSR-88D convectiva:              a=300, b=1.4  (si hay chubascos/TS)

El resultado se etiqueta como "dBZ estimado". Además se adjunta el último
frame de RainViewer para que el frontend lo pinte en el mapa.
"""
from __future__ import annotations

import logging
import math
import random
import statistics
from datetime import datetime, timezone

from ..config import settings
from .geo import compass, destination, validate_coords
from .http import UpstreamError, get_json

log = logging.getLogger("aerobot.radar")

RAINVIEWER_META = "https://api.rainviewer.com/public/weather-maps.json"
# Esquema de color 2 (Universal Blue), suavizado 1, nieve 1. RainViewer sirve hasta z=7.
RAINVIEWER_TILE = "{host}{path}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"
RAINVIEWER_MAX_ZOOM = 7

_CONVECTIVE_CODES = {80, 81, 82, 85, 86, 95, 96, 99}
_THUNDER_CODES = {95, 96, 99}


def rain_rate_to_dbz(rate_mm_h: float, convective: bool = False) -> float | None:
    """Relación Z-R. Devuelve None si no hay precipitación."""
    if rate_mm_h is None or rate_mm_h <= 0.05:
        return None
    a, b = (300.0, 1.4) if convective else (200.0, 1.6)
    return round(10 * math.log10(a * rate_mm_h**b), 1)


def classify_dbz(dbz: float | None) -> str:
    if dbz is None:
        return "sin eco"
    if dbz < 20:
        return "eco débil / llovizna"
    if dbz < 35:
        return "lluvia ligera a moderada"
    if dbz < 45:
        return "lluvia moderada a fuerte"
    if dbz < 55:
        return "lluvia intensa / núcleo convectivo"
    return "núcleo severo (posible granizo)"


async def rainviewer_frames() -> dict:
    """Metadatos de los frames de radar disponibles en RainViewer."""
    meta = await get_json(RAINVIEWER_META, ttl=120)
    host = meta["host"]
    frames = [
        {
            "time": f["time"],
            "time_utc": datetime.fromtimestamp(f["time"], tz=timezone.utc).isoformat(),
            "tile_url": RAINVIEWER_TILE.format(host=host, path=f["path"]),
        }
        for f in (meta.get("radar") or {}).get("past", [])
    ]
    return {"source": "rainviewer", "max_zoom": RAINVIEWER_MAX_ZOOM, "frames": frames}


def _sample_points(lat: float, lon: float, radius_km: float) -> list[dict]:
    pts = [{"lat": lat, "lon": lon, "dist_km": 0.0, "bearing": None}]
    for frac in (0.5, 1.0):
        for brg in range(0, 360, 45):
            plat, plon = destination(lat, lon, brg, radius_km * frac)
            pts.append({"lat": plat, "lon": plon, "dist_km": round(radius_km * frac, 1), "bearing": brg})
    return pts


async def _open_meteo_nowcast(points: list[dict]) -> list[dict]:
    params = {
        "latitude": ",".join(f"{p['lat']:.4f}" for p in points),
        "longitude": ",".join(f"{p['lon']:.4f}" for p in points),
        "current": "precipitation,rain,showers,snowfall,weather_code",
        "timezone": "UTC",
    }
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key
    data = await get_json(settings.open_meteo_base, params=params, ttl=300)
    return data if isinstance(data, list) else [data]


def _mock_nowcast(points: list[dict]) -> list[dict]:
    rnd = random.Random(f"{points[0]['lat']:.1f}{points[0]['lon']:.1f}")
    cell_brg = rnd.choice(range(0, 360, 45))
    out = []
    for p in points:
        near_cell = p["bearing"] == cell_brg
        precip = round(rnd.uniform(1.5, 6.0), 2) if near_cell else round(max(0, rnd.gauss(0.05, 0.15)), 2)
        out.append({"current": {"precipitation": precip, "showers": precip if near_cell else 0,
                                "weather_code": 95 if near_cell and precip > 4 else 61 if precip else 0,
                                "interval": 900, "time": datetime.now(timezone.utc).isoformat()}})
    return out


async def analyze_radar_echoes(lat: float, lon: float, radius_km: float = 50) -> dict:
    lat, lon = validate_coords(lat, lon)
    radius_km = max(5.0, min(float(radius_km), 300.0))
    points = _sample_points(lat, lon, radius_km)

    source, warning = "open-meteo (nowcast 15 min) + Z-R", None
    try:
        results = await _open_meteo_nowcast(points)
    except UpstreamError as exc:
        if not settings.allow_mock_fallback:
            raise
        results, source = _mock_nowcast(points), "mock"
        warning = f"Datos SIMULADOS (no aptos para decisión operacional). Motivo: {exc}"

    samples, obs_time = [], None
    for p, r in zip(points, results):
        cur = (r or {}).get("current") or {}
        interval = cur.get("interval") or 900
        precip = cur.get("precipitation") or 0.0
        rate = precip * 3600.0 / interval  # mm en el intervalo -> mm/h
        code = cur.get("weather_code")
        convective = code in _CONVECTIVE_CODES or (cur.get("showers") or 0) > 0
        dbz = rain_rate_to_dbz(rate, convective)
        obs_time = obs_time or cur.get("time")
        samples.append({
            **p,
            "lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
            "rain_rate_mm_h": round(rate, 2),
            "dbz_est": dbz,
            "weather_code": code,
            "thunder": code in _THUNDER_CODES,
        })

    echoes = [s for s in samples if s["dbz_est"] is not None]
    strongest = max(echoes, key=lambda s: s["dbz_est"]) if echoes else None
    summary = {
        "coverage_pct": round(100 * len(echoes) / len(samples), 1),
        "max_dbz_est": strongest["dbz_est"] if strongest else None,
        "max_dbz_class": classify_dbz(strongest["dbz_est"] if strongest else None),
        "max_rain_rate_mm_h": max((s["rain_rate_mm_h"] for s in samples), default=0),
        "mean_dbz_est_in_echoes": round(statistics.fmean(s["dbz_est"] for s in echoes), 1) if echoes else None,
        "strongest_cell": None if not strongest else {
            "lat": strongest["lat"], "lon": strongest["lon"], "distance_km": strongest["dist_km"],
            "direction_from_center": compass(strongest["bearing"]) if strongest["bearing"] is not None else "centro",
        },
        "thunder_reported_points": sum(s["thunder"] for s in samples),
        "center_dbz_est": samples[0]["dbz_est"],
    }

    try:
        rv = await rainviewer_frames()
        latest = rv["frames"][-1] if rv["frames"] else None
        radar_frame = {"latest_frame_utc": latest["time_utc"] if latest else None,
                       "frames_available": len(rv["frames"]), "tile_url": latest["tile_url"] if latest else None}
    except (UpstreamError, KeyError) as exc:
        radar_frame = {"error": f"RainViewer no disponible: {exc}"}

    out = {
        "source": source,
        "method": "dBZ estimado vía Z-R (Marshall-Palmer 200R^1.6 estratiforme / 300R^1.4 convectivo)",
        "valid_time_utc": obs_time,
        "query": {"lat": lat, "lon": lon, "radius_km": radius_km, "sample_points": len(samples)},
        "summary": summary,
        "samples": samples,
        "rainviewer": radar_frame,
    }
    if warning:
        out["warning"] = warning
    return out
