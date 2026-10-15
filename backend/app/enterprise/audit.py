"""
Módulo 3 · Auditor climático (Día Caído / paralización).

Fuentes históricas libres:
  1. Sensor del cliente (lecturas entregadas en la solicitud): la única fuente
     que puede ser "minuto a minuto".
  2. METAR histórico de la estación más cercana (archivo Iowa Environmental
     Mesonet; observaciones cada 30–60 min + especiales).
  3. Reanálisis ERA5 (Open-Meteo Archive): ráfaga horaria en las coordenadas exactas.

Estadística por fuente: media del viento, media de ráfagas, desviación estándar,
P95 y máximo de ráfagas, minutos sobre el umbral (cada observación representa el
intervalo hasta la siguiente, máx. 60 min) y ventanas continuas de excedencia.
Cuando una observación no reporta ráfaga, se usa el viento medio (criterio conservador).

El informe es evidencia técnica para un peritaje: debe ser revisado y firmado por
un profesional competente; AeroBot no emite certificaciones legales.
"""
from __future__ import annotations

import csv
import io
import math
import statistics
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..services.geo import bbox_around, haversine_km, validate_coords
from ..services.http import UpstreamError, get_client, get_json

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"
AWC_STATIONS = "https://aviationweather.gov/api/data/stationinfo"
FORCE_MAJEURE = "Condiciones exceden límite operativo seguro. Paralización justificada por fuerza mayor meteorológica."
NOT_EXCEEDED = ("Las condiciones registradas no superan el límite operativo estipulado; con estos datos no se acredita "
                "fuerza mayor meteorológica.")


def percentile(values: list[float], p: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def analyze(obs: list[dict], threshold: float, start: datetime, end: datetime) -> dict | None:
    """obs: [{time, wind_kt, gust_kt}] ordenadas. Devuelve estadística y ventanas de excedencia."""
    obs = [o for o in obs if start <= o["time"] <= end and (o.get("wind_kt") is not None or o.get("gust_kt") is not None)]
    if not obs:
        return None
    obs.sort(key=lambda o: o["time"])
    peak = [max(o.get("gust_kt") or 0, o.get("wind_kt") or 0) for o in obs]
    winds = [o["wind_kt"] for o in obs if o.get("wind_kt") is not None]
    gusts = [o["gust_kt"] for o in obs if o.get("gust_kt") is not None]
    minutes_over, windows, cur = 0.0, [], None
    for k, o in enumerate(obs):
        nxt = obs[k + 1]["time"] if k + 1 < len(obs) else min(end, o["time"] + timedelta(minutes=60))
        dur = max(0.0, min((nxt - o["time"]).total_seconds() / 60, 60))
        if peak[k] >= threshold:
            minutes_over += dur
            if cur is None:
                cur = {"start": o["time"], "end": o["time"] + timedelta(minutes=dur), "max_kt": peak[k]}
            else:
                cur["end"], cur["max_kt"] = o["time"] + timedelta(minutes=dur), max(cur["max_kt"], peak[k])
        elif cur:
            windows.append(cur)
            cur = None
    if cur:
        windows.append(cur)
    span = (end - start).total_seconds() / 60
    return {
        "n_obs": len(obs), "first": obs[0]["time"].isoformat(), "last": obs[-1]["time"].isoformat(),
        "mean_wind_kt": round(statistics.fmean(winds), 1) if winds else None,
        "std_wind_kt": round(statistics.pstdev(winds), 1) if len(winds) > 1 else None,
        "mean_gust_kt": round(statistics.fmean(gusts), 1) if gusts else None,
        "gusts_reported": len(gusts),
        "p95_peak_kt": round(percentile(peak, 95), 1), "max_peak_kt": round(max(peak), 1),
        "std_peak_kt": round(statistics.pstdev(peak), 1) if len(peak) > 1 else 0.0,
        "minutes_over_threshold": round(minutes_over), "pct_shift_over_threshold": round(100 * minutes_over / span, 1) if span else 0,
        "exceeded": max(peak) >= threshold,
        "windows": [{"start": w["start"].isoformat(), "end": w["end"].isoformat(), "max_kt": w["max_kt"]} for w in windows],
    }


async def nearest_station(lat: float, lon: float, radius_km: float = 60) -> dict | None:
    s, w, n, e = bbox_around(lat, lon, radius_km)
    data = await get_json(AWC_STATIONS, params={"bbox": f"{s:.3f},{w:.3f},{n:.3f},{e:.3f}", "format": "json"}, ttl=86400)
    cands = [d for d in (data or []) if d.get("icaoId") and d.get("lat") is not None and "METAR" in (d.get("siteType") or ["METAR"])]
    if not cands:
        return None
    best = min(cands, key=lambda d: haversine_km(lat, lon, d["lat"], d["lon"]))
    return {"icao": best["icaoId"], "name": best.get("site") or best.get("name"), "lat": best["lat"], "lon": best["lon"],
            "distance_km": round(haversine_km(lat, lon, best["lat"], best["lon"]), 1)}


async def iem_metar(icao: str, start: datetime, end: datetime) -> list[dict]:
    d0, d1 = start - timedelta(hours=1), end + timedelta(hours=1)
    params = [("station", icao), ("data", "sknt"), ("data", "gust"), ("data", "drct"),
              ("year1", d0.year), ("month1", d0.month), ("day1", d0.day), ("hour1", d0.hour),
              ("year2", d1.year), ("month2", d1.month), ("day2", d1.day), ("hour2", d1.hour),
              ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"), ("missing", "null"),
              ("report_type", "3"), ("report_type", "4")]
    resp = await get_client().get(IEM_URL, params=params, timeout=40)
    if resp.status_code != 200:
        raise UpstreamError(f"IEM {resp.status_code}")
    rows = []
    for r in csv.DictReader(io.StringIO(resp.text)):
        t = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        f = lambda v: None if v in (None, "", "null", "M") else float(v)  # noqa: E731
        rows.append({"time": t, "wind_kt": f(r.get("sknt")), "gust_kt": f(r.get("gust")), "dir": f(r.get("drct"))})
    return rows


async def era5(lat: float, lon: float, day: date) -> list[dict]:
    d = await get_json(ERA5_URL, ttl=86400, params={
        "latitude": lat, "longitude": lon, "start_date": day.isoformat(), "end_date": (day + timedelta(days=1)).isoformat(),
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m", "wind_speed_unit": "kn", "timezone": "UTC"})
    h = d.get("hourly") or {}
    return [{"time": datetime.fromisoformat(t).replace(tzinfo=timezone.utc), "wind_kt": w, "gust_kt": g}
            for t, w, g in zip(h.get("time", []), h.get("wind_speed_10m", []), h.get("wind_gusts_10m", []))]


async def audit_historical_weather(lat: float, lon: float, date_str: str, threshold_knots: float = 38,
                                   start_hour: int = 0, end_hour: int = 24, timezone_name: str = "America/Santiago",
                                   station: str | None = None, client_readings: list[dict] | None = None,
                                   site_name: str | None = None, contract_ref: str | None = None) -> dict:
    lat, lon = validate_coords(lat, lon)
    day = date.fromisoformat(date_str)
    if day > datetime.now(timezone.utc).date():
        raise ValueError("La fecha auditada no puede ser futura")
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(day, time(max(0, min(start_hour, 23))), tz).astimezone(timezone.utc)
    end = (datetime.combine(day, time(0), tz) + timedelta(hours=max(1, min(end_hour, 24)))).astimezone(timezone.utc)
    sources, gaps, raw = {}, [], {}

    if client_readings:
        cr = [{"time": (datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00"))).astimezone(timezone.utc),
               "wind_kt": r.get("wind_kt"), "gust_kt": r.get("gust_kt")} for r in client_readings]
        res = analyze(cr, threshold_knots, start, end)
        if res:
            sources["sensor_cliente"] = {"label": "Sensor del cliente en faena", "distance_km": 0.0, **res}
            raw["sensor_cliente"] = cr

    st = None
    try:
        st = {"icao": station.upper(), "name": station.upper(), "distance_km": None} if station else await nearest_station(lat, lon)
    except UpstreamError as exc:
        gaps.append(f"catálogo de estaciones no disponible ({exc})")
    if st:
        try:
            m = await iem_metar(st["icao"], start, end)
            res = analyze(m, threshold_knots, start, end)
            if res:
                sources["metar"] = {"label": f"METAR {st['icao']} ({st.get('name') or ''})", "distance_km": st.get("distance_km"), **res}
                raw["metar"] = [o for o in m if start <= o["time"] <= end]
            else:
                gaps.append(f"sin METAR de {st['icao']} en la ventana")
        except (UpstreamError, Exception) as exc:
            gaps.append(f"archivo METAR no disponible ({type(exc).__name__})")
    else:
        gaps.append("sin estación METAR a ≤60 km")

    try:
        e = await era5(lat, lon, day)
        res = analyze(e, threshold_knots, start, end)
        if res:
            sources["era5"] = {"label": "Reanálisis ERA5 (horario, celda ~25 km)", "distance_km": 0.0, **res}
            raw["era5"] = [o for o in e if start <= o["time"] <= end]
    except UpstreamError as exc:
        gaps.append(f"ERA5 no disponible ({str(exc)[:60]})")

    if not sources:
        raise ValueError("Sin datos históricos para la ventana: " + "; ".join(gaps))

    # Fuente primaria: sensor del cliente > METAR (≤25 km) > ERA5.
    primary_key = "sensor_cliente" if "sensor_cliente" in sources else \
        ("metar" if "metar" in sources and (sources["metar"]["distance_km"] or 0) <= 25 else
         ("era5" if "era5" in sources else "metar"))
    primary = sources[primary_key]
    exceeded = primary["exceeded"]
    corroborated = [k for k, v in sources.items() if k != primary_key and v["exceeded"]]

    loc = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%H:%M")  # noqa: E731
    lines = [
        "INFORME TÉCNICO METEOROLÓGICO — EVALUACIÓN DE LÍMITE OPERATIVO POR VIENTO",
        f"Referencia: {contract_ref or 'sin referencia contractual'} · Faena: {site_name or 'sin nombre'}",
        f"Coordenadas: {lat:.5f}, {lon:.5f} · Fecha: {day.strftime('%d-%m-%Y')} · Ventana: "
        f"{start.astimezone(tz).strftime('%H:%M')}–{end.astimezone(tz).strftime('%H:%M')} ({timezone_name})",
        f"Norma evaluada: ráfaga ≥ {threshold_knots:.0f} kt ({threshold_knots * 1.852:.0f} km/h).",
        "",
        "1. METODOLOGÍA",
        "Se analizaron las series disponibles calculando media y desviación estándar del viento, media, percentil 95 "
        "(P95) y máximo de ráfagas, y la duración acumulada sobre el umbral (cada observación representa el intervalo "
        "hasta la siguiente, máx. 60 min). Sin ráfaga reportada se usa el viento medio.",
        "",
        "2. RESULTADOS POR FUENTE",
    ]
    for k, v in sources.items():
        dist = f" a {v['distance_km']} km" if v.get("distance_km") else ""
        gust_txt = f"ráfaga media {v['mean_gust_kt']} kt ({v['gusts_reported']} reportes)" if v["mean_gust_kt"] is not None \
            else "sin ráfagas reportadas (se usa viento medio)"
        lines.append(f"- {v['label']}{dist}: {v['n_obs']} obs.; viento medio {v['mean_wind_kt']} kt (σ {v['std_wind_kt']}); "
                     f"{gust_txt}; P95 {v['p95_peak_kt']} kt; máximo {v['max_peak_kt']} kt; "
                     f"{v['minutes_over_threshold']} min sobre el umbral ({v['pct_shift_over_threshold']} % de la ventana).")
        for w in v["windows"][:6]:
            lines.append(f"    Excedencia {loc(w['start'])}–{loc(w['end'])} h, máx. {w['max_kt']:.0f} kt")
    lines += ["", "3. CONCLUSIÓN",
              f"Fuente primaria: {primary['label']}." + (f" Corroborado por: {', '.join(sources[c]['label'] for c in corroborated)}." if corroborated else ""),
              FORCE_MAJEURE if exceeded else NOT_EXCEEDED]
    if gaps:
        lines += ["", "Limitaciones de datos: " + "; ".join(gaps) + "."]
    lines += ["", "Nota: informe técnico generado por AeroBot a partir de datos públicos y del cliente. Para efectos "
              "legales o de seguros debe ser revisado y firmado por un profesional competente."]

    audit_id = uuid.uuid4().hex[:10]
    return {
        "audit_id": audit_id, "exceeded": exceeded, "primary_source": primary_key, "corroborated_by": corroborated,
        "threshold_knots": threshold_knots, "window_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "coordinates": {"lat": lat, "lon": lon}, "station": st, "sources": sources, "data_gaps": gaps,
        "report_text": "\n".join(lines),
        "raw_data": {k: [{"time": o["time"].isoformat(), "wind_kt": o.get("wind_kt"), "gust_kt": o.get("gust_kt")} for o in v]
                     for k, v in raw.items()},
        "pdf_payload": {"title": lines[0], "body": lines[1:], "audit_id": audit_id,
                        "generated_at": datetime.now(timezone.utc).isoformat()},
    }
