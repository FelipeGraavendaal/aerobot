"""
Consenso multimodelo en la ubicación de cada sensor.

Miembros:
  - Open-Meteo (una sola petición multi-ubicación): GFS, ECMWF IFS, ICON (DWD),
    GEM (Canadá), JMA, Météo-France ARPEGE/AROME y UKMO.
  - MET Norway Locationforecast (gratuito, sin key; una petición por punto).

Se corrigen diferencias de altura entre el sensor y la celda del modelo:
temperatura (−6,5 °C/km), punto de rocío (−1,8 °C/km) y presión en estación
(ecuación hipsométrica). Así una estación en un cerro no se marca como falla
solo porque el modelo "ve" el valle.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings
from ..services.http import UpstreamError, get_json
from .models import Sensor

log = logging.getLogger("aerobot.sensors.consensus")

OM_MODELS = {
    "gfs": "gfs_seamless", "ecmwf": "ecmwf_ifs025", "icon": "icon_seamless", "gem": "gem_seamless",
    "jma": "jma_seamless", "meteofrance": "meteofrance_seamless", "ukmo": "ukmo_seamless",
}
OM_VAR = {
    "temp_c": "temperature_2m", "dewpoint_c": "dew_point_2m", "rh_pct": "relative_humidity_2m",
    "pressure_hpa": "pressure_msl", "station_pressure": "surface_pressure",
    "wind_kt": "wind_speed_10m", "wind_dir": "wind_direction_10m", "gust_kt": "wind_gusts_10m",
    "precip_mm_h": "precipitation",
}
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MS_TO_KT = 1.943844


@dataclass
class Consensus:
    """Pronóstico horario por miembro: members[modelo][variable] = [valor por hora]."""
    times: list[datetime] = field(default_factory=list)
    members: dict[str, dict[str, list]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def _index(self, t: datetime) -> int | None:
        if not self.times:
            return None
        i = min(range(len(self.times)), key=lambda k: abs((self.times[k] - t).total_seconds()))
        return i if abs((self.times[i] - t).total_seconds()) <= 5400 else None

    def at(self, variable: str, t: datetime) -> dict[str, float]:
        i = self._index(t)
        if i is None:
            return {}
        out = {}
        for model, vars_ in self.members.items():
            series = vars_.get(variable)
            if series and i < len(series) and series[i] is not None:
                out[model] = float(series[i])
        return out

    def hours_from(self, t: datetime) -> list[int]:
        i = self._index(t)
        return [] if i is None else list(range(i, len(self.times)))


def stats(values: dict[str, float], circular: bool = False) -> dict | None:
    v = list(values.values())
    if not v:
        return None
    if circular:
        s = sum(math.sin(math.radians(x)) for x in v)
        c = sum(math.cos(math.radians(x)) for x in v)
        mean = (math.degrees(math.atan2(s, c)) + 360) % 360
        r = math.hypot(s, c) / len(v)
        spread = math.degrees(math.sqrt(-2 * math.log(max(r, 1e-6))))
        return {"median": round(mean), "min": None, "max": None, "spread": round(spread, 1), "n": len(v)}
    return {"median": round(statistics.median(v), 2), "min": round(min(v), 2), "max": round(max(v), 2),
            "spread": round(statistics.pstdev(v), 2) if len(v) > 1 else None, "n": len(v)}


def _adjust(variable: str, value: float, dz: float, temp_c: float | None) -> float:
    if not dz:
        return value
    if variable == "temp_c":
        return value - 0.0065 * dz
    if variable == "dewpoint_c":
        return value - 0.0018 * dz
    if variable == "station_pressure":
        t_k = (temp_c if temp_c is not None else 15.0) + 273.15
        return value * math.exp(-dz / (29.27 * t_k))
    return value


async def _open_meteo(sensors: list[Sensor]) -> list[Consensus]:
    params = {
        "latitude": ",".join(f"{s.lat:.4f}" for s in sensors),
        "longitude": ",".join(f"{s.lon:.4f}" for s in sensors),
        "hourly": ",".join(sorted(set(OM_VAR.values()))),
        "models": ",".join(OM_MODELS.values()),
        "past_hours": 6, "forecast_hours": 13, "wind_speed_unit": "kn", "timezone": "UTC",
    }
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key
    data = await get_json(settings.open_meteo_base, params=params, ttl=900)
    blocks = data if isinstance(data, list) else [data]
    out = []
    for s, blk in zip(sensors, blocks):
        h = blk.get("hourly") or {}
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
        dz = (s.elevation_m - blk["elevation"]) if s.elevation_m is not None and blk.get("elevation") is not None else 0.0
        members: dict[str, dict[str, list]] = {}
        for name, key in OM_MODELS.items():
            mv = {}
            temps = h.get(f"temperature_2m_{key}") or [None] * len(times)
            for var, om in OM_VAR.items():
                series = h.get(f"{om}_{key}")
                if not series or all(x is None for x in series):
                    continue
                mv[var] = [None if x is None else _adjust(var, x, dz, temps[i]) for i, x in enumerate(series)]
            if mv:
                members[name] = mv
        out.append(Consensus(times, members, ["Open-Meteo"]))
    return out


async def _metno(s: Sensor) -> Consensus:
    params = {"lat": f"{s.lat:.4f}", "lon": f"{s.lon:.4f}"}
    if s.elevation_m is not None:
        params["altitude"] = int(s.elevation_m)
    data = await get_json(METNO_URL, params=params, ttl=1800,
                          headers={"User-Agent": "AeroBot/1.0 github.com/FelipeGraavendaal/aerobot"})
    times, series = [], {k: [] for k in ("temp_c", "dewpoint_c", "rh_pct", "pressure_hpa", "wind_kt", "wind_dir", "gust_kt", "precip_mm_h")}
    for row in (data or {}).get("properties", {}).get("timeseries", [])[:14]:
        d = row["data"]["instant"]["details"]
        times.append(datetime.fromisoformat(row["time"].replace("Z", "+00:00")))
        ws, wg = d.get("wind_speed"), d.get("wind_speed_of_gust")
        series["temp_c"].append(d.get("air_temperature"))
        series["dewpoint_c"].append(d.get("dew_point_temperature"))
        series["rh_pct"].append(d.get("relative_humidity"))
        series["pressure_hpa"].append(d.get("air_pressure_at_sea_level"))
        series["wind_kt"].append(ws * MS_TO_KT if ws is not None else None)
        series["wind_dir"].append(d.get("wind_from_direction"))
        series["gust_kt"].append(wg * MS_TO_KT if wg is not None else None)
        series["precip_mm_h"].append(((row["data"].get("next_1_hours") or {}).get("details") or {}).get("precipitation_amount"))
    return Consensus(times, {"metno": {k: v for k, v in series.items() if any(x is not None for x in v)}}, ["MET Norway"])


def _merge(a: Consensus, b: Consensus) -> Consensus:
    """Alinea MET Norway (sin horas pasadas) al eje temporal de Open-Meteo."""
    if not a.times:
        return b
    if not b.times:
        return a
    idx = {t: i for i, t in enumerate(b.times)}
    for model, vars_ in b.members.items():
        a.members[model] = {var: [vals[idx[t]] if t in idx else None for t in a.times] for var, vals in vars_.items()}
    a.sources = sorted(set(a.sources + b.sources))
    return a


async def fetch_consensus(sensors: list[Sensor], metno_limit: int = 12) -> tuple[dict[str, Consensus], list[str]]:
    """Consenso por id de sensor. MET Norway solo para los primeros `metno_limit` (respeta sus términos)."""
    gaps: list[str] = []
    result = {s.id: Consensus() for s in sensors}
    if not sensors:
        return result, gaps
    try:
        for s, c in zip(sensors, await _open_meteo(sensors)):
            result[s.id] = c
    except UpstreamError as exc:
        gaps.append(f"Open-Meteo no disponible ({str(exc)[:60]}); consenso solo con MET Norway")

    sem = asyncio.Semaphore(4)

    async def one(s: Sensor):
        async with sem:
            try:
                result[s.id] = _merge(result[s.id], await _metno(s))
            except UpstreamError as exc:
                log.info("MET Norway falló para %s: %s", s.id, exc)

    await asyncio.gather(*(one(s) for s in sensors[:metno_limit]))
    return result, gaps
