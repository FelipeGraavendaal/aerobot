"""
Módulo 1 · Go / No-Go autónomo para UAV.

Por cada hora:
  • Perfil de viento 10/50/80/120 m AGL. 50 m se interpola con la ley potencial
    usando el exponente local α = ln(V80/V10) / ln(8).
  • Cizalladura vectorial β = |ΔV⃗| / ΔZ (s⁻¹) en las capas 10–50 m y 50–80 m
    (todas bajo 100 m AGL). Regla: β > 0,1 s⁻¹ ⇒ RECHAZO.
  • Viento crítico = máx(ráfaga 10 m, viento medio del perfil hasta 120 m).
    Regla: ≥ 25 kt ⇒ RECHAZO.
  • Visibilidad < 5 000 m (operación VLOS), precipitación ≥ 0,5 mm/h o
    tormenta ⇒ RECHAZO.
  • La observación real (METAR a ≤30 km) prevalece sobre el modelo en la hora actual.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..services.aviation import fetch_aviation_weather
from ..services.geo import validate_coords, wind_to_uv
from .forecast import point_forecast

KT_TO_MS = 0.514444
THUNDER = {95, 96, 99}


def _vec(speed_kt, dir_deg):
    if speed_kt is None or dir_deg is None:
        return None
    return wind_to_uv(speed_kt * KT_TO_MS, dir_deg)


def _circ_interp(d1, d2, w):
    if d1 is None or d2 is None:
        return d1 if d2 is None else d2
    diff = (d2 - d1 + 180) % 360 - 180
    return (d1 + w * diff) % 360


def profile_at(pf, i) -> dict:
    v10, v80, v120 = pf.get("wind_speed_10m", i), pf.get("wind_speed_80m", i), pf.get("wind_speed_120m", i)
    d10, d80, d120 = pf.get("wind_direction_10m", i), pf.get("wind_direction_80m", i), pf.get("wind_direction_120m", i)
    alpha = None
    if v10 and v80 and v10 > 0.5 and v80 > 0.5:
        alpha = math.log(v80 / v10) / math.log(8)
        v50 = v10 * 5 ** alpha
    else:
        v50 = None if v10 is None or v80 is None else (v10 + (v80 - v10) * 40 / 70)
    d50 = _circ_interp(d10, d80, 40 / 70)
    u10, u50, u80 = _vec(v10, d10), _vec(v50, d50), _vec(v80, d80)
    beta_low = math.dist(u10, u50) / 40 if u10 and u50 else None
    beta_mid = math.dist(u50, u80) / 30 if u50 and u80 else None
    betas = [b for b in (beta_low, beta_mid) if b is not None]
    return {
        "v10_kt": v10, "v50_kt": None if v50 is None else round(v50, 1), "v80_kt": v80, "v120_kt": v120,
        "dir10": d10, "dir120": d120, "alpha": None if alpha is None else round(alpha, 3),
        "beta_10_50": None if beta_low is None else round(beta_low, 4),
        "beta_50_80": None if beta_mid is None else round(beta_mid, 4),
        "beta_max": round(max(betas), 4) if betas else None,
    }


LEVELS_M = (10, 50, 80, 120)


def evaluate_hour(pf, i, max_wind_kt, max_beta, min_vis_m, obs: dict | None = None, max_agl_m: float = 120) -> dict:
    p = profile_at(pf, i)
    if max_agl_m < 80:     # la capa 50–80 m queda fuera de la envolvente
        p["beta_max"] = p["beta_10_50"]
    gust = pf.get("wind_gusts_10m", i)
    sustained = [p[f"v{z}_kt"] for z in LEVELS_M if z <= max(max_agl_m, 10) and p[f"v{z}_kt"] is not None]
    vis, precip, code = pf.get("visibility", i), pf.get("precipitation", i), pf.get("weather_code", i)
    source = "modelo"
    if obs:  # la observación manda en la hora actual
        if obs.get("gust_kt") is not None:
            gust = max(gust or 0, obs["gust_kt"])
        if obs.get("wind_kt") is not None:
            sustained.append(obs["wind_kt"])
        if obs.get("visibility_m") is not None:
            vis = obs["visibility_m"]
        if obs.get("thunder"):
            code = 95
        source = f"modelo + {obs['station']}"
    crit_wind = max([gust or 0] + sustained) if (gust is not None or sustained) else None
    reasons = []
    if crit_wind is not None and crit_wind >= max_wind_kt:
        reasons.append(f"viento crítico {crit_wind:.0f} kt ≥ {max_wind_kt:.0f} kt")
    if p["beta_max"] is not None and p["beta_max"] > max_beta:
        reasons.append(f"cizalladura β = {p['beta_max']:.3f} s⁻¹ > {max_beta} s⁻¹ bajo 100 m AGL")
    if vis is not None and vis < min_vis_m:
        reasons.append(f"visibilidad {vis:.0f} m < {min_vis_m:.0f} m (VLOS)")
    if precip is not None and precip >= 0.5:
        reasons.append(f"precipitación {precip:.1f} mm/h")
    if code in THUNDER:
        reasons.append("tormenta eléctrica")
    return {"time": pf.times[i].isoformat(), **p, "gust10_kt": gust, "critical_wind_kt": crit_wind,
            "visibility_m": vis, "precip_mm_h": precip, "go": not reasons, "reasons": reasons, "source": source}


def _local(t: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(t).astimezone(tz).strftime("%d-%m %H:%M")


async def go_no_go(lat: float, lon: float, hours: int = 24, max_wind_kt: float = 25, max_beta: float = 0.1,
                   min_visibility_m: float = 5000, min_window_h: int = 1, timezone_name: str = "America/Santiago",
                   max_agl_m: float = 120) -> dict:
    lat, lon = validate_coords(lat, lon)
    max_agl_m = max(10.0, min(float(max_agl_m), 120.0))
    hours = max(3, min(int(hours), 72))
    tz = ZoneInfo(timezone_name)
    pf = await point_forecast(lat, lon, hours=hours + 1)
    now = datetime.now(timezone.utc)
    i0 = min(range(len(pf.times)), key=lambda k: abs((pf.times[k] - now).total_seconds()))

    obs = None
    try:
        res = await fetch_aviation_weather(lat, lon, radius_km=30)
        m = res.get("metar")
        if m and res.get("source") != "mock" and m.get("distance_km") is not None and m["distance_km"] <= 30:
            obs = {"station": m["station"], "gust_kt": m.get("wind_gust_kt"), "wind_kt": m.get("wind_speed_kt"),
                   "visibility_m": m["visibility_sm"] * 1609.34 if m.get("visibility_sm") is not None else None,
                   "thunder": any("TS" in w for w in m.get("significant_weather") or []), "raw": m.get("raw_metar")}
    except Exception:
        obs = None

    rows = [evaluate_hour(pf, i, max_wind_kt, max_beta, min_visibility_m, obs if i == i0 else None, max_agl_m)
            for i in range(i0, len(pf.times))]
    now_row = rows[0]

    # Próxima ventana apta (≥ min_window_h horas consecutivas) y próxima hora bajo 25 kt.
    window = None
    run: list[dict] = []
    for r in rows[1:] if not now_row["go"] else rows:
        run = run + [r] if r["go"] else []
        if len(run) >= min_window_h:
            end = run[-1]
            for r2 in rows[rows.index(end) + 1:]:
                if not r2["go"]:
                    break
                end = r2
            window = {"start": run[0]["time"], "end": (datetime.fromisoformat(end["time"]) + timedelta(hours=1)).isoformat()}
            break
    below = next((r for r in rows if r["critical_wind_kt"] is not None and r["critical_wind_kt"] < max_wind_kt), None)

    verdict = "[VENTANA APROBADA]" if now_row["go"] else "[VENTANA RECHAZADA]"
    p = now_row
    v50_txt = f"α = {p['alpha']}, V50 = V10·5^α = {p['v50_kt']} kt" if p["alpha"] is not None else \
        f"viento débil: V50 interpolado linealmente = {p['v50_kt']} kt"
    calc = (f"β(10–50 m) = |ΔV⃗|/ΔZ = {p['beta_10_50']} s⁻¹; β(50–80 m) = {p['beta_50_80']} s⁻¹ ({v50_txt})"
            if p["beta_max"] is not None else "β no calculable")
    summary = (f"{verdict} {_local(p['time'], tz)} hora local · viento 10/50/80/120 m: "
               f"{p['v10_kt']}/{p['v50_kt']}/{p['v80_kt']}/{p['v120_kt']} kt · ráfaga {p['gust10_kt']} kt · {calc}")
    if p["reasons"]:
        summary += " · Motivos: " + "; ".join(p["reasons"])
    return {
        "verdict": verdict, "go": now_row["go"], "summary": summary,
        "now": now_row, "observation": obs,
        "next_window": None if not window else {**window, "start_local": _local(window["start"], tz), "end_local": _local(window["end"], tz)},
        "next_hour_below_threshold": None if not below else {"time": below["time"], "local": _local(below["time"], tz),
                                                              "critical_wind_kt": below["critical_wind_kt"]},
        "profile_now": [{"agl_m": z, "wind_kt": now_row[f"v{z}_kt"], "within_envelope": z <= max_agl_m} for z in LEVELS_M],
        "criteria": {"max_wind_kt": max_wind_kt, "max_agl_m": max_agl_m, "max_beta_s-1": max_beta, "min_visibility_m": min_visibility_m,
                     "min_window_h": min_window_h},
        "hourly": [{k: r[k] for k in ("time", "go", "critical_wind_kt", "gust10_kt", "v120_kt", "beta_max", "visibility_m", "reasons")}
                   for r in rows[:24]],
        "source": pf.source, "estimated_variables": pf.estimated,
        "notes": ["El viento a 50 m es interpolado (ley potencial) y el de 80/120 m es de modelo, no medido."]
                 + (["Sin viento en altura en la fuente: 80/120 m extrapolados desde 10 m (α = 1/7)."] if pf.estimated else []),
    }
