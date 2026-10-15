"""
Comparación de modelos numéricos GFS (NOAA) vs ECMWF IFS 0.25° vía Open-Meteo.

Variables: CAPE, CIN, Lifted Index, viento 10 m / ráfagas, viento en 850 y
500 hPa, precipitación. Se deriva una cizalladura "bulk" aproximada
10 m -> 500 hPa (~0-5.5 km) como proxy de la cizalladura 0-6 km.

Nota: Open-Meteo agrega el sufijo del modelo a cada variable cuando se piden
varios modelos (p. ej. "cape_gfs_seamless"). Algunas variables no existen en
todos los modelos (ECMWF no publica CIN): se reportan en `data_gaps`.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone

from ..config import settings
from .geo import validate_coords, wind_to_uv
from .http import UpstreamError, get_json

log = logging.getLogger("aerobot.models")

MODELS = {"gfs": "gfs_seamless", "ecmwf": "ecmwf_ifs025"}
VARIABLES = [
    "cape", "convective_inhibition", "lifted_index",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "wind_speed_850hPa", "wind_direction_850hPa",
    "wind_speed_500hPa", "wind_direction_500hPa",
    "precipitation", "temperature_2m",
]


def _clean(values: list | None, n: int) -> list[float | None]:
    return list(values) if values else [None] * n


def _max_with_index(series: list[float | None]) -> tuple[float | None, int | None]:
    best, idx = None, None
    for i, v in enumerate(series):
        if v is not None and (best is None or v > best):
            best, idx = v, i
    return best, idx


def _bulk_shear(s10, d10, s500, d500) -> float | None:
    if None in (s10, d10, s500, d500):
        return None
    u1, v1 = wind_to_uv(s10, d10)
    u2, v2 = wind_to_uv(s500, d500)
    return round(math.hypot(u2 - u1, v2 - v1), 1)


def _model_stats(h: dict, model_key: str, times: list[str]) -> dict:
    n = len(times)
    g = {var: _clean(h.get(f"{var}_{model_key}"), n) for var in VARIABLES}
    shear = [_bulk_shear(g["wind_speed_10m"][i], g["wind_direction_10m"][i],
                         g["wind_speed_500hPa"][i], g["wind_direction_500hPa"][i]) for i in range(n)]
    cape_max, i_cape = _max_with_index(g["cape"])
    gust_max, i_gust = _max_with_index(g["wind_gusts_10m"])
    shear_max, _ = _max_with_index(shear)
    cin_vals = [abs(v) for v in g["convective_inhibition"] if v is not None]
    li_vals = [v for v in g["lifted_index"] if v is not None]
    precip_vals = [v for v in g["precipitation"] if v is not None]
    return {
        "cape_max_jkg": cape_max,
        "cape_max_time": times[i_cape] if i_cape is not None else None,
        "cin_at_cape_max_jkg": abs(g["convective_inhibition"][i_cape]) if i_cape is not None and g["convective_inhibition"][i_cape] is not None else None,
        "cin_max_magnitude_jkg": max(cin_vals) if cin_vals else None,
        "lifted_index_min": min(li_vals) if li_vals else None,
        "gust_max_kt": gust_max,
        "gust_max_time": times[i_gust] if i_gust is not None else None,
        "wind_850hpa_max_kt": _max_with_index(g["wind_speed_850hPa"])[0],
        "wind_500hpa_max_kt": _max_with_index(g["wind_speed_500hPa"])[0],
        "bulk_shear_10m_500hpa_max_kt": shear_max,
        "shear_at_cape_max_kt": shear[i_cape] if i_cape is not None else None,
        "precip_total_mm": round(sum(precip_vals), 1) if precip_vals else None,
        "_series": {**g, "bulk_shear": shear},
    }


def _agreement(a: float | None, b: float | None, abs_floor: float) -> dict:
    """Diferencia absoluta/relativa y nivel de acuerdo entre dos modelos."""
    if a is None or b is None:
        return {"diff": None, "rel_diff_pct": None, "agreement": "no comparable"}
    diff = round(a - b, 1)
    ref = max(abs(a), abs(b), abs_floor)
    rel = round(100 * abs(diff) / ref, 0)
    level = "alta" if rel < 20 else "media" if rel < 50 else "baja"
    return {"diff": diff, "rel_diff_pct": rel, "agreement": level}


def _severity_hints(stats: dict[str, dict]) -> list[str]:
    hints = []
    for name, s in stats.items():
        cape, shear = s["cape_max_jkg"] or 0, s["shear_at_cape_max_kt"] or 0
        cin = s["cin_at_cape_max_jkg"]
        if cape >= 2500 and shear >= 40:
            hints.append(f"{name.upper()}: CAPE {cape:.0f} J/kg con cizalladura {shear:.0f} kt -> entorno de supercélulas.")
        elif cape >= 1000 and shear >= 30:
            hints.append(f"{name.upper()}: CAPE {cape:.0f} J/kg + cizalladura {shear:.0f} kt -> convección organizada posible.")
        elif cape >= 1000 and (cin is None or cin < 50):
            hints.append(f"{name.upper()}: CAPE {cape:.0f} J/kg con CIN débil -> convección probable si hay forzante.")
        if (s["gust_max_kt"] or 0) >= 50:
            hints.append(f"{name.upper()}: ráfagas de {s['gust_max_kt']:.0f} kt (umbral de daño).")
        if (s["lifted_index_min"] or 0) <= -6:
            hints.append(f"{name.upper()}: LI {s['lifted_index_min']:.1f} -> inestabilidad extrema.")
    return hints


def _mock_payload(lat: float, lon: float, hours: int) -> dict:
    rnd = random.Random(f"{lat:.1f}{lon:.1f}{hours}")
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    hourly: dict = {"time": times}
    for key, bias in (("gfs_seamless", 1.25), ("ecmwf_ifs025", 0.85)):
        peak = rnd.uniform(200, 1800) * bias
        for var in VARIABLES:
            vals = []
            for i in range(hours):
                diurnal = max(0.0, math.sin(math.pi * ((i + start.hour - 12) % 24) / 12))
                vals.append({
                    "cape": round(peak * diurnal, 0),
                    "convective_inhibition": None if key == "ecmwf_ifs025" else round(rnd.uniform(5, 120), 0),
                    "lifted_index": round(2 - 6 * diurnal * bias, 1),
                    "wind_speed_10m": round(rnd.uniform(5, 20) * bias, 1), "wind_direction_10m": rnd.choice(range(0, 360, 10)),
                    "wind_gusts_10m": round(rnd.uniform(12, 38) * bias, 1),
                    "wind_speed_850hPa": round(rnd.uniform(15, 40), 1), "wind_direction_850hPa": 300,
                    "wind_speed_500hPa": round(rnd.uniform(30, 70), 1), "wind_direction_500hPa": 280,
                    "precipitation": round(max(0, rnd.gauss(0.2, 0.6)) * diurnal, 1), "temperature_2m": round(12 + 8 * diurnal, 1),
                }[var])
            hourly[f"{var}_{key}"] = vals
    return {"hourly": hourly, "timezone": "UTC"}


async def compare_atmospheric_models(lat: float, lon: float, hours_ahead: int = 48) -> dict:
    lat, lon = validate_coords(lat, lon)
    hours_ahead = max(3, min(int(hours_ahead), 240))
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(VARIABLES),
        "models": ",".join(MODELS.values()),
        "forecast_hours": hours_ahead,
        "wind_speed_unit": "kn",
        "timezone": "UTC",
    }
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key

    source, warning = "open-meteo", None
    try:
        data = await get_json(settings.open_meteo_base, params=params, ttl=900)
    except UpstreamError as exc:
        if not settings.allow_mock_fallback:
            raise
        data, source = _mock_payload(lat, lon, hours_ahead), "mock"
        warning = f"Datos SIMULADOS (no aptos para decisión operacional). Motivo: {exc}"

    h = data.get("hourly", {})
    times = h.get("time", [])
    stats = {name: _model_stats(h, key, times) for name, key in MODELS.items()}

    gaps = [f"{var} no disponible en {name.upper()}"
            for name, s in stats.items() for var in VARIABLES
            if all(v is None for v in s["_series"][var])]

    comparison = {
        "cape_max": _agreement(stats["gfs"]["cape_max_jkg"], stats["ecmwf"]["cape_max_jkg"], 250),
        "gust_max": _agreement(stats["gfs"]["gust_max_kt"], stats["ecmwf"]["gust_max_kt"], 10),
        "wind_500hpa_max": _agreement(stats["gfs"]["wind_500hpa_max_kt"], stats["ecmwf"]["wind_500hpa_max_kt"], 10),
        "precip_total": _agreement(stats["gfs"]["precip_total_mm"], stats["ecmwf"]["precip_total_mm"], 2),
    }
    t_g, t_e = stats["gfs"]["cape_max_time"], stats["ecmwf"]["cape_max_time"]
    if t_g and t_e:
        comparison["cape_peak_timing_offset_h"] = round(
            (datetime.fromisoformat(t_g) - datetime.fromisoformat(t_e)).total_seconds() / 3600, 0)

    # Serie resumida cada 3 h para no saturar el contexto del LLM.
    step = 3 if hours_ahead <= 72 else 6
    timeline = []
    for i in range(0, len(times), step):
        row = {"time_utc": times[i]}
        for name in MODELS:
            s = stats[name]["_series"]
            row[f"cape_{name}"] = s["cape"][i]
            row[f"gust_{name}"] = s["wind_gusts_10m"][i]
            row[f"shear_{name}"] = s["bulk_shear"][i]
        timeline.append(row)

    for s in stats.values():
        s.pop("_series")

    out = {
        "source": source,
        "models": {"gfs": "NOAA GFS (seamless)", "ecmwf": "ECMWF IFS 0.25°"},
        "units": {"cape/cin": "J/kg (CIN expresado como magnitud)", "wind": "kt", "precip": "mm"},
        "query": {"lat": lat, "lon": lon, "hours_ahead": hours_ahead},
        "per_model": stats,
        "comparison": comparison,
        "severity_hints": _severity_hints(stats),
        "data_gaps": gaps,
        "timeline": timeline,
    }
    if warning:
        out["warning"] = warning
    return out


async def wind_grid(south: float, west: float, north: float, east: float, nx: int = 12, ny: int = 9) -> dict:
    """Malla regular de viento a 10 m (u/v en m/s) para animar partículas en el frontend."""
    nx, ny = max(2, min(nx, 16)), max(2, min(ny, 16))
    south, north = max(-85.0, south), min(85.0, north)
    west, east = max(-180.0, west), min(180.0, east)
    lats = [south + (north - south) * j / (ny - 1) for j in range(ny)]
    lons = [west + (east - west) * i / (nx - 1) for i in range(nx)]
    pts = [(la, lo) for la in lats for lo in lons]  # fila = latitud (sur -> norte)
    params = {
        "latitude": ",".join(f"{p[0]:.3f}" for p in pts),
        "longitude": ",".join(f"{p[1]:.3f}" for p in pts),
        "current": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
    }
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key
    source = "open-meteo"
    try:
        data = await get_json(settings.open_meteo_base, params=params, ttl=900)
        data = data if isinstance(data, list) else [data]
        speeds = [(d.get("current") or {}).get("wind_speed_10m") or 0.0 for d in data]
        dirs = [(d.get("current") or {}).get("wind_direction_10m") or 0.0 for d in data]
    except UpstreamError:
        if not settings.allow_mock_fallback:
            raise
        source = "mock"
        cx, cy = (west + east) / 2, (south + north) / 2
        speeds, dirs = [], []
        for la, lo in pts:  # vórtice sintético
            ang = math.degrees(math.atan2(la - cy, lo - cx))
            speeds.append(6 + 8 * math.exp(-((la - cy) ** 2 + (lo - cx) ** 2) / 50))
            dirs.append((ang + 360) % 360)
    u, v = zip(*(wind_to_uv(s, d) for s, d in zip(speeds, dirs)))
    return {
        "source": source, "bbox": [south, west, north, east], "nx": nx, "ny": ny,
        "u": [round(x, 2) for x in u], "v": [round(x, 2) for x in v],
        "max_speed_ms": round(max(speeds), 1) if speeds else 0,
    }
