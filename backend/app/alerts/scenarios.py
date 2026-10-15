"""
Simulacros: telemetría sintética para probar el despachador de punta a punta.
Todas las alertas generadas aquí salen marcadas como SIMULACRO.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .rules import Telemetry


def _hours(now, start_h, end_h, **kw) -> list[Telemetry]:
    return [Telemetry(valid_at=now + timedelta(hours=h), simulated=True, **kw) for h in range(start_h, end_h)]


SCENARIOS = {
    "tormenta-scel": {
        "title": "Tormenta con granizo acercándose a SCEL",
        "poi_id": "scel",
        "build": lambda now: [
            Telemetry(valid_at=now, observed=True, simulated=True, source="METAR SCEL",
                      gust_kt=38, wind_kt=22, thunder=True, visibility_m=4000, ceiling_ft=1800),
            Telemetry(valid_at=now, observed=True, simulated=True, source="radar (dBZ estimado Z-R)",
                      dbz=57, cell_distance_km=14, cell_eta_min=25),
        ],
    },
    "viento-scci": {
        "title": "Temporal de viento en Punta Arenas",
        "poi_id": "scci",
        "build": lambda now: _hours(now, 2, 9, source="pronóstico", gust_kt=58, wind_kt=36),
    },
    "helada-casablanca": {
        "title": "Helada en viñedos de Casablanca",
        "poi_id": "casablanca",
        "build": lambda now: _hours(now, 5, 10, source="pronóstico", temp_c=-3.5),
    },
    "nieve-libertadores": {
        "title": "Nevazón en Paso Los Libertadores",
        "poi_id": "los-libertadores",
        "build": lambda now: _hours(now, 3, 11, source="pronóstico", snow_cm_h=3.2, visibility_m=400, gust_kt=30),
    },
    "lluvia-stgo": {
        "title": "Lluvia intensa en Santiago Centro",
        "poi_id": "stgo-centro",
        "build": lambda now: _hours(now, 1, 5, source="pronóstico", precip_mm_h=16, gust_kt=28),
    },
}


def build(scenario_id: str, now: datetime | None = None) -> tuple[str, list[Telemetry]]:
    sc = SCENARIOS[scenario_id]
    return sc["poi_id"], sc["build"](now or datetime.now(timezone.utc))
