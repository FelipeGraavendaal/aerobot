"""Contratos de la API (Pydantic v2)."""
from __future__ import annotations

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
