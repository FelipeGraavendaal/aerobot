"""
LLM simulado con la MISMA interfaz que anthropic.AsyncAnthropic
(`client.messages.create(...)` -> objeto con .content y .stop_reason).

Permite ejercitar el bucle agéntico real (tool_use -> ejecución -> tool_result
-> respuesta final) sin API key. La "decisión" de qué herramienta usar se
basa en palabras clave; la redacción final es una plantilla con las cifras
devueltas por las herramientas.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str = field(default_factory=lambda: f"toolu_mock_{uuid.uuid4().hex[:12]}")
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class MockResponse:
    content: list[Any]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


# Algunos lugares conocidos para resolver nombres sin geocoder.
PLACES = {
    "santiago": (-33.393, -70.786), "scel": (-33.393, -70.786), "pudahuel": (-33.393, -70.786),
    "antofagasta": (-23.444, -70.445), "calama": (-22.498, -68.904), "iquique": (-20.535, -70.181),
    "la serena": (-29.916, -71.200), "concepcion": (-36.773, -73.063), "temuco": (-38.926, -72.651),
    "puerto montt": (-41.439, -73.094), "punta arenas": (-53.003, -70.855), "valparaiso": (-33.047, -71.612),
    "buenos aires": (-34.822, -58.536), "lima": (-12.022, -77.114), "mendoza": (-32.832, -68.793),
    "patagonia": (-48.5, -71.0), "miami": (25.795, -80.290), "madrid": (40.472, -3.561), "bogota": (4.702, -74.147),
}


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _resolve_location(text: str) -> tuple[float, float]:
    t = _norm(text)
    for name, coords in PLACES.items():
        if name in t:
            return coords
    m = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text.split("<contexto_mapa>")[0])
    if m:
        return float(m.group(1)), float(m.group(2))
    sel = re.search(r"punto_seleccionado_lat=(-?[\d.]+); punto_seleccionado_lon=(-?[\d.]+)", text)
    if sel:
        return float(sel.group(1)), float(sel.group(2))
    ctr = re.search(r"centro_lat=(-?[\d.]+); centro_lon=(-?[\d.]+)", text)
    if ctr:
        return float(ctr.group(1)), float(ctr.group(2))
    return PLACES["santiago"]


# Palabras clave -> id de Lugar de Interés registrado.
POI_KEYWORDS = {
    "scel": "scel", "pudahuel": "scel", "aeropuerto de santiago": "scel", "punta arenas": "scci", "scci": "scci",
    "valparaiso": "puerto-valpo", "casablanca": "casablanca", "libertadores": "los-libertadores",
    "santiago": "stgo-centro",
}


def _plan_tools(text: str) -> list[ToolUseBlock]:
    t = _norm(text.split("<contexto_mapa>")[0])
    lat, lon = _resolve_location(text)
    if re.search(r"sensor|estacion|calibr|falla|anemometro|termometro", t):
        icao = re.search(r"\b(sc[a-z]{2}|sa[a-z]{2}|k[a-z]{3})\b", t)
        args = {"sensor_id": f"metar:{icao.group(1).upper()}"} if icao else {"lat": lat, "lon": lon}
        return [ToolUseBlock("inspect_sensor", args)]
    if re.search(r"alerta|riesgo|despach|que hago|que hacer|peligro", t):
        poi_id = next((pid for kw, pid in POI_KEYWORDS.items() if kw in t), None)
        args = {"poi_id": poi_id} if poi_id else {"lat": lat, "lon": lon, "name": "Punto seleccionado"}
        return [ToolUseBlock("dispatch_risk_alert", args)]
    want_avi = re.search(r"metar|taf|aerodromo|aeropuerto|visibilidad|techo|vfr|ifr|aterriz|despeg|pista", t)
    want_radar = re.search(r"radar|lluvia|precipitac|eco|dbz|chubasc|granizo", t)
    want_models = re.search(r"modelo|gfs|ecmwf|cape|cin\b|conveccion|inestabil|pronostico|cizall", t)
    want_wind = re.search(r"viento|rafaga|wind", t)
    want_sat = re.search(r"satelite|nubes|nubosidad|infrarrojo|\bir\b", t)
    storm = re.search(r"tormenta|severo|alerta", t)

    calls: list[ToolUseBlock] = []
    if want_avi:
        calls.append(ToolUseBlock("fetch_aviation_weather", {"lat": lat, "lon": lon}))
    if want_radar or storm:
        calls.append(ToolUseBlock("analyze_radar_echoes", {"lat": lat, "lon": lon, "radius_km": 60}))
    if want_models or want_wind or storm:
        calls.append(ToolUseBlock("compare_atmospheric_models", {"lat": lat, "lon": lon, "hours_ahead": 48}))
    if not calls and not want_sat:
        # Consulta genérica: panorama completo.
        calls = [ToolUseBlock("fetch_aviation_weather", {"lat": lat, "lon": lon}),
                 ToolUseBlock("analyze_radar_echoes", {"lat": lat, "lon": lon, "radius_km": 60})]

    layer, zoom = None, 7
    if want_sat:
        layer, zoom = "satellite_ir", 5
    elif want_radar or storm:
        layer = "radar"
    elif want_wind or want_models:
        layer, zoom = "wind_particles", 6
    elif want_avi:
        layer, zoom = "radar", 9
    if layer or not calls:
        calls.append(ToolUseBlock("trigger_map_layer_update",
                                  {"layer_type": layer or "radar", "focus_lat": lat, "focus_lon": lon, "zoom_level": zoom}))
    return calls


def _fmt(v, unit="", nd=0):
    if v is None:
        return "s/d"
    return f"{v:.{nd}f}{unit}" if isinstance(v, (int, float)) else f"{v}{unit}"


def _compose(results: dict[str, dict]) -> str:
    sensor = results.get("inspect_sensor")
    if sensor:
        if "error" in sensor:
            return f"No pude validar el sensor: {sensor['error']}"
        head = f"**{sensor['sensor']['name']}** · confianza {int(sensor['confidence'] * 100)} % · modelos: {', '.join(sensor['models']) or 'ninguno'}"
        gaps = ("\n\nDatos faltantes: " + "; ".join(sensor["data_gaps"])) if sensor.get("data_gaps") else ""
        return f"{head}\n\n{sensor['interpretation']}{gaps}"
    alert = results.get("dispatch_risk_alert")
    if alert:
        if "error" in alert:
            return f"No pude evaluar el lugar: {alert['error']}"
        if alert["level"] == 0:
            out = "Sin riesgo meteorológico relevante en las próximas 24 h para ese lugar."
        else:
            out = alert["message"]
        if alert.get("data_gaps"):
            out += "\n\nDatos faltantes: " + "; ".join(alert["data_gaps"]) + "."
        return out
    lines, alerts, mock = [], [], False
    for name, r in results.items():
        if r.get("source") == "mock" or r.get("warning"):
            mock = True
        if "error" in r:
            lines.append(f"- {name}: error ({r['error']}).")

    avi = results.get("fetch_aviation_weather")
    if avi and avi.get("metar"):
        m = avi["metar"]
        gust = f" G{m['wind_gust_kt']}" if m.get("wind_gust_kt") else ""
        lines.append(
            f"- **METAR {m['station']}** ({_fmt(m['distance_km'], ' km', 0)}, {m['observed_utc']}): viento "
            f"{_fmt(m['wind_dir_deg'])}°/{_fmt(m['wind_speed_kt'])}{gust} kt, visibilidad {_fmt(m['visibility_sm'], ' SM', 1)}, "
            f"techo {_fmt(m['ceiling_ft'], ' ft')}, {_fmt(m['temperature_c'])}/{_fmt(m['dewpoint_c'])} °C -> **{m['flight_category']}**."
        )
        if m.get("significant_weather"):
            lines.append(f"  - Fenómenos: {', '.join(m['significant_weather'])}.")
        if m["flight_category"] == "LIFR" or any("TS" in w or "GR" in w for w in m.get("significant_weather", [])):
            alerts.append(f"[ALERTA] {m['station']}: condiciones {m['flight_category']} / fenómeno significativo en METAR.")

    rad = results.get("analyze_radar_echoes")
    if rad and "summary" in rad:
        s = rad["summary"]
        cell = s.get("strongest_cell")
        where = f" a {cell['distance_km']} km al {cell['direction_from_center']}" if cell else ""
        lines.append(
            f"- **Radar** (r={rad['query']['radius_km']:.0f} km): cobertura {s['coverage_pct']}%, máx. "
            f"{_fmt(s['max_dbz_est'], ' dBZ', 1)} estimado ({s['max_dbz_class']}){where}; tasa máx. {s['max_rain_rate_mm_h']} mm/h."
        )
        if (s.get("max_dbz_est") or 0) >= 50:
            alerts.append(f"[ALERTA] Núcleo de {s['max_dbz_est']} dBZ{where}.")

    mod = results.get("compare_atmospheric_models")
    if mod and "per_model" in mod:
        g, e, c = mod["per_model"]["gfs"], mod["per_model"]["ecmwf"], mod["comparison"]
        lines.append(
            f"- **GFS vs ECMWF** ({mod['query']['hours_ahead']} h): CAPE máx. {_fmt(g['cape_max_jkg'])} vs "
            f"{_fmt(e['cape_max_jkg'])} J/kg (acuerdo {c['cape_max']['agreement']}); ráfagas {_fmt(g['gust_max_kt'])} vs "
            f"{_fmt(e['gust_max_kt'])} kt; cizalladura máx. {_fmt(g['bulk_shear_10m_500hpa_max_kt'])} vs "
            f"{_fmt(e['bulk_shear_10m_500hpa_max_kt'])} kt."
        )
        if "cape_peak_timing_offset_h" in c:
            lines.append(f"  - Desfase del pico de CAPE: {c['cape_peak_timing_offset_h']:+.0f} h (GFS respecto de ECMWF).")
        for gap in mod.get("data_gaps", [])[:2]:
            lines.append(f"  - Nota: {gap}.")
        for h in mod.get("severity_hints", []):
            lines.append(f"  - {h}")
            if "supercélulas" in h or "daño" in h:
                alerts.append(f"[ALERTA] {h}")

    head = "**Análisis AeroBot (modo demo sin LLM)**"
    if mock:
        head += "\n\n> Atención: parte de los datos son SIMULADOS (API no disponible)."
        alerts = []  # regla: no alertar con datos simulados
    tail = "\n\nRecomendación: verifica tendencias en los próximos 30-60 min y contrasta con el TAF antes de decidir."
    return "\n".join([head, "", *alerts, *lines]) + tail


class _MockMessages:
    async def create(self, *, messages: list[dict], tool_choice: dict | None = None, **_: Any) -> MockResponse:
        last = messages[-1]
        # 1) Turno nuevo del usuario -> decidir herramientas
        if last["role"] == "user" and isinstance(last["content"], str):
            if tool_choice and tool_choice.get("type") == "none":
                return MockResponse([TextBlock("Sin herramientas disponibles para responder.")], "end_turn")
            calls = _plan_tools(last["content"])
            return MockResponse([TextBlock("Consultando fuentes de datos…"), *calls], "tool_use")

        # 2) Llegaron tool_results -> redactar respuesta final
        id_to_name = {}
        for msg in messages:
            if msg["role"] == "assistant":
                for b in msg["content"]:
                    if getattr(b, "type", None) == "tool_use":
                        id_to_name[b.id] = b.name
        results = {}
        for block in last["content"]:
            if block.get("type") == "tool_result":
                results[id_to_name.get(block["tool_use_id"], "?")] = json.loads(block["content"])
        return MockResponse([TextBlock(_compose(results))], "end_turn")


class MockAnthropic:
    """Sustituto de anthropic.AsyncAnthropic para desarrollo sin API key."""

    def __init__(self) -> None:
        self.messages = _MockMessages()
