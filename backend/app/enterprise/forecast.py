"""
Pronóstico horario de un punto para los módulos Enterprise.

Fuente principal: Open-Meteo (modelo "best_match"), que publica viento a 10, 80,
120 y 180 m, isoterma 0 °C, déficit de presión de vapor, nubosidad y visibilidad.
Respaldo: MET Norway (solo superficie). Si falta el viento en altura, se
extrapola desde 10 m con la ley potencial V(z) = V10·(z/10)^α, α = 1/7, y el
resultado se marca como ESTIMADO (no medido).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings
from ..services.http import UpstreamError, get_json

HOURLY = [
    "temperature_2m", "relative_humidity_2m", "vapour_pressure_deficit", "precipitation", "snowfall", "weather_code",
    "cloud_cover", "visibility", "freezing_level_height",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "wind_speed_80m", "wind_direction_80m", "wind_speed_120m", "wind_direction_120m",
]
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MS_TO_KT = 1.943844
POWER_LAW_ALPHA = 1 / 7


@dataclass
class PointForecast:
    lat: float
    lon: float
    elevation_m: float | None
    times: list[datetime]
    series: dict[str, list]           # variable Open-Meteo -> valores (viento en kt)
    source: str
    estimated: list[str] = field(default_factory=list)   # variables extrapoladas, no modeladas

    def get(self, var: str, i: int):
        s = self.series.get(var)
        return s[i] if s and i < len(s) else None


async def _open_meteo(lat: float, lon: float, hours: int, past_hours: int) -> PointForecast:
    params = {"latitude": lat, "longitude": lon, "hourly": ",".join(HOURLY), "forecast_hours": hours,
              "past_hours": past_hours, "wind_speed_unit": "kn", "timezone": "UTC"}
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key
    d = await get_json(settings.open_meteo_base, params=params, ttl=900)
    h = d.get("hourly") or {}
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
    return PointForecast(lat, lon, d.get("elevation"), times, {k: h.get(k) for k in HOURLY if h.get(k)}, "Open-Meteo")


async def _metno(lat: float, lon: float, hours: int) -> PointForecast:
    d = await get_json(METNO_URL, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}, ttl=1800,
                       headers={"User-Agent": "AeroBot/1.0 github.com/FelipeGraavendaal/aerobot"})
    rows = (d or {}).get("properties", {}).get("timeseries", [])[:hours]
    s: dict[str, list] = {k: [] for k in ("temperature_2m", "relative_humidity_2m", "precipitation", "cloud_cover",
                                          "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")}
    times = []
    for r in rows:
        det = r["data"]["instant"]["details"]
        times.append(datetime.fromisoformat(r["time"].replace("Z", "+00:00")))
        ws, wg = det.get("wind_speed"), det.get("wind_speed_of_gust")
        s["temperature_2m"].append(det.get("air_temperature"))
        s["relative_humidity_2m"].append(det.get("relative_humidity"))
        s["cloud_cover"].append(det.get("cloud_area_fraction"))
        s["wind_speed_10m"].append(ws * MS_TO_KT if ws is not None else None)
        s["wind_direction_10m"].append(det.get("wind_from_direction"))
        s["wind_gusts_10m"].append(wg * MS_TO_KT if wg is not None else None)
        s["precipitation"].append(((r["data"].get("next_1_hours") or {}).get("details") or {}).get("precipitation_amount"))
    elev = ((d or {}).get("geometry") or {}).get("coordinates", [None, None, None])[2]
    return PointForecast(lat, lon, elev, times, s, "MET Norway")


def _fill_upper_winds(pf: PointForecast) -> None:
    """Ley potencial para niveles ausentes (marcados como estimados)."""
    v10, d10 = pf.series.get("wind_speed_10m"), pf.series.get("wind_direction_10m")
    if not v10:
        return
    for z in (80, 120):
        key = f"wind_speed_{z}m"
        if not pf.series.get(key):
            pf.series[key] = [None if v is None else v * (z / 10) ** POWER_LAW_ALPHA for v in v10]
            pf.series[f"wind_direction_{z}m"] = list(d10 or [None] * len(v10))
            pf.estimated.append(key)


async def point_forecast(lat: float, lon: float, hours: int = 48, past_hours: int = 0) -> PointForecast:
    try:
        pf = await _open_meteo(lat, lon, hours, past_hours)
    except UpstreamError:
        pf = await _metno(lat, lon, hours)   # si también falla, el UpstreamError sube al llamador
    _fill_upper_winds(pf)
    return pf
