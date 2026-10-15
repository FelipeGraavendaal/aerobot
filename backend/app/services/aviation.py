"""
METAR/TAF desde AviationWeather.gov (NOAA, sin API key) con respaldo en
CheckWX (si hay CHECKWX_API_KEY) y, en último caso, datos simulados.

Estrategia: se consulta un bounding box alrededor del punto, se elige la
estación con observación más reciente más cercana y luego se pide su TAF.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone

from ..config import settings
from .geo import bbox_around, haversine_km, validate_coords
from .http import UpstreamError, get_json

log = logging.getLogger("aerobot.aviation")

AWC_BASE = "https://aviationweather.gov/api/data"
CHECKWX_BASE = "https://api.checkwx.com"

# Fenómenos significativos (subset de códigos METAR) -> descripción en español.
_WX_CODES = {
    "TS": "tormenta eléctrica", "GR": "granizo", "GS": "granizo pequeño", "FC": "nube embudo",
    "SQ": "turbonada", "FZ": "engelante", "SN": "nieve", "RA": "lluvia", "DZ": "llovizna",
    "FG": "niebla", "BR": "neblina", "HZ": "calima", "FU": "humo", "DU": "polvo",
    "SS": "tormenta de arena", "DS": "tormenta de polvo", "VA": "ceniza volcánica", "PL": "hielo granulado",
}
_WX_TOKEN = re.compile(r"^(\+|-|VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)+)$")


def _parse_visibility_sm(vis) -> float | None:
    """'6+' -> 6.0 ; '1/2' -> 0.5 ; 10 -> 10.0 (millas estatutarias)."""
    if vis is None:
        return None
    if isinstance(vis, (int, float)):
        return float(vis)
    s = str(vis).replace("+", "").strip()
    try:
        if " " in s:
            whole, frac = s.split(" ", 1)
            n, d = frac.split("/")
            return float(whole) + float(n) / float(d)
        if "/" in s:
            n, d = s.split("/")
            return float(n) / float(d)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def _ceiling_ft(clouds: list[dict]) -> int | None:
    bases = [c.get("base") for c in clouds or [] if c.get("cover") in {"BKN", "OVC", "VV", "OVX"} and c.get("base") is not None]
    return min(bases) if bases else None


def flight_category(ceiling_ft: int | None, vis_sm: float | None) -> str:
    """Categoría de vuelo FAA (VFR/MVFR/IFR/LIFR)."""
    c = ceiling_ft if ceiling_ft is not None else 99_999
    v = vis_sm if vis_sm is not None else 99.0
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


def significant_weather(raw: str) -> list[str]:
    found: list[str] = []
    for tok in (raw or "").split():
        m = _WX_TOKEN.match(tok)
        if not m:
            continue
        intensity, descriptor, phen = m.groups()
        parts = [_WX_CODES.get(descriptor or "", "")] + [_WX_CODES.get(phen[i:i + 2], phen[i:i + 2]) for i in range(0, len(phen), 2)]
        label = " ".join(p for p in parts if p)
        if intensity == "+":
            label += " (fuerte)"
        elif intensity == "-":
            label += " (débil)"
        elif intensity == "VC":
            label += " (en las cercanías)"
        found.append(f"{tok}: {label}")
    return found


def _normalize_awc(m: dict, lat: float, lon: float) -> dict:
    clouds = m.get("clouds") or []
    vis = _parse_visibility_sm(m.get("visib"))
    ceil = _ceiling_ft(clouds)
    temp, dew = m.get("temp"), m.get("dewp")
    altim = m.get("altim")
    return {
        "station": m.get("icaoId"),
        "station_name": m.get("name"),
        "station_lat": m.get("lat"),
        "station_lon": m.get("lon"),
        "distance_km": round(haversine_km(lat, lon, m["lat"], m["lon"]), 1),
        "observed_utc": m.get("reportTime"),
        "raw_metar": m.get("rawOb"),
        "temperature_c": temp,
        "dewpoint_c": dew,
        "temp_dewpoint_spread_c": round(temp - dew, 1) if temp is not None and dew is not None else None,
        "wind_dir_deg": m.get("wdir"),
        "wind_speed_kt": m.get("wspd"),
        "wind_gust_kt": m.get("wgst"),
        "visibility_sm": vis,
        "altimeter_hpa": altim,
        "clouds": [{"cover": c.get("cover"), "base_ft": c.get("base")} for c in clouds],
        "ceiling_ft": ceil,
        "flight_category": m.get("fltCat") or flight_category(ceil, vis),
        "significant_weather": significant_weather(m.get("rawOb", "")),
    }


async def _awc_nearest_metar(lat: float, lon: float, radius_km: float) -> dict | None:
    s, w, n, e = bbox_around(lat, lon, radius_km)
    data = await get_json(
        f"{AWC_BASE}/metar",
        params={"bbox": f"{s:.3f},{w:.3f},{n:.3f},{e:.3f}", "format": "json", "hours": 3},
        ttl=300,
    )
    if not data:
        return None
    # Una estación puede traer varias observaciones: nos quedamos con la más reciente.
    latest: dict[str, dict] = {}
    for m in data:
        if m.get("lat") is None or m.get("lon") is None:
            continue
        sid = m.get("icaoId")
        if sid not in latest or (m.get("obsTime") or 0) > (latest[sid].get("obsTime") or 0):
            latest[sid] = m
    if not latest:
        return None
    nearest = min(latest.values(), key=lambda m: haversine_km(lat, lon, m["lat"], m["lon"]))
    return _normalize_awc(nearest, lat, lon)


async def _awc_taf(icao: str) -> str | None:
    data = await get_json(f"{AWC_BASE}/taf", params={"ids": icao, "format": "json"}, ttl=600)
    if data and isinstance(data, list):
        return data[0].get("rawTAF")
    return None


async def _checkwx_nearest(lat: float, lon: float) -> dict | None:
    headers = {"X-API-Key": settings.checkwx_api_key or ""}
    data = await get_json(f"{CHECKWX_BASE}/metar/lat/{lat}/lon/{lon}/decoded", headers=headers, ttl=300)
    items = (data or {}).get("data") or []
    if not items:
        return None
    d = items[0]
    coords = (d.get("station") or {}).get("geometry", {}).get("coordinates") or [lon, lat]
    clouds = [{"cover": c.get("code"), "base_ft": c.get("base_feet_agl")} for c in d.get("clouds", [])]
    vis = (d.get("visibility") or {}).get("miles_float")
    ceil = _ceiling_ft([{"cover": c["cover"], "base": c["base_ft"]} for c in clouds])
    temp = (d.get("temperature") or {}).get("celsius")
    dew = (d.get("dewpoint") or {}).get("celsius")
    raw = d.get("raw_text", "")
    return {
        "station": d.get("icao"),
        "station_name": (d.get("station") or {}).get("name"),
        "station_lat": coords[1], "station_lon": coords[0],
        "distance_km": round(haversine_km(lat, lon, coords[1], coords[0]), 1),
        "observed_utc": d.get("observed"),
        "raw_metar": raw,
        "temperature_c": temp, "dewpoint_c": dew,
        "temp_dewpoint_spread_c": round(temp - dew, 1) if temp is not None and dew is not None else None,
        "wind_dir_deg": (d.get("wind") or {}).get("degrees"),
        "wind_speed_kt": (d.get("wind") or {}).get("speed_kts"),
        "wind_gust_kt": (d.get("wind") or {}).get("gust_kts"),
        "visibility_sm": vis,
        "altimeter_hpa": (d.get("barometer") or {}).get("hpa"),
        "clouds": clouds, "ceiling_ft": ceil,
        "flight_category": d.get("flight_category") or flight_category(ceil, vis),
        "significant_weather": significant_weather(raw),
    }


def _mock(lat: float, lon: float, reason: str) -> dict:
    rnd = random.Random(f"{lat:.1f},{lon:.1f}")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    wdir, wspd = rnd.choice(range(0, 360, 10)), rnd.randint(3, 22)
    temp = rnd.randint(5, 28)
    dew = temp - rnd.randint(1, 12)
    raw = f"METAR MOCK {now:%d%H%M}Z {wdir:03d}{wspd:02d}KT 9999 SCT030 {temp:02d}/{dew:02d} Q{rnd.randint(1005, 1025)}"
    return {
        "source": "mock",
        "warning": f"Datos SIMULADOS (no aptos para decisión operacional). Motivo: {reason}",
        "metar": {
            "station": "MOCK", "station_name": "Estación simulada", "station_lat": lat, "station_lon": lon,
            "distance_km": 0.0, "observed_utc": now.isoformat(), "raw_metar": raw,
            "temperature_c": temp, "dewpoint_c": dew, "temp_dewpoint_spread_c": temp - dew,
            "wind_dir_deg": wdir, "wind_speed_kt": wspd, "wind_gust_kt": None, "visibility_sm": 6.2,
            "altimeter_hpa": None, "clouds": [{"cover": "SCT", "base_ft": 3000}], "ceiling_ft": None,
            "flight_category": "VFR", "significant_weather": [],
        },
        "taf": None,
    }


async def fetch_aviation_weather(lat: float, lon: float, radius_km: float = 150) -> dict:
    """Último METAR (+TAF si existe) de la estación más cercana al punto."""
    lat, lon = validate_coords(lat, lon)
    radius_km = max(20.0, min(float(radius_km), 400.0))
    metar, source, errors = None, None, []

    try:
        metar = await _awc_nearest_metar(lat, lon, radius_km)
        source = "aviationweather.gov"
    except UpstreamError as exc:
        errors.append(f"AWC: {exc}")

    if metar is None and settings.checkwx_api_key:
        try:
            metar = await _checkwx_nearest(lat, lon)
            source = "checkwx"
        except UpstreamError as exc:
            errors.append(f"CheckWX: {exc}")

    if metar is None:
        reason = "; ".join(errors) or f"sin estaciones METAR en {radius_km:.0f} km"
        if settings.allow_mock_fallback and errors:
            return _mock(lat, lon, reason)
        return {"source": source or "aviationweather.gov", "metar": None, "taf": None, "note": reason}

    taf = None
    try:
        taf = await _awc_taf(metar["station"])
    except UpstreamError as exc:
        log.info("TAF no disponible para %s: %s", metar["station"], exc)

    return {
        "source": source,
        "query_point": {"lat": lat, "lon": lon, "search_radius_km": radius_km},
        "metar": metar,
        "taf": taf or "La estación no emite TAF o no está disponible.",
    }
