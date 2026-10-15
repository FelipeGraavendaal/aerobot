"""
Lugares de Interés (POI): los puntos que el despachador vigila.

Se guardan en memoria con persistencia opcional en JSON (AEROBOT_POI_FILE).
En Render (disco efímero) los POI agregados se pierden en cada despliegue;
para producción, mover a Postgres/Supabase.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..services.geo import validate_coords

log = logging.getLogger("aerobot.pois")

POI_TYPES = ("aeropuerto", "puerto", "obra", "evento", "ruta", "agricola", "urbano")


@dataclass
class POI:
    id: str
    name: str
    lat: float
    lon: float
    type: str = "urbano"
    radius_km: float = 40.0          # radio de vigilancia de ecos de radar
    timezone: str = "America/Santiago"
    icao: str | None = None          # estación METAR asociada (aeropuertos)
    custom: bool = field(default=False)

    def __post_init__(self) -> None:
        self.lat, self.lon = validate_coords(self.lat, self.lon)
        if self.type not in POI_TYPES:
            raise ValueError(f"Tipo de lugar inválido '{self.type}'. Opciones: {', '.join(POI_TYPES)}")
        self.radius_km = max(5.0, min(float(self.radius_km), 150.0))
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Zona horaria inválida '{self.timezone}'") from exc


DEFAULT_POIS = [
    POI("scel", "Aeropuerto AMB (SCEL), Pudahuel", -33.393, -70.786, "aeropuerto", 40, icao="SCEL"),
    POI("stgo-centro", "Santiago Centro", -33.4372, -70.6506, "urbano", 30),
    POI("puerto-valpo", "Puerto de Valparaíso", -33.036, -71.627, "puerto", 40),
    POI("casablanca", "Valle de Casablanca (viñedos)", -33.32, -71.41, "agricola", 30),
    POI("los-libertadores", "Paso Los Libertadores (Ruta 60-CH)", -32.835, -70.09, "ruta", 30),
    POI("scci", "Aeropuerto Punta Arenas (SCCI)", -53.003, -70.855, "aeropuerto", 50,
        timezone="America/Punta_Arenas", icao="SCCI"),
]


def slugify(text: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "poi"


class POIStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._pois: dict[str, POI] = {p.id: POI(**asdict(p)) for p in DEFAULT_POIS}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            for raw in json.loads(self.path.read_text("utf-8")):
                poi = POI(**raw)
                self._pois[poi.id] = poi
        except Exception:
            log.exception("No se pudo leer %s; se usan los POI por defecto", self.path)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            custom = [asdict(p) for p in self._pois.values() if p.custom]
            self.path.write_text(json.dumps(custom, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            log.warning("No se pudo persistir POIs en %s", self.path)

    def list(self) -> list[POI]:
        return list(self._pois.values())

    def get(self, poi_id: str) -> POI | None:
        return self._pois.get(poi_id)

    def add(self, name: str, lat: float, lon: float, type: str = "urbano", radius_km: float = 30,
            timezone: str = "America/Santiago", icao: str | None = None) -> POI:
        base = slugify(name)
        pid, n = base, 2
        while pid in self._pois:
            pid, n = f"{base}-{n}", n + 1
        poi = POI(pid, name.strip()[:80], lat, lon, type, radius_km, timezone, (icao or "").upper() or None, custom=True)
        self._pois[pid] = poi
        self._save()
        return poi

    def delete(self, poi_id: str) -> bool:
        poi = self._pois.get(poi_id)
        if not poi:
            return False
        del self._pois[poi_id]
        self._save()
        return True
