"""Estructuras de datos de la red de sensores."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Variables canónicas (unidades fijas en todo el sistema).
VARIABLES = {
    "temp_c": "Temperatura (°C)",
    "dewpoint_c": "Punto de rocío (°C)",
    "rh_pct": "Humedad relativa (%)",
    "pressure_hpa": "Presión a nivel del mar / QNH (hPa)",
    "station_pressure": "Presión en la estación (hPa)",
    "wind_kt": "Viento medio (kt)",
    "wind_dir": "Dirección del viento (°)",
    "gust_kt": "Ráfaga (kt)",
    "precip_mm_h": "Precipitación (mm/h)",
}


@dataclass
class Reading:
    time: datetime                       # UTC
    values: dict[str, float]             # variable canónica -> valor


@dataclass
class Sensor:
    id: str                              # "metar:SCEL", "osm:<boxId>", "custom:<slug>"
    name: str
    network: str                         # metar | opensensemap | custom
    lat: float
    lon: float
    elevation_m: float | None = None
    poi_id: str | None = None            # si alimenta las alertas de un Lugar de Interés
    readings: list[Reading] = field(default_factory=list)   # ascendente por tiempo

    @property
    def latest(self) -> Reading | None:
        return self.readings[-1] if self.readings else None


@dataclass
class VariableCheck:
    variable: str
    observed: float
    consensus_median: float | None = None
    consensus_min: float | None = None
    consensus_max: float | None = None
    spread: float | None = None
    members: dict[str, float] = field(default_factory=dict)
    z: float | None = None
    neighbors: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)       # fuera_de_rango | valor_pegado | salto_brusco
    verdict: str = "sin_referencia"
    reason: str = ""


VERDICT_CONFIDENCE = {
    "consistente": 0.95, "fenomeno_local": 0.8, "desviacion": 0.7,
    "sin_referencia": 0.6, "dudoso": 0.45, "falla_probable": 0.15,
}
VERDICT_LABEL = {
    "consistente": "Consistente con los modelos",
    "fenomeno_local": "Fenómeno local probable (vecinos lo confirman)",
    "desviacion": "Desviación moderada",
    "sin_referencia": "Sin referencia de modelos",
    "dudoso": "Dudoso",
    "falla_probable": "Falla probable del sensor",
}
VERDICT_SEVERITY = ["consistente", "sin_referencia", "desviacion", "fenomeno_local", "dudoso", "falla_probable"]
