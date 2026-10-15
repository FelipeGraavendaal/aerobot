"""
Predicción local corregida por sensor (nowcast tipo MOS ligero).

    pred(h) = mediana_modelos(h) + sesgo · e^(−h/τ)
    sesgo   = observación − mediana_modelos(ahora)

El sesgo observado se "olvida" con una constante τ por variable (la memoria
de un error local de temperatura dura más que la de una ráfaga). La banda de
incertidumbre es el rango de los modelos desplazado por el sesgo, ampliado
con el piso de error de representación. No se corrige con lecturas marcadas
como falla o dudosas.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime

from .consensus import Consensus
from .models import VariableCheck
from .qc import FLOOR

TAU_H = {"temp_c": 6, "dewpoint_c": 8, "rh_pct": 5, "pressure_hpa": 12, "station_pressure": 12,
         "wind_kt": 3, "gust_kt": 2}
PREDICT_VARS = list(TAU_H)


def predict(cons: Consensus, checks: list[VariableCheck], now: datetime, hours: int = 12) -> dict:
    by_var = {c.variable: c for c in checks}
    idx = cons.hours_from(now)[: hours + 1]
    out = {}
    for var in PREDICT_VARS:
        c = by_var.get(var)
        trusted = c is not None and c.consensus_median is not None and c.verdict in ("consistente", "desviacion", "fenomeno_local")
        bias = (c.observed - c.consensus_median) if trusted else 0.0
        rows = []
        for k, i in enumerate(idx):
            vals = [m[var][i] for m in cons.members.values() if var in m and i < len(m[var]) and m[var][i] is not None]
            if not vals:
                continue
            med = statistics.median(vals)
            corr = bias * math.exp(-k / TAU_H[var])
            floor = FLOOR.get(var, 1.0) * (0.5 + 0.05 * k)
            lo, hi = min(vals) + corr - floor, max(vals) + corr + floor
            if var in ("wind_kt", "gust_kt", "rh_pct"):
                lo = max(0.0, lo)
            if var == "rh_pct":
                hi = min(100.0, hi)
            rows.append({"time": cons.times[i].isoformat(), "h": k, "value": round(med + corr, 1),
                         "low": round(lo, 1), "high": round(hi, 1), "models": len(vals)})
        if rows:
            out[var] = {"bias_applied": round(bias, 2), "trusted_observation": trusted, "tau_h": TAU_H[var], "series": rows}
    return out
