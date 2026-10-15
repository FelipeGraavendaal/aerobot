"""
Herramientas (Function Calling) expuestas al LLM.

- TOOL_DEFINITIONS: esquemas JSON en formato Anthropic Messages API.
- execute_tool():   despachador que valida, ejecuta y mide cada herramienta.

`trigger_map_layer_update` no consulta datos: registra un comando de mapa en
el ToolContext y (si hay WebSocket) lo emite de inmediato al frontend.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..services.aviation import fetch_aviation_weather
from ..services.geo import validate_coords
from ..services.models import compare_atmospheric_models
from ..services.radar import analyze_radar_echoes

log = logging.getLogger("aerobot.tools")

MAP_LAYERS = ["radar", "wind_particles", "satellite_ir", "none"]

EventCallback = Callable[[dict], Awaitable[None]]

_LATLON = {
    "lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "Latitud en grados decimales (sur negativo)."},
    "lon": {"type": "number", "minimum": -180, "maximum": 180, "description": "Longitud en grados decimales (oeste negativo)."},
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "fetch_aviation_weather",
        "description": (
            "Obtiene el METAR más reciente y el TAF de la estación aeronáutica más cercana al punto. "
            "Devuelve el METAR crudo y decodificado (viento, ráfagas, visibilidad en SM, techo en ft, "
            "temperatura/rocío, QNH, categoría de vuelo VFR/MVFR/IFR/LIFR y fenómenos significativos)."
        ),
        "input_schema": {"type": "object", "properties": {**_LATLON}, "required": ["lat", "lon"]},
    },
    {
        "name": "analyze_radar_echoes",
        "description": (
            "Analiza los ecos de precipitación en un radio alrededor de un punto: reflectividad máxima "
            "estimada (dBZ), cobertura de ecos (%), tasa de lluvia (mm/h), ubicación y rumbo de la celda "
            "más intensa y puntos con tormenta eléctrica. Incluye el último frame de radar RainViewer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {**_LATLON, "radius_km": {"type": "number", "minimum": 5, "maximum": 300, "default": 50,
                                                     "description": "Radio de análisis en km."}},
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "compare_atmospheric_models",
        "description": (
            "Compara los modelos GFS y ECMWF para un punto y un horizonte de pronóstico: CAPE, CIN, "
            "Lifted Index, ráfagas, viento en 850/500 hPa, cizalladura bulk 10 m-500 hPa y precipitación. "
            "Devuelve estadísticas por modelo, nivel de acuerdo, desfase temporal del pico de CAPE, "
            "indicios de severidad y una serie temporal resumida."
        ),
        "input_schema": {
            "type": "object",
            "properties": {**_LATLON, "hours_ahead": {"type": "integer", "minimum": 3, "maximum": 240, "default": 48,
                                                       "description": "Horizonte de pronóstico en horas."}},
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "trigger_map_layer_update",
        "description": (
            "Controla el mapa del usuario: cambia la capa visible y centra la vista. No devuelve datos "
            "meteorológicos. Capas: 'radar' (precipitación animada RainViewer), 'wind_particles' (partículas "
            "de viento 10 m), 'satellite_ir' (infrarrojo GOES banda 13), 'none' (solo mapa base). "
            "Úsala para mostrar visualmente lo que explicas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_type": {"type": "string", "enum": MAP_LAYERS},
                "focus_lat": _LATLON["lat"],
                "focus_lon": _LATLON["lon"],
                "zoom_level": {"type": "number", "minimum": 2, "maximum": 12, "description": "Zoom del mapa (2 global - 12 local)."},
                "annotation": {"type": "string", "maxLength": 140,
                               "description": "Texto opcional para un marcador en el punto de foco (p. ej. 'Celda 52 dBZ')."},
                "overlay_id": {"type": "string", "description": "Id de una geometría calculada por otra herramienta (p. ej. el cono de `run_fire_spread_vector`) para dibujarla."},
            },
            "required": ["layer_type", "focus_lat", "focus_lon", "zoom_level"],
        },
    },
    {
        "name": "dispatch_risk_alert",
        "description": (
            "Despachador de riesgos: evalúa con datos reales (METAR, radar, pronóstico 24 h) un Lugar de "
            "Interés y devuelve la directriz de 4 líneas [NIVEL] [LUGAR] [TIEMPO] [ACCIÓN] calculada por un "
            "motor de reglas determinista. Usa `poi_id` si el lugar ya está registrado (scel, stgo-centro, "
            "puerto-valpo, casablanca, los-libertadores, scci) o lat/lon + nombre + tipo para un lugar ad hoc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "poi_id": {"type": "string", "description": "Id de un Lugar de Interés registrado."},
                **_LATLON,
                "name": {"type": "string", "maxLength": 80, "description": "Nombre del lugar ad hoc."},
                "poi_type": {"type": "string", "enum": ["aeropuerto", "puerto", "obra", "evento", "ruta", "agricola", "urbano"]},
            },
        },
    },
]

TOOL_DEFINITIONS.append({
    "name": "inspect_sensor",
    "description": (
        "Valida un sensor meteorológico (METAR, openSenseMap o sensor propio) contra el consenso de hasta 8 "
        "modelos (GFS, ECMWF, ICON, GEM, JMA, Météo-France, UKMO, MET Norway) y sus vecinos: detecta fallas "
        "(valor pegado, salto, fuera de rango) o fenómenos locales que los modelos no captan, entrega una "
        "predicción 0-12 h corregida por el sesgo observado y una interpretación en 4 líneas. Usa `sensor_id` "
        "(p. ej. 'metar:SCEL') o lat/lon para el sensor más cercano."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"sensor_id": {"type": "string", "description": "Id del sensor, p. ej. 'metar:SCEL'."}, **_LATLON},
    },
})

_ENTERPRISE_TOOLS = [
    {
        "name": "calculate_flight_envelope",
        "description": (
            "Módulo 1 Go/No-Go UAV. Perfil vertical de viento 10/50/80/120 m AGL, cizalladura vectorial "
            "β = |ΔV⃗|/ΔZ bajo 100 m, ráfagas, visibilidad y METAR cercano. Devuelve `verdict` "
            "([VENTANA APROBADA]/[VENTANA RECHAZADA]) con los cálculos y la próxima ventana bajo el umbral."
        ),
        "input_schema": {"type": "object", "properties": {
            **_LATLON,
            "max_wind_knots": {"type": "number", "minimum": 5, "maximum": 60, "default": 25},
            "max_agl_meters": {"type": "number", "minimum": 10, "maximum": 120, "default": 120},
            "hours": {"type": "integer", "minimum": 3, "maximum": 72, "default": 24},
        }, "required": ["lat", "lon"]},
    },
    {
        "name": "run_fire_spread_vector",
        "description": (
            "Módulo 2 incendios. Rothermel con pendiente del DEM y humedad del combustible (T/HR ≈ VPD): tasa de "
            "avance, vector de máximo avance, regla 30-30-30 y cono GeoJSON a 1 y 3 h. Devuelve `overlay_id` para "
            "dibujarlo con trigger_map_layer_update (usa `suggested_zoom`). humidity/wind_dir/wind_speed (kt) reemplazan al modelo si se conocen."
        ),
        "input_schema": {"type": "object", "properties": {
            **_LATLON,
            "humidity": {"type": "number", "minimum": 1, "maximum": 100},
            "wind_dir": {"type": "number", "minimum": 0, "maximum": 360, "description": "Dirección DESDE la que sopla (°)."},
            "wind_speed": {"type": "number", "minimum": 0, "maximum": 100, "description": "Viento a 10 m en kt."},
            "fuel": {"type": "string", "enum": ["pastizal", "pastizal_alto", "matorral", "matorral_denso", "hojarasca"], "default": "pastizal"},
        }, "required": ["lat", "lon"]},
    },
    {
        "name": "calculate_runoff_isotherm",
        "description": (
            "Módulo 2 aluviones. Isoterma 0 °C vs cota de la faena y método racional modificado Q = 0,278·C·i·A_eff "
            "para las próximas 48 h. Emite 'Escorrentía Crítica por Isoterma Alta' si corresponde."
        ),
        "input_schema": {"type": "object", "properties": {
            **_LATLON,
            "catchment_area_km2": {"type": "number", "exclusiveMinimum": 0},
            "catchment_top_m": {"type": "number"},
            "runoff_coeff": {"type": "number", "minimum": 0.05, "maximum": 0.95, "default": 0.5},
            "channel_capacity_m3s": {"type": "number"},
        }, "required": ["lat", "lon", "catchment_area_km2"]},
    },
    {
        "name": "audit_historical_weather",
        "description": (
            "Módulo 3 auditor. Para una fecha pasada, analiza METAR histórico de la estación más cercana, ERA5 y "
            "lecturas del cliente: media, σ, P95 y máximo de ráfagas, minutos y ventanas sobre el umbral. Devuelve "
            "`exceeded` (bool), `report_text` pericial, `raw_data` y `audit_id` (PDF en /api/audit/{audit_id}.pdf)."
        ),
        "input_schema": {"type": "object", "properties": {
            **_LATLON,
            "date": {"type": "string", "description": "AAAA-MM-DD"},
            "threshold_knots": {"type": "number", "minimum": 5, "maximum": 100, "default": 38},
            "start_hour": {"type": "integer", "minimum": 0, "maximum": 23, "default": 0},
            "end_hour": {"type": "integer", "minimum": 1, "maximum": 24, "default": 24},
            "station": {"type": "string", "description": "ICAO opcional (p. ej. SCEL)."},
        }, "required": ["lat", "lon", "date"]},
    },
    {
        "name": "evaluate_supply_chain_risk",
        "description": (
            "Módulo 4. Revisa las próximas 72 h de los Lugares de Interés (heladas por radiación, viento ≥38 kt, "
            "lluvia o nieve intensas) y ejecuta el protocolo de mitigación con la política de límites del cliente. "
            "Devuelve el mensaje 'Aviso de impacto inminente…' y el estado de cada acción."
        ),
        "input_schema": {"type": "object", "properties": {
            "client_id": {"type": "string", "default": "demo"},
            "simulate": {"type": "boolean", "default": False, "description": "true = simulacro de helada en Casablanca."},
        }},
    },
    {
        "name": "trigger_supply_chain_webhook",
        "description": (
            "Dispara la API logística del cliente (compra, IoT o aviso a proveedor). Solo se ejecuta sola si "
            "proviene de un evento de riesgo alto (`event_id`) y el monto está bajo el tope del cliente; si no, "
            "queda pendiente de aprobación humana."
        ),
        "input_schema": {"type": "object", "properties": {
            "client_id": {"type": "string"},
            "action": {"type": "string", "enum": ["pre_aprobar_compra", "activar_iot", "alertar_proveedor"]},
            "item": {"type": "string", "maxLength": 200},
            "amount_clp": {"type": "integer", "minimum": 0, "default": 0},
            "event_id": {"type": "string"},
            "reason": {"type": "string", "maxLength": 300},
        }, "required": ["client_id", "action", "item"]},
    },
]
TOOL_DEFINITIONS.extend(_ENTERPRISE_TOOLS)

# Los inyecta main.py al arrancar (evita importaciones circulares).
MITIGATION_SERVICE = None
AUDITS: dict[str, dict] = {}
ALERT_SERVICE = None
SENSOR_SERVICE = None


@dataclass
class ToolContext:
    """Estado compartido de una ejecución del agente (un turno de chat)."""
    map_commands: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    on_event: EventCallback | None = None

    async def emit(self, event: dict) -> None:
        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:  # un cliente desconectado no debe romper el agente
                log.debug("No se pudo emitir evento %s", event.get("type"), exc_info=True)


OVERLAYS: dict[str, dict] = {}   # overlay_id -> GeoJSON (últimos 50)


def store_overlay(geojson: dict) -> str:
    oid = uuid.uuid4().hex[:8]
    OVERLAYS[oid] = geojson
    while len(OVERLAYS) > 50:
        OVERLAYS.pop(next(iter(OVERLAYS)))
    return oid


async def trigger_map_layer_update(ctx: ToolContext, layer_type: str, focus_lat: float, focus_lon: float,
                                   zoom_level: float, annotation: str | None = None, overlay_id: str | None = None) -> dict:
    if overlay_id and overlay_id not in OVERLAYS:
        raise ValueError(f"overlay_id '{overlay_id}' no existe")
    if layer_type not in MAP_LAYERS:
        raise ValueError(f"layer_type inválido '{layer_type}'. Opciones: {MAP_LAYERS}")
    lat, lon = validate_coords(focus_lat, focus_lon)
    command = {
        "id": uuid.uuid4().hex[:8],
        "type": "trigger_map_layer_update",
        "layer_type": layer_type,
        "focus": {"lat": round(lat, 4), "lon": round(lon, 4)},
        "zoom_level": max(2.0, min(float(zoom_level), 12.0)),
        "annotation": (annotation or "")[:140] or None,
        "overlay": OVERLAYS.get(overlay_id) if overlay_id else None,
    }
    ctx.map_commands.append(command)
    await ctx.emit({"type": "map_command", "command": command})
    return {"status": "ok", "message": f"Mapa actualizado a la capa '{layer_type}' en ({lat:.2f}, {lon:.2f}) zoom {command['zoom_level']}."}


async def dispatch_risk_alert(poi_id: str | None = None, lat: float | None = None, lon: float | None = None,
                              name: str | None = None, poi_type: str = "urbano") -> dict:
    if ALERT_SERVICE is None:
        raise ValueError("Servicio de alertas no inicializado")
    from ..alerts.pois import POI

    if poi_id:
        poi = ALERT_SERVICE.store.get(poi_id)
        if poi is None:
            raise ValueError(f"No existe el Lugar de Interés '{poi_id}'")
    elif lat is not None and lon is not None:
        poi = POI("ad-hoc", (name or f"Punto {lat:.3f}, {lon:.3f}")[:80], lat, lon, poi_type or "urbano", 30)
    else:
        raise ValueError("Indica poi_id o lat/lon")
    alert = await ALERT_SERVICE.evaluate_live(poi)
    return {"message": alert["message"], "level": alert["level"], "dispatcher": alert["dispatcher"],
            "hazards": alert["hazards"], "data_gaps": alert["data_gaps"], "sources": alert["sources"],
            "map_hint": alert["map_hint"], "source": "alertas"}


async def inspect_sensor(sensor_id: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
    if SENSOR_SERVICE is None:
        raise ValueError("Servicio de sensores no inicializado")
    if not sensor_id:
        if lat is None or lon is None:
            raise ValueError("Indica sensor_id o lat/lon")
        net = await SENSOR_SERVICE.network(lat - 0.5, lon - 0.6, lat + 0.5, lon + 0.6)
        if not net["sensors"]:
            return {"error": "No hay sensores con datos frescos a ~50 km", "data_gaps": net["data_gaps"], "source": "sensores"}
        from ..services.geo import haversine_km
        sensor_id = min(net["sensors"], key=lambda x: haversine_km(lat, lon, x["sensor"]["lat"], x["sensor"]["lon"]))["sensor"]["id"]
    try:
        r = await SENSOR_SERVICE.interpretation(sensor_id)
    except KeyError:
        raise ValueError(f"Sensor '{sensor_id}' no encontrado; prueba con lat/lon")
    pred = {v: [x for x in p["series"] if x["h"] in (0, 3, 6, 12)] for v, p in r["prediction"].items()}
    return {"sensor": r["sensor"], "status": r["status"], "confidence": r["confidence"],
            "interpretation": r["interpretation"], "interpreter": r["interpreter"],
            "checks": [{k: c[k] for k in ("variable", "observed", "consensus_median", "spread", "z", "verdict", "reason")}
                       for c in r["checks"]],
            "models": r["consensus_sources"], "prediction": pred, "data_gaps": r.get("data_gaps", []), "source": "sensores"}


async def calculate_flight_envelope(lat: float, lon: float, max_wind_knots: float = 25, max_agl_meters: float = 120,
                                    hours: int = 24) -> dict:
    from ..enterprise.uav import go_no_go
    r = await go_no_go(lat, lon, hours=hours, max_wind_kt=max_wind_knots, max_agl_m=max_agl_meters)
    return {**r, "source": r["source"]}


async def run_fire_spread_vector(lat: float, lon: float, humidity: float | None = None, wind_dir: float | None = None,
                                 wind_speed: float | None = None, fuel: str = "pastizal") -> dict:
    from ..enterprise.fire import fire_spread
    r = await fire_spread(lat, lon, fuel=fuel, hours=[1, 3], humidity=humidity, wind_dir=wind_dir, wind_speed_kt=wind_speed)
    r["overlay_id"] = store_overlay(r["cone"])
    r["cone_center"] = [lat, lon]
    r.pop("cone")
    return r


async def calculate_runoff_isotherm(lat: float, lon: float, catchment_area_km2: float, catchment_top_m: float | None = None,
                                    runoff_coeff: float = 0.5, channel_capacity_m3s: float | None = None) -> dict:
    from ..enterprise.flood import flood_runoff
    r = await flood_runoff(lat, lon, catchment_area_km2, catchment_top_m, runoff_coeff,
                           channel_capacity_m3s=channel_capacity_m3s)
    r["hourly"] = r["hourly"][:24:3]
    return r


async def audit_historical_weather(lat: float, lon: float, date: str, threshold_knots: float = 38, start_hour: int = 0,
                                   end_hour: int = 24, station: str | None = None) -> dict:
    from ..enterprise.audit import audit_historical_weather as run_audit
    r = await run_audit(lat, lon, date, threshold_knots, start_hour, end_hour, station=station)
    AUDITS[r["audit_id"]] = r
    while len(AUDITS) > 100:
        AUDITS.pop(next(iter(AUDITS)))
    out = {k: v for k, v in r.items() if k != "raw_data"}
    out["raw_data_points"] = {k: len(v) for k, v in r["raw_data"].items()}
    out["pdf_url"] = f"/api/audit/{r['audit_id']}.pdf"
    out["source"] = "auditoría"
    return out


async def evaluate_supply_chain_risk(client_id: str = "demo", simulate: bool = False) -> dict:
    if MITIGATION_SERVICE is None:
        raise ValueError("Servicio de mitigación no inicializado")
    if simulate:
        return {"results": [await MITIGATION_SERVICE.simulate_frost(client_id=client_id)], "source": "simulacro"}
    res = await MITIGATION_SERVICE.scan(client_id)
    return {"results": res, "events_detected": sum(1 for r in res if "event" in r), "source": "mitigación"}


async def trigger_supply_chain_webhook(client_id: str, action: str, item: str, amount_clp: int = 0,
                                       event_id: str | None = None, reason: str | None = None) -> dict:
    if MITIGATION_SERVICE is None:
        raise ValueError("Servicio de mitigación no inicializado")
    r = await MITIGATION_SERVICE.trigger(client_id, action, item, amount_clp, event_id=event_id, reason=reason)
    return {**r, "source": "webhook"}


# Herramientas de datos: nombre -> (función, argumentos permitidos)
_DATA_TOOLS: dict[str, tuple[Callable[..., Awaitable[dict]], set[str]]] = {
    "fetch_aviation_weather": (fetch_aviation_weather, {"lat", "lon"}),
    "analyze_radar_echoes": (analyze_radar_echoes, {"lat", "lon", "radius_km"}),
    "compare_atmospheric_models": (compare_atmospheric_models, {"lat", "lon", "hours_ahead"}),
    "dispatch_risk_alert": (dispatch_risk_alert, {"poi_id", "lat", "lon", "name", "poi_type"}),
    "inspect_sensor": (inspect_sensor, {"sensor_id", "lat", "lon"}),
    "calculate_flight_envelope": (calculate_flight_envelope, {"lat", "lon", "max_wind_knots", "max_agl_meters", "hours"}),
    "run_fire_spread_vector": (run_fire_spread_vector, {"lat", "lon", "humidity", "wind_dir", "wind_speed", "fuel"}),
    "calculate_runoff_isotherm": (calculate_runoff_isotherm, {"lat", "lon", "catchment_area_km2", "catchment_top_m",
                                                              "runoff_coeff", "channel_capacity_m3s"}),
    "audit_historical_weather": (audit_historical_weather, {"lat", "lon", "date", "threshold_knots", "start_hour", "end_hour", "station"}),
    "evaluate_supply_chain_risk": (evaluate_supply_chain_risk, {"client_id", "simulate"}),
    "trigger_supply_chain_webhook": (trigger_supply_chain_webhook, {"client_id", "action", "item", "amount_clp", "event_id", "reason"}),
}


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> tuple[dict, bool]:
    """Ejecuta una herramienta. Devuelve (resultado, is_error). Nunca lanza."""
    started = time.perf_counter()
    await ctx.emit({"type": "tool_start", "name": name, "input": args})
    is_error = False
    try:
        if name == "trigger_map_layer_update":
            allowed = {"layer_type", "focus_lat", "focus_lon", "zoom_level", "annotation", "overlay_id"}
            result = await trigger_map_layer_update(ctx, **{k: v for k, v in args.items() if k in allowed})
        elif name in _DATA_TOOLS:
            fn, allowed = _DATA_TOOLS[name]
            result = await fn(**{k: v for k, v in args.items() if k in allowed})
        else:
            raise ValueError(f"Herramienta desconocida: {name}")
    except (TypeError, ValueError) as exc:  # argumentos inválidos: el LLM puede corregir
        result, is_error = {"error": f"Argumentos inválidos: {exc}"}, True
    except Exception as exc:  # error de API externa u otro
        log.exception("Error ejecutando %s", name)
        result, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True

    duration_ms = round((time.perf_counter() - started) * 1000)
    entry = {"name": name, "input": args, "duration_ms": duration_ms, "is_error": is_error,
             "source": result.get("source") if isinstance(result, dict) else None}
    ctx.trace.append(entry)
    await ctx.emit({"type": "tool_end", **entry})
    return result, is_error
