"""
Control de calidad (QC) determinista de una lectura.

Para cada variable:
  1. Chequeos internos: rango físico, valor pegado (≥4 lecturas idénticas en
     ≥2 h) y salto brusco entre lecturas consecutivas.
  2. Contraste con el consenso multimodelo: z = (obs − mediana) / σ, con
     σ² = dispersión² + piso² (el piso cubre el error típico de representación).
  3. Chequeo de vecinos ("buddy check"): sensores a ≤50 km con su propia
     anomalía respecto de los modelos.
       - Vecinos con anomalía del mismo signo -> fenómeno local real (los modelos fallan).
       - Vecinos alineados con los modelos    -> la falla es del sensor.
"""
from __future__ import annotations

import math
from datetime import timedelta

from ..services.geo import haversine_km
from .consensus import Consensus, stats
from .models import VERDICT_SEVERITY, Sensor, VariableCheck

RANGES = {
    "temp_c": (-60, 55), "dewpoint_c": (-70, 35), "rh_pct": (0, 100.5), "pressure_hpa": (870, 1085),
    "station_pressure": (500, 1085), "wind_kt": (0, 200), "wind_dir": (0, 360), "gust_kt": (0, 250),
    "precip_mm_h": (0, 300),
}
FLOOR = {"temp_c": 1.5, "dewpoint_c": 2.0, "rh_pct": 10, "pressure_hpa": 2.5, "station_pressure": 2.5,
         "wind_kt": 5, "wind_dir": 40, "gust_kt": 8, "precip_mm_h": 2}
MAX_STEP_PER_H = {"temp_c": 8, "dewpoint_c": 10, "rh_pct": 40, "pressure_hpa": 6, "station_pressure": 6}
FLATLINE_VARS = {"temp_c", "dewpoint_c", "rh_pct", "station_pressure"}
NEIGHBOR_KM = 50


def _angle_diff(a: float, b: float) -> float:
    return (a - b + 180) % 360 - 180


# Variación que los modelos deben esperar en la ventana para que un valor constante sea sospechoso.
FLAT_EXPECTED_CHANGE = {"temp_c": 3.0, "dewpoint_c": 3.0, "rh_pct": 15.0, "station_pressure": 3.0}


def internal_flags(sensor: Sensor, variable: str, cons: Consensus | None = None) -> list[str]:
    flags = []
    latest = sensor.latest
    v = latest.values.get(variable)
    lo, hi = RANGES.get(variable, (-1e9, 1e9))
    if v is None:
        return flags
    if not lo <= v <= hi:
        flags.append("fuera_de_rango")
    series = [(r.time, r.values[variable]) for r in sensor.readings if variable in r.values]
    # Valor pegado: constante ≥2 h MIENTRAS los modelos esperan un cambio claro en esa ventana.
    # (Un METAR redondea a grados enteros: un rocío estable por la tarde NO es una falla.)
    n_min = 5 if sensor.network == "metar" else 4
    if variable in FLATLINE_VARS and len(series) >= n_min and cons is not None:
        tail = series[-n_min:]
        if len({x[1] for x in tail}) == 1 and tail[-1][0] - tail[0][0] >= timedelta(hours=2):
            medians = [stats(cons.at(variable, t))["median"] for t, _ in tail if cons.at(variable, t)]
            if len(medians) >= 2 and max(medians) - min(medians) >= FLAT_EXPECTED_CHANGE[variable]:
                flags.append("valor_pegado")
    if variable in MAX_STEP_PER_H and len(series) >= 2:
        (t0, v0), (t1, v1) = series[-2], series[-1]
        hours = max((t1 - t0).total_seconds() / 3600, 0.25)
        if abs(v1 - v0) / hours > MAX_STEP_PER_H[variable] and hours <= 3:
            flags.append("salto_brusco")
    return flags


def model_check(variable: str, obs: float, members: dict[str, float]) -> VariableCheck:
    vc = VariableCheck(variable=variable, observed=obs, members={k: round(v, 2) for k, v in members.items()})
    st = stats(members, circular=(variable == "wind_dir"))
    if not st:
        return vc
    vc.consensus_median, vc.consensus_min, vc.consensus_max, vc.spread = st["median"], st["min"], st["max"], st["spread"]
    floor = FLOOR.get(variable, 1.0)
    sigma = math.hypot(st["spread"] or floor, floor)
    dev = _angle_diff(obs, st["median"]) if variable == "wind_dir" else obs - st["median"]
    vc.z = round(dev / sigma, 2)
    return vc


def neighbor_anomalies(sensor: Sensor, variable: str, others: list[Sensor],
                       consensus: dict[str, Consensus]) -> list[dict]:
    out = []
    for o in others:
        if o.id == sensor.id or not o.latest or variable not in o.latest.values:
            continue
        d = haversine_km(sensor.lat, sensor.lon, o.lat, o.lon)
        if d > NEIGHBOR_KM:
            continue
        members = consensus.get(o.id, Consensus()).at(variable, o.latest.time)
        vc = model_check(variable, o.latest.values[variable], members)
        if vc.z is not None:
            out.append({"id": o.id, "name": o.name, "distance_km": round(d, 1), "observed": o.latest.values[variable], "z": vc.z})
    return sorted(out, key=lambda x: x["distance_km"])[:5]


def classify(vc: VariableCheck) -> None:
    if "fuera_de_rango" in vc.flags or "valor_pegado" in vc.flags:
        vc.verdict = "falla_probable"
        vc.reason = "valor fuera de rango físico" if "fuera_de_rango" in vc.flags else "valor idéntico por más de 2 h (sensor pegado)"
        return
    if vc.z is None:
        vc.verdict, vc.reason = ("dudoso", "salto brusco sin referencia") if vc.flags else ("sin_referencia", "sin modelos disponibles")
        return
    az = abs(vc.z)
    same_sign = [n for n in vc.neighbors if abs(n["z"]) >= 1.5 and math.copysign(1, n["z"]) == math.copysign(1, vc.z)]
    aligned = [n for n in vc.neighbors if abs(n["z"]) < 1.5]
    if az < 2:
        vc.verdict = "dudoso" if "salto_brusco" in vc.flags else "consistente"
        vc.reason = "salto brusco entre lecturas" if vc.flags else f"dentro de {az:.1f}σ del consenso"
    elif same_sign and len(same_sign) >= max(1, len(aligned)):
        vc.verdict = "fenomeno_local"
        vc.reason = f"{len(same_sign)} vecino(s) muestran la misma anomalía: los modelos no captan un fenómeno local"
    elif aligned and az >= 4:
        vc.verdict = "falla_probable"
        vc.reason = f"desvío de {az:.1f}σ mientras {len(aligned)} vecino(s) coinciden con los modelos"
    elif az >= 4:
        vc.verdict = "dudoso"
        vc.reason = f"desvío de {az:.1f}σ sin vecinos que lo confirmen"
    else:
        vc.verdict = "desviacion"
        vc.reason = f"desvío de {az:.1f}σ respecto del consenso"


def check_sensor(sensor: Sensor, others: list[Sensor], consensus: dict[str, Consensus]) -> list[VariableCheck]:
    latest = sensor.latest
    if not latest:
        return []
    cons = consensus.get(sensor.id, Consensus())
    checks = []
    for var, obs in latest.values.items():
        if var == "wind_dir" and (latest.values.get("wind_kt") or 0) < 8:
            continue  # la dirección con viento débil no es informativa
        vc = model_check(var, obs, cons.at(var, latest.time))
        vc.flags = internal_flags(sensor, var, cons)
        if vc.z is not None and abs(vc.z) >= 2:
            vc.neighbors = neighbor_anomalies(sensor, var, others, consensus)
        classify(vc)
        checks.append(vc)
    return checks


def overall(checks: list[VariableCheck]) -> str:
    if not checks:
        return "sin_referencia"
    return max((c.verdict for c in checks), key=VERDICT_SEVERITY.index)
