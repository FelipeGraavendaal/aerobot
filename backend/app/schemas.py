"""Contratos de la API (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class MapState(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    zoom: float = Field(ge=0, le=22)
    active_layer: str | None = None
    selected_point: LatLon | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    map_state: MapState | None = None


class MapCommand(BaseModel):
    id: str
    type: Literal["trigger_map_layer_update"]
    layer_type: Literal["radar", "wind_particles", "satellite_ir", "none"]
    focus: LatLon
    zoom_level: float
    annotation: str | None = None


class ToolTrace(BaseModel):
    name: str
    input: dict
    duration_ms: int
    is_error: bool
    source: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    map_commands: list[MapCommand]
    tool_trace: list[ToolTrace]
    mode: Literal["anthropic", "mock"]
    iterations: int
    usage: dict


# --------------------------------------------------------------------------
# Alertas / Lugares de Interés
# --------------------------------------------------------------------------
POIType = Literal["aeropuerto", "puerto", "obra", "evento", "ruta", "agricola", "urbano"]


class POICreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    type: POIType = "urbano"
    radius_km: float = Field(default=30, ge=5, le=150)
    timezone: str = "America/Santiago"
    icao: str | None = Field(default=None, max_length=4)


class TelemetryIn(BaseModel):
    """Variables que tocan un Lugar de Interés (sensor propio, integración externa, etc.)."""
    valid_at: datetime | None = Field(default=None, description="UTC; por defecto ahora")
    observed: bool = True
    source: str = Field(default="sensor externo", max_length=60)
    gust_kt: float | None = Field(default=None, ge=0, le=250)
    wind_kt: float | None = Field(default=None, ge=0, le=250)
    dbz: float | None = Field(default=None, ge=-10, le=80)
    cell_distance_km: float | None = Field(default=None, ge=0, le=500)
    cell_eta_min: float | None = Field(default=None, ge=0, le=1440)
    thunder: bool = False
    hail: bool = False
    precip_mm_h: float | None = Field(default=None, ge=0, le=500)
    precip_24h_mm: float | None = Field(default=None, ge=0, le=2000)
    snow_cm_h: float | None = Field(default=None, ge=0, le=100)
    temp_c: float | None = Field(default=None, ge=-90, le=60)
    visibility_m: float | None = Field(default=None, ge=0, le=100000)
    ceiling_ft: float | None = Field(default=None, ge=0, le=60000)
    cape_jkg: float | None = Field(default=None, ge=0, le=10000)
    shear_kt: float | None = Field(default=None, ge=0, le=200)


class EvaluateRequest(BaseModel):
    poi_id: str | None = None
    poi: POICreate | None = None
    telemetry: list[TelemetryIn] = Field(min_length=1, max_length=200)
    simulated: bool = False
