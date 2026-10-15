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
            },
            "required": ["layer_type", "focus_lat", "focus_lon", "zoom_level"],
        },
    },
]


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


async def trigger_map_layer_update(ctx: ToolContext, layer_type: str, focus_lat: float, focus_lon: float,
                                   zoom_level: float, annotation: str | None = None) -> dict:
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
    }
    ctx.map_commands.append(command)
    await ctx.emit({"type": "map_command", "command": command})
    return {"status": "ok", "message": f"Mapa actualizado a la capa '{layer_type}' en ({lat:.2f}, {lon:.2f}) zoom {command['zoom_level']}."}


# Herramientas de datos: nombre -> (función, argumentos permitidos)
_DATA_TOOLS: dict[str, tuple[Callable[..., Awaitable[dict]], set[str]]] = {
    "fetch_aviation_weather": (fetch_aviation_weather, {"lat", "lon"}),
    "analyze_radar_echoes": (analyze_radar_echoes, {"lat", "lon", "radius_km"}),
    "compare_atmospheric_models": (compare_atmospheric_models, {"lat", "lon", "hours_ahead"}),
}


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> tuple[dict, bool]:
    """Ejecuta una herramienta. Devuelve (resultado, is_error). Nunca lanza."""
    started = time.perf_counter()
    await ctx.emit({"type": "tool_start", "name": name, "input": args})
    is_error = False
    try:
        if name == "trigger_map_layer_update":
            allowed = {"layer_type", "focus_lat", "focus_lon", "zoom_level", "annotation"}
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
