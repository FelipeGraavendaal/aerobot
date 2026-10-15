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
    overlay: dict | None = None          # GeoJSON a dibujar (p. ej. cono de incendio)


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


# --------------------------------------------------------------------------
# Sensores
# --------------------------------------------------------------------------
class SensorCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    elevation_m: float | None = Field(default=None, ge=-500, le=9000)
    poi_id: str | None = Field(default=None, description="Lugar de Interés al que alimenta alertas")


class SensorReadingIn(BaseModel):
    time: datetime | None = None
    temp_c: float | None = None
    dewpoint_c: float | None = None
    rh_pct: float | None = None
    pressure_hpa: float | None = Field(default=None, description="Presión a nivel del mar (QNH)")
    station_pressure: float | None = Field(default=None, description="Presión absoluta en la estación")
    wind_kt: float | None = None
    wind_dir: float | None = None
    gust_kt: float | None = None
    precip_mm_h: float | None = None


# --------------------------------------------------------------------------
# Enterprise
# --------------------------------------------------------------------------
class FireRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    fuel: Literal["pastizal", "pastizal_alto", "matorral", "matorral_denso", "hojarasca"] = "pastizal"
    humidity: float | None = Field(default=None, ge=1, le=100)
    wind_dir: float | None = Field(default=None, ge=0, le=360)
    wind_speed: float | None = Field(default=None, ge=0, le=100, description="kt a 10 m")


class RunoffRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    catchment_area_km2: float = Field(gt=0, le=50000)
    catchment_top_m: float | None = Field(default=None, ge=0, le=9000)
    runoff_coeff: float = Field(default=0.5, ge=0.05, le=0.95)
    tc_hours: int = Field(default=1, ge=1, le=24)
    channel_capacity_m3s: float | None = Field(default=None, gt=0)


class ClientReading(BaseModel):
    time: datetime
    wind_kt: float | None = Field(default=None, ge=0, le=250)
    gust_kt: float | None = Field(default=None, ge=0, le=250)


class AuditRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    threshold_knots: float = Field(default=38, ge=5, le=100)
    start_hour: int = Field(default=0, ge=0, le=23)
    end_hour: int = Field(default=24, ge=1, le=24)
    station: str | None = Field(default=None, max_length=4)
    site_name: str | None = Field(default=None, max_length=120)
    contract_ref: str | None = Field(default=None, max_length=120)
    client_readings: list[ClientReading] | None = Field(default=None, max_length=5000)


class ClientConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    name: str = Field(min_length=2, max_length=80)
    webhooks: dict[str, str] = Field(description="acción -> URL https o internal://test-receiver")
    secret: str = Field(default="", max_length=200)
    auto_max_amount_clp: int = Field(default=1_000_000, ge=0)
    auto_actions: list[str] = ["pre_aprobar_compra", "activar_iot", "alertar_proveedor"]
