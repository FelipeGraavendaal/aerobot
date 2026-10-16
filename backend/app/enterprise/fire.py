"""
Módulo 2a · Propagación de incendios (Rothermel 1972, constantes de Albini 1976).

    R = I_R · ξ · (1 + φ_W + φ_S) / (ρ_b · ε · Q_ig)          [ft/min]
    φ_W = C · U^B · (β/β_op)^(−E)                             (U: viento a media llama, ft/min)
    φ_S = 5,275 · β^(−0,3) · tan²θ                            (θ: pendiente)

Simplificaciones declaradas: clases muertas 1/10/100 h del modelo de Anderson
ponderadas por área superficial, sin combustible vivo; humedad del combustible fino = humedad de equilibrio de
Simard (1968) a partir de T y HR (equivalente a usar el VPD); viento a media
llama = viento 10 m ÷ 1,15 × factor de ajuste (WAF). La dirección de máximo
avance es la suma vectorial de los efectos de viento (a favor del viento) y
pendiente (cuesta arriba). La forma es elíptica con razón largo/ancho de
Anderson (1983).

Es una estimación de comportamiento del fuego para planificación, no un
simulador operativo (no modela focos secundarios, barreras ni cambios de
combustible).
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from ..config import settings
from ..services.geo import destination, validate_coords
from ..services.http import get_json
from .forecast import point_forecast

TPA_TO_LBFT2 = 0.0459137
FUEL_MODELS = {  # cargas muertas 1 h / 10 h / 100 h (t/acre), σ 1 h (1/ft), profundidad δ (ft), Mx, WAF
    "pastizal": {"anderson": 1, "w": (0.74, 0.0, 0.0), "sigma": 3500, "delta": 1.0, "mx": 0.12, "waf": 0.36},
    "pastizal_alto": {"anderson": 3, "w": (3.01, 0.0, 0.0), "sigma": 1500, "delta": 2.5, "mx": 0.25, "waf": 0.44},
    "matorral": {"anderson": 6, "w": (1.50, 2.50, 2.00), "sigma": 1750, "delta": 2.5, "mx": 0.25, "waf": 0.44},
    "matorral_denso": {"anderson": 4, "w": (5.01, 4.01, 2.00), "sigma": 2000, "delta": 6.0, "mx": 0.20, "waf": 0.55},
    "hojarasca": {"anderson": 8, "w": (1.50, 1.00, 2.50), "sigma": 2000, "delta": 0.2, "mx": 0.30, "waf": 0.28},
}
SIGMA_10H, SIGMA_100H = 109.0, 30.0
H_BTU, S_T, S_E, RHO_P = 8000.0, 0.0555, 0.010, 32.0
MS_TO_FTMIN = 196.85
FTMIN_TO_MMIN = 0.3048


def simard_emc(temp_c: float, rh: float) -> float:
    """Humedad de equilibrio del combustible muerto (fracción)."""
    t = temp_c * 9 / 5 + 32
    if rh < 10:
        emc = 0.03229 + 0.281073 * rh - 0.000578 * rh * t
    elif rh < 50:
        emc = 2.22749 + 0.160107 * rh - 0.01478 * t
    else:
        emc = 21.0606 + 0.005565 * rh ** 2 - 0.00035 * rh * t - 0.483199 * rh
    return max(0.02, emc / 100)


def rothermel(fuel: dict, mf: float, midflame_ftmin: float, slope_tan: float) -> dict:
    """Rothermel con las 3 clases de combustible muerto ponderadas por área superficial (sin combustible vivo)."""
    delta, mx = fuel["delta"], fuel["mx"]
    sig = (fuel["sigma"], SIGMA_10H, SIGMA_100H)
    w = [x * TPA_TO_LBFT2 for x in fuel["w"]]
    moist = (mf, mf + 0.01, mf + 0.02)                      # 10 h y 100 h: +1 % y +2 % sobre el fino
    area = [s_ * w_ / RHO_P for s_, w_ in zip(sig, w)]
    f = [a / sum(area) for a in area]
    sigma = sum(fi * si for fi, si in zip(f, sig))          # σ característico
    m_char = sum(fi * mi for fi, mi in zip(f, moist))
    w0 = sum(w)
    beta = w0 / (delta * RHO_P)
    beta_op = 3.348 * sigma ** -0.8189
    ratio = beta / beta_op
    gmax = sigma ** 1.5 / (495 + 0.0594 * sigma ** 1.5)
    a = 133 * sigma ** -0.7913
    gamma = gmax * ratio ** a * math.exp(a * (1 - ratio))
    wn = sum(fi * wi for fi, wi in zip(f, w)) * (1 - S_T)
    rm = min(m_char / mx, 1.0)
    eta_m = max(0.0, 1 - 2.59 * rm + 5.11 * rm ** 2 - 3.52 * rm ** 3)
    eta_s = 0.174 * S_E ** -0.19
    ir = gamma * wn * H_BTU * eta_m * eta_s
    xi = math.exp((0.792 + 0.681 * sigma ** 0.5) * (beta + 0.1)) / (192 + 0.2595 * sigma)
    c = 7.47 * math.exp(-0.133 * sigma ** 0.55)
    b = 0.02526 * sigma ** 0.54
    e = 0.715 * math.exp(-3.59e-4 * sigma)
    u = min(midflame_ftmin, 0.9 * ir) if ir > 0 else 0.0          # límite de viento de Rothermel
    phi_w = c * u ** b * ratio ** -e if u > 0 else 0.0
    phi_s = 5.275 * beta ** -0.3 * slope_tan ** 2
    rho_b = w0 / delta
    heat_sink = sum(fi * math.exp(-138 / si) * (250 + 1116 * mi) for fi, si, mi in zip(f, sig, moist))
    r0 = ir * xi / (rho_b * heat_sink) if eta_m > 0 else 0.0
    return {"r0_ftmin": r0, "phi_w": phi_w, "phi_s": phi_s, "ir_btu_ft2_min": ir, "beta": beta,
            "beta_op": beta_op, "C": c, "B": b, "E": e, "eta_m": eta_m, "extinguished": eta_m == 0, "sigma": sigma}


async def slope_aspect(lat: float, lon: float, d_m: float = 300) -> dict:
    """Pendiente y dirección cuesta arriba con 5 puntos del DEM (Copernicus 90 m vía Open-Meteo)."""
    pts = [(lat, lon)] + [destination(lat, lon, b, d_m / 1000) for b in (0, 90, 180, 270)]
    params = {"latitude": ",".join(f"{p[0]:.5f}" for p in pts), "longitude": ",".join(f"{p[1]:.5f}" for p in pts)}
    if settings.open_meteo_api_key:
        params["apikey"] = settings.open_meteo_api_key
    base = settings.open_meteo_base.replace("/forecast", "/elevation")
    z = (await get_json(base, params=params, ttl=86400))["elevation"]
    dzdx, dzdy = (z[2] - z[4]) / (2 * d_m), (z[1] - z[3]) / (2 * d_m)
    tan = math.hypot(dzdx, dzdy)
    upslope = (math.degrees(math.atan2(dzdx, dzdy)) + 360) % 360
    return {"elevation_m": z[0], "slope_pct": round(100 * tan, 1), "slope_deg": round(math.degrees(math.atan(tan)), 1),
            "upslope_bearing": round(upslope), "tan": tan}


def _ellipse(lat, lon, heading, head_m, back_m, lb, n=48) -> list[list[float]]:
    length = head_m + back_m
    a, bb = length / 2, length / (2 * lb)
    shift = head_m - a                     # centro respecto del foco, en la dirección de avance
    h = math.radians(heading)
    coords = []
    for k in range(n + 1):
        t = 2 * math.pi * k / n
        x_al, x_cr = shift + a * math.cos(t), bb * math.sin(t)      # a lo largo / transversal
        east = x_al * math.sin(h) + x_cr * math.cos(h)
        north = x_al * math.cos(h) - x_cr * math.sin(h)
        dlat = north / 111_320
        dlon = east / (111_320 * max(math.cos(math.radians(lat)), 0.05))
        coords.append([round(lon + dlon, 6), round(lat + dlat, 6)])
    return coords


async def fire_spread(lat: float, lon: float, fuel: str = "pastizal", hours: list[int] | None = None,
                      at_hour: int | None = None, humidity: float | None = None, wind_dir: float | None = None,
                      wind_speed_kt: float | None = None, temp_c: float | None = None) -> dict:
    """humidity/wind_dir/wind_speed_kt/temp_c, si se entregan, reemplazan al modelo en la hora evaluada (p. ej. telemetría de un sensor)."""
    lat, lon = validate_coords(lat, lon)
    if fuel not in FUEL_MODELS:
        raise ValueError(f"Combustible '{fuel}' no soportado: {', '.join(FUEL_MODELS)}")
    fm = FUEL_MODELS[fuel]
    hours = hours or [1, 3]
    pf = await point_forecast(lat, lon, hours=25)
    terrain_note = None
    try:
        terrain = await slope_aspect(lat, lon)
    except Exception as exc:  # sin DEM: se asume terreno plano y se declara
        terrain = {"elevation_m": pf.elevation_m, "slope_pct": 0.0, "slope_deg": 0.0, "upslope_bearing": 0, "tan": 0.0}
        terrain_note = f"DEM no disponible ({type(exc).__name__}): se asumió pendiente 0 %."

    now = datetime.now(timezone.utc)
    i0 = min(range(len(pf.times)), key=lambda k: abs((pf.times[k] - now).total_seconds()))

    sel_index = i0 + (at_hour or 0)
    overrides = {"t": temp_c, "rh": humidity, "ws": wind_speed_kt, "wd": wind_dir}

    def at(i):
        t, rh = pf.get("temperature_2m", i), pf.get("relative_humidity_2m", i)
        ws, wd, vpd = pf.get("wind_speed_10m", i), pf.get("wind_direction_10m", i), pf.get("vapour_pressure_deficit", i)
        if i == sel_index:
            t = overrides["t"] if overrides["t"] is not None else t
            rh = overrides["rh"] if overrides["rh"] is not None else rh
            ws = overrides["ws"] if overrides["ws"] is not None else ws
            wd = overrides["wd"] if overrides["wd"] is not None else wd
            if (overrides["t"] is not None or overrides["rh"] is not None) and t is not None and rh is not None:
                es = 0.6108 * math.exp(17.27 * t / (t + 237.3))     # Tetens, kPa
                vpd = round(es * (1 - rh / 100), 2)
        if None in (t, rh, ws, wd):
            return None
        mf = simard_emc(t, rh)
        midflame = ws * 0.514444 / 1.15 * fm["waf"] * MS_TO_FTMIN
        r = rothermel(fm, mf, midflame, terrain["tan"])
        # Suma vectorial de efectos: viento empuja a sotavento, la pendiente cuesta arriba.
        downwind = math.radians((wd + 180) % 360)
        up = math.radians(terrain["upslope_bearing"])
        x = r["phi_w"] * math.sin(downwind) + r["phi_s"] * math.sin(up)
        y = r["phi_w"] * math.cos(downwind) + r["phi_s"] * math.cos(up)
        phi_eff = math.hypot(x, y)
        heading = (math.degrees(math.atan2(x, y)) + 360) % 360 if phi_eff > 0 else (wd + 180) % 360
        ros_head = r["r0_ftmin"] * (1 + phi_eff) * FTMIN_TO_MMIN
        u_eff_mph = ((phi_eff * (r["beta"] / r["beta_op"]) ** r["E"] / r["C"]) ** (1 / r["B"]) / 88) if phi_eff > 0 else 0
        lb = min(8.0, 0.936 * math.exp(0.2566 * u_eff_mph) + 0.461 * math.exp(-0.1548 * u_eff_mph) - 0.397)
        lb = max(1.0, lb)
        ecc = math.sqrt(lb ** 2 - 1) / lb
        ros_back = ros_head * (1 - ecc) / (1 + ecc)
        wind_kmh = ws * 1.852
        return {"time": pf.times[i].isoformat(), "temp_c": t, "rh_pct": rh, "vpd_kpa": vpd, "wind_kt": ws, "wind_dir": wd,
                "fuel_moisture_pct": round(mf * 100, 1), "midflame_wind_kmh": round(midflame / MS_TO_FTMIN * 3.6, 1),
                "phi_w": round(r["phi_w"], 2), "phi_s": round(r["phi_s"], 2), "phi_eff": round(phi_eff, 2),
                "ros_head_m_min": round(ros_head, 2), "ros_back_m_min": round(ros_back, 3), "heading_deg": round(heading),
                "length_to_breadth": round(lb, 2), "extinguished": r["extinguished"],
                "rule_30_30_30": t >= 30 and rh <= 30 and wind_kmh >= 30,
                "_ir": r["ir_btu_ft2_min"]}

    rows = [r for r in (at(i) for i in range(i0, len(pf.times))) if r]
    if not rows:
        raise ValueError("Pronóstico sin temperatura/humedad/viento suficientes")
    worst = max(rows, key=lambda r: r["ros_head_m_min"])
    sel = rows[min(at_hour or 0, len(rows) - 1)]

    features = []
    for h in sorted(hours, reverse=True):
        head, back = sel["ros_head_m_min"] * 60 * h, sel["ros_back_m_min"] * 60 * h
        if head < 1:
            continue
        features.append({"type": "Feature", "properties": {"label": f"Cono de peligro +{h} h", "hours": h,
                         "head_m": round(head), "level": 3 if h == min(hours) else 2},
                         "geometry": {"type": "Polygon", "coordinates": [_ellipse(lat, lon, sel["heading_deg"], head, back, sel["length_to_breadth"])]}})
    features.append({"type": "Feature", "properties": {"label": "Foco", "kind": "ignition"},
                     "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    for r in rows:
        r.pop("_ir", None)
    reach = max([f["properties"].get("head_m", 0) for f in features] + [100])
    zoom = 14 if reach < 1000 else 13 if reach < 2500 else 12 if reach < 5000 else 11 if reach < 10000 else 10 if reach < 20000 else 9
    return {
        "suggested_zoom": zoom,
        "fuel": {"name": fuel, **fm}, "terrain": {k: v for k, v in terrain.items() if k != "tan"},
        "now": sel, "inputs_overridden": [k for k, v in {"temp_c": temp_c, "humidity": humidity, "wind_dir": wind_dir,
                                                            "wind_speed_kt": wind_speed_kt}.items() if v is not None],
        "worst_24h": worst, "rule_30_30_30_now": sel["rule_30_30_30"],
        "rule_30_30_30_hours": [r["time"] for r in rows if r["rule_30_30_30"]][:24],
        "cone": {"type": "FeatureCollection", "features": features},
        "formula": "R = I_R·ξ·(1+φ_W+φ_S)/(ρ_b·ε·Q_ig); φ_W = C·U^B·(β/β_op)^−E; φ_S = 5,275·β^−0,3·tan²θ",
        "source": pf.source, "estimated_variables": pf.estimated,
        "notes": ["Estimación de planificación (Rothermel, combustible muerto 1/10/100 h, sin vivo); no reemplaza a CONAF ni a un simulador operativo."]
                 + (["Modelo 4 sin componente vivo: la tasa puede sobrestimarse hasta ~2×."] if fuel == "matorral_denso" else [])
                 + ([terrain_note] if terrain_note else []),
    }
