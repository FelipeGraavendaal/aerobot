"""Utilidades geodésicas mínimas (esfera de radio medio terrestre)."""
from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088
_COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]


def validate_coords(lat: float, lon: float) -> tuple[float, float]:
    lat, lon = float(lat), float(lon)
    if not -90 <= lat <= 90:
        raise ValueError(f"Latitud fuera de rango: {lat}")
    if not -180 <= lon <= 180:
        raise ValueError(f"Longitud fuera de rango: {lon}")
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def destination(lat: float, lon: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Punto a `dist_km` desde (lat, lon) siguiendo el rumbo `bearing_deg`."""
    d = dist_km / EARTH_RADIUS_KM
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def bbox_around(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) aproximado alrededor de un punto."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return (max(lat - dlat, -90), max(lon - dlon, -180), min(lat + dlat, 90), min(lon + dlon, 180))


def compass(bearing_deg: float) -> str:
    return _COMPASS[int(((bearing_deg % 360) + 22.5) // 45) % 8]


def wind_to_uv(speed: float, direction_from_deg: float) -> tuple[float, float]:
    """Convierte (rapidez, dirección meteorológica 'desde') a componentes u/v."""
    rad = math.radians(direction_from_deg)
    return -speed * math.sin(rad), -speed * math.cos(rad)
