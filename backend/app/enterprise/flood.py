"""
Módulo 2b · Escorrentía por isoterma alta (método racional modificado).

    Q = 0,278 · C · i · A_eff        [m³/s]  (i en mm/h, A en km²)

• Isoterma 0 °C horaria del modelo (freezing_level_height).
• Si H_faena < H_isoterma, la precipitación cae LÍQUIDA sobre la faena; la
  fracción de cuenca que aporta agua es la que queda bajo la isoterma
  (hipsometría lineal entre H_faena y la cota máxima de la cuenca).
• "Modificado": si la isoterma sube ≥500 m en 24 h (lluvia sobre nieve o suelo
  congelado), el coeficiente de escorrentía se incrementa en +0,2 (tope 0,95).
• i = intensidad media máxima en una ventana igual al tiempo de concentración Tc.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..services.geo import validate_coords
from .fire import slope_aspect
from .forecast import point_forecast


async def flood_runoff(lat: float, lon: float, catchment_area_km2: float, catchment_top_m: float | None = None,
                       runoff_coeff: float = 0.5, tc_hours: int = 1, channel_capacity_m3s: float | None = None,
                       site_elevation_m: float | None = None, hours: int = 48) -> dict:
    lat, lon = validate_coords(lat, lon)
    if catchment_area_km2 <= 0:
        raise ValueError("El área de la cuenca debe ser mayor que 0 km²")
    pf = await point_forecast(lat, lon, hours=hours)
    h_site = site_elevation_m
    if h_site is None:
        try:
            h_site = (await slope_aspect(lat, lon))["elevation_m"]
        except Exception:
            h_site = pf.elevation_m
    fl, pr = pf.series.get("freezing_level_height"), pf.series.get("precipitation")
    if not fl or not pr:
        raise ValueError(f"La fuente {pf.source} no entrega isoterma 0 °C o precipitación")

    now = datetime.now(timezone.utc)
    i0 = min(range(len(pf.times)), key=lambda k: abs((pf.times[k] - now).total_seconds()))
    tc = max(1, int(tc_hours))
    rows = []
    for i in range(i0, len(pf.times) - tc + 1):
        window = [p or 0 for p in pr[i:i + tc]]
        intensity = sum(window) / tc
        iso = fl[i]
        if iso is None:
            continue
        rise = iso - min(x for x in fl[max(0, i - 24):i + 1] if x is not None)
        c_eff = min(0.95, runoff_coeff + (0.2 if rise >= 500 and intensity > 0 else 0))
        if iso <= h_site:
            frac = 0.0
        elif catchment_top_m and catchment_top_m > h_site:
            frac = min(1.0, (iso - h_site) / (catchment_top_m - h_site))
        else:
            frac = 1.0
        q = 0.278 * c_eff * intensity * catchment_area_km2 * frac
        rows.append({"time": pf.times[i].isoformat(), "freezing_level_m": iso, "intensity_mm_h": round(intensity, 2),
                     "liquid_on_site": iso > h_site, "contributing_fraction": round(frac, 2), "c_eff": round(c_eff, 2),
                     "isotherm_rise_24h_m": round(rise), "q_m3s": round(q, 2)})
    if not rows:
        raise ValueError("Sin datos horarios de isoterma")
    peak = max(rows, key=lambda r: r["q_m3s"])
    high_iso = peak["freezing_level_m"] >= (catchment_top_m or h_site + 1500)
    critical = (channel_capacity_m3s is not None and peak["q_m3s"] >= channel_capacity_m3s) or \
               (channel_capacity_m3s is None and high_iso and peak["intensity_mm_h"] >= 2)
    alert = None
    if critical:
        alert = (f"[ALERTA] Escorrentía Crítica por Isoterma Alta · isoterma {peak['freezing_level_m']:.0f} m sobre faena a "
                 f"{h_site:.0f} m · Q pico {peak['q_m3s']:.1f} m³/s" +
                 (f" ≥ capacidad {channel_capacity_m3s:.1f} m³/s" if channel_capacity_m3s is not None else "") +
                 f" · {datetime.fromisoformat(peak['time']).strftime('%d-%m %H:%M')} UTC")
    return {
        "site_elevation_m": h_site, "catchment": {"area_km2": catchment_area_km2, "top_m": catchment_top_m,
                                                  "runoff_coeff": runoff_coeff, "tc_hours": tc,
                                                  "channel_capacity_m3s": channel_capacity_m3s},
        "peak": peak, "critical": critical, "alert": alert,
        "total_precip_mm": round(sum(p or 0 for p in pr[i0:]), 1),
        "hourly": rows[:48], "formula": "Q = 0,278·C·i·A_eff (m³/s)", "source": pf.source,
        "notes": ["Hipsometría lineal y C uniforme: usa el área y la cota máxima reales de tu cuenca para mayor precisión."],
    }
