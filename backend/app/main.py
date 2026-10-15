"""
Servidor FastAPI de AeroBot.

Endpoints:
  POST /api/chat         -> turno de chat (REST). Devuelve texto + comandos de mapa.
  WS   /ws/chat          -> igual que /api/chat, pero emite eventos en vivo
                            (tool_start, tool_end, map_command, final).
  GET  /api/radar/frames -> frames RainViewer para la capa de radar animada.
  GET  /api/wind-grid    -> malla u/v de viento para la capa de partículas.
  GET  /api/health       -> estado y modo (anthropic | mock).
  GET  /                 -> frontend estático (../frontend).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from datetime import datetime, timezone

from .agent import tools as agent_tools
from .agent.mock_llm import MockAnthropic
from .alerts.dispatcher import Dispatcher
from .alerts.pois import POI, POIStore
from .alerts.rules import Telemetry
from .alerts.scenarios import SCENARIOS
from .alerts.service import AlertService
from .agent.runner import AeroBotAgent, AgentResult, trim_history
from .config import settings
from .schemas import (AuditRequest, ChatRequest, ChatResponse, ClientConfig, EvaluateRequest, FireRequest, POICreate,
                      RunoffRequest, SensorCreate, SensorReadingIn)
from .sensors.service import SensorService
from .enterprise.mitigation import MitigationService
from .enterprise import uav as ent_uav, fire as ent_fire, flood as ent_flood
from .enterprise.pdf import render_pdf
from .services.http import UpstreamError, close_client
from .services.models import wind_grid
from .services.radar import rainviewer_frames

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aerobot")


# ---------------------------------------------------------------------------
# Sesiones de conversación en memoria (LRU). En producción: Redis/Postgres.
# ---------------------------------------------------------------------------
class SessionStore:
    def __init__(self, max_sessions: int = 500):
        self._data: OrderedDict[str, list[dict]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self.max_sessions = max_sessions

    def get(self, sid: str) -> list[dict]:
        if sid in self._data:
            self._data.move_to_end(sid)
        return self._data.get(sid, [])

    def set(self, sid: str, messages: list[dict]) -> None:
        self._data[sid] = trim_history(messages, settings.max_history_messages)
        self._data.move_to_end(sid)
        while len(self._data) > self.max_sessions:
            old, _ = self._data.popitem(last=False)
            self._locks.pop(old, None)

    def lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())


def build_llm_client():
    if settings.use_mock_llm:
        log.warning("AeroBot en modo MOCK (sin ANTHROPIC_API_KEY o AEROBOT_FORCE_MOCK_LLM=1).")
        return MockAnthropic(), "mock"
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key), "anthropic"


@asynccontextmanager
async def lifespan(app: FastAPI):
    client, mode = build_llm_client()
    app.state.mode = mode
    app.state.agent = AeroBotAgent(client, settings.anthropic_model, settings.max_tokens, settings.max_agent_iterations)
    app.state.sessions = SessionStore()
    dispatcher = Dispatcher(client, settings.anthropic_model, use_llm=(mode == "anthropic"))
    app.state.alerts = AlertService(POIStore(settings.poi_file), dispatcher, settings.alert_scan_minutes)
    agent_tools.ALERT_SERVICE = app.state.alerts
    app.state.sensors = SensorService(client, settings.anthropic_model, use_llm=(mode == "anthropic"),
                                      alert_service=app.state.alerts)
    agent_tools.SENSOR_SERVICE = app.state.sensors
    app.state.mitigation = MitigationService(app.state.alerts.store)
    agent_tools.MITIGATION_SERVICE = app.state.mitigation
    mitigation_task = None
    if settings.mitigation_scan_minutes > 0:
        async def _mitigation_loop():
            await asyncio.sleep(20)
            while True:
                try:
                    await app.state.mitigation.scan()
                except Exception:
                    log.exception("Fallo en escaneo de mitigación")
                await asyncio.sleep(settings.mitigation_scan_minutes * 60)
        mitigation_task = asyncio.create_task(_mitigation_loop())
    app.state.alerts.start()
    log.info("AeroBot listo · modo=%s · modelo=%s", mode, settings.anthropic_model if mode == "anthropic" else "mock")
    yield
    await app.state.alerts.stop()
    if mitigation_task:
        mitigation_task.cancel()
    await close_client()


app = FastAPI(title="AeroBot API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"])


async def _run_turn(req: ChatRequest, on_event=None) -> ChatResponse:
    sid = req.session_id or uuid.uuid4().hex
    store: SessionStore = app.state.sessions
    async with store.lock(sid):
        history = store.get(sid)
        result: AgentResult
        result, new_history = await app.state.agent.run(
            history, req.message, req.map_state.model_dump() if req.map_state else None, on_event=on_event)
        store.set(sid, new_history)
    return ChatResponse(
        session_id=sid, reply=result.reply, map_commands=result.map_commands, tool_trace=result.tool_trace,
        mode=app.state.mode, iterations=result.iterations, usage=result.usage,
    )


def _llm_error_message(exc: Exception) -> str:
    name = type(exc).__name__
    if "Authentication" in name:
        return "API key de Anthropic inválida."
    if "RateLimit" in name:
        return "Límite de uso del LLM alcanzado; intenta en unos segundos."
    return f"Error del agente: {name}"


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": app.state.mode,
            "model": settings.anthropic_model if app.state.mode == "anthropic" else "mock"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    try:
        return await _run_turn(req)
    except Exception as exc:
        log.exception("Fallo en /api/chat")
        raise HTTPException(status_code=502, detail=_llm_error_message(exc)) from exc


@app.get("/api/radar/frames")
async def radar_frames():
    try:
        return await rainviewer_frames()
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/wind-grid")
async def api_wind_grid(
    south: float = Query(ge=-90, le=90), west: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90), east: float = Query(ge=-180, le=180),
    nx: int = Query(12, ge=2, le=16), ny: int = Query(9, ge=2, le=16),
):
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="bbox inválido (north>south y east>west).")
    # Redondeo a 0.25° para aprovechar la caché entre paneos pequeños.
    r = lambda x: round(x * 4) / 4  # noqa: E731
    try:
        return await wind_grid(r(south), r(west), r(north), r(east), nx, ny)
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Alertas y Lugares de Interés
# ---------------------------------------------------------------------------
def _poi_out(p: POI) -> dict:
    return {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon, "type": p.type,
            "radius_km": p.radius_km, "timezone": p.timezone, "icao": p.icao, "custom": p.custom}


@app.get("/api/pois")
async def list_pois():
    return [_poi_out(p) for p in app.state.alerts.store.list()]


@app.post("/api/pois", status_code=201)
async def create_poi(body: POICreate):
    try:
        poi = app.state.alerts.store.add(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _poi_out(poi)


@app.delete("/api/pois/{poi_id}", status_code=204)
async def delete_poi(poi_id: str):
    poi = app.state.alerts.store.get(poi_id)
    if poi is None:
        raise HTTPException(status_code=404, detail="Lugar no encontrado")
    if not poi.custom:
        raise HTTPException(status_code=403, detail="Los lugares predefinidos no se pueden eliminar")
    app.state.alerts.store.delete(poi_id)


@app.get("/api/alerts")
async def get_alerts():
    svc: AlertService = app.state.alerts
    if svc.latest()["generated_at"] is None:
        return await svc.scan()
    return svc.latest()


@app.post("/api/alerts/scan")
async def scan_alerts():
    return await app.state.alerts.scan()


@app.get("/api/alerts/scenarios")
async def list_scenarios():
    return [{"id": k, "title": v["title"], "poi_id": v["poi_id"]} for k, v in SCENARIOS.items()]


@app.post("/api/alerts/simulate/{scenario_id}")
async def simulate_alert(scenario_id: str):
    try:
        return await app.state.alerts.simulate(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Simulacro no encontrado: {exc}") from exc


@app.post("/api/alerts/evaluate")
async def evaluate_alert(body: EvaluateRequest):
    """Telemetría externa -> directriz. Requiere poi_id registrado o un poi ad hoc."""
    store = app.state.alerts.store
    if body.poi_id:
        poi = store.get(body.poi_id)
        if poi is None:
            raise HTTPException(status_code=404, detail="Lugar no encontrado")
    elif body.poi:
        try:
            poi = POI("ad-hoc", **body.poi.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=422, detail="Indica poi_id o poi")
    now = datetime.now(timezone.utc)

    def utc(d: datetime | None) -> datetime:
        d = d or now
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)

    telemetry = [Telemetry(**{**t.model_dump(), "valid_at": utc(t.valid_at), "simulated": body.simulated})
                 for t in body.telemetry]
    return await app.state.alerts.evaluate(poi, telemetry)


# ---------------------------------------------------------------------------
# Sensores: validación multimodelo, predicción e interpretación del agente
# ---------------------------------------------------------------------------
@app.get("/api/sensors")
async def list_sensors(south: float = Query(ge=-90, le=90), west: float = Query(ge=-180, le=180),
                       north: float = Query(ge=-90, le=90), east: float = Query(ge=-180, le=180)):
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="bbox inválido")
    if (north - south) * (east - west) > 400:
        raise HTTPException(status_code=422, detail="Área demasiado grande: acerca el mapa (máx. ~20°×20°)")
    return await app.state.sensors.network(south, west, north, east)


@app.get("/api/sensors/{sensor_id}/report")
async def sensor_report(sensor_id: str):
    try:
        return await app.state.sensors.report(sensor_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Sensor no encontrado: lista primero la zona") from exc


@app.post("/api/sensors/{sensor_id}/interpret")
async def sensor_interpret(sensor_id: str):
    try:
        return await app.state.sensors.interpretation(sensor_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Sensor no encontrado: lista primero la zona") from exc


@app.post("/api/sensors", status_code=201)
async def register_sensor(body: SensorCreate):
    if body.poi_id and app.state.alerts.store.get(body.poi_id) is None:
        raise HTTPException(status_code=422, detail="poi_id no existe")
    s = app.state.sensors.register(**body.model_dump())
    return {"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon, "poi_id": s.poi_id}


@app.post("/api/sensors/{sensor_id}/readings")
async def post_reading(sensor_id: str, body: SensorReadingIn):
    if app.state.sensors.custom.get(sensor_id) is None:
        raise HTTPException(status_code=404, detail="Sensor no registrado (POST /api/sensors primero)")
    values = {k: v for k, v in body.model_dump(exclude={"time"}).items() if v is not None}
    if not values:
        raise HTTPException(status_code=422, detail="La lectura no trae variables")
    t = body.time or datetime.now(timezone.utc)
    t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return await app.state.sensors.ingest(sensor_id, t, values)


# ---------------------------------------------------------------------------
# AeroBot Enterprise: módulos 1-4
# ---------------------------------------------------------------------------
def _require_admin(token: str | None) -> None:
    if settings.admin_token and token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Requiere X-Admin-Token")


async def _guard(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Fuente de datos no disponible: {exc}") from exc


@app.get("/api/ops/flight-envelope")
async def flight_envelope(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                          max_wind_knots: float = Query(25, ge=5, le=60), max_agl_meters: float = Query(120, ge=10, le=120),
                          hours: int = Query(24, ge=3, le=72)):
    return await _guard(ent_uav.go_no_go(lat, lon, hours=hours, max_wind_kt=max_wind_knots, max_agl_m=max_agl_meters))


@app.post("/api/ops/fire")
async def fire(body: FireRequest):
    return await _guard(ent_fire.fire_spread(body.lat, body.lon, fuel=body.fuel, hours=[1, 3], humidity=body.humidity,
                                             wind_dir=body.wind_dir, wind_speed_kt=body.wind_speed))


@app.post("/api/ops/runoff")
async def runoff(body: RunoffRequest):
    return await _guard(ent_flood.flood_runoff(body.lat, body.lon, body.catchment_area_km2, body.catchment_top_m,
                                               body.runoff_coeff, body.tc_hours, body.channel_capacity_m3s))


@app.post("/api/audit")
async def audit(body: AuditRequest):
    from .enterprise.audit import audit_historical_weather as run_audit
    r = await _guard(run_audit(body.lat, body.lon, body.date, body.threshold_knots, body.start_hour, body.end_hour,
                               station=body.station, site_name=body.site_name, contract_ref=body.contract_ref,
                               client_readings=[x.model_dump() for x in body.client_readings or []]))
    agent_tools.AUDITS[r["audit_id"]] = r
    return {**{k: v for k, v in r.items() if k != "raw_data"}, "pdf_url": f"/api/audit/{r['audit_id']}.pdf",
            "raw_url": f"/api/audit/{r['audit_id']}/raw", "raw_data_points": {k: len(v) for k, v in r["raw_data"].items()}}


@app.get("/api/audit/{audit_id}.pdf")
async def audit_pdf(audit_id: str):
    a = agent_tools.AUDITS.get(audit_id)
    if not a:
        raise HTTPException(status_code=404, detail="Auditoría no encontrada (expira al reiniciar el servidor)")
    return Response(render_pdf(a["pdf_payload"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="aerobot-auditoria-{audit_id}.pdf"'})


@app.get("/api/audit/{audit_id}/raw")
async def audit_raw(audit_id: str):
    a = agent_tools.AUDITS.get(audit_id)
    if not a:
        raise HTTPException(status_code=404, detail="Auditoría no encontrada")
    return a["raw_data"]


@app.get("/api/mitigation")
async def mitigation_state():
    return app.state.mitigation.state()


@app.post("/api/mitigation/scan")
async def mitigation_scan(client_id: str = "demo"):
    return await app.state.mitigation.scan(client_id)


@app.post("/api/mitigation/simulate")
async def mitigation_simulate(client_id: str = "demo"):
    return await app.state.mitigation.simulate_frost(client_id=client_id)


@app.post("/api/mitigation/actions/{action_id}/approve")
async def mitigation_approve(action_id: str, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        return await app.state.mitigation.approve(action_id, approved_by="usuario (app)")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Acción no encontrada o ya resuelta") from exc


@app.post("/api/mitigation/actions/{action_id}/reject")
async def mitigation_reject(action_id: str, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        return app.state.mitigation.reject(action_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Acción no encontrada o ya resuelta") from exc


@app.post("/api/mitigation/clients", status_code=201)
async def mitigation_client(body: ClientConfig, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        c = app.state.mitigation.upsert_client(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": c.id, "name": c.name, "webhooks": c.webhooks, "auto_max_amount_clp": c.auto_max_amount_clp}


@app.post("/api/mitigation/test-receiver")
async def mitigation_receiver(payload: dict):
    """Receptor de prueba: úsalo como URL de webhook para ver qué enviaría AeroBot."""
    m = app.state.mitigation
    m.receiver_log = (m.receiver_log + [{"received_at": datetime.now(timezone.utc).isoformat(), "url": "http", "payload": payload}])[-100:]
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket: el mapa reacciona en cuanto el LLM invoca trigger_map_layer_update
# ---------------------------------------------------------------------------
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": "ready", "mode": app.state.mode})
    try:
        while True:
            raw = await ws.receive_json()
            try:
                req = ChatRequest.model_validate(raw)
            except ValidationError as exc:
                await ws.send_json({"type": "error", "detail": exc.errors()[0].get("msg", "payload inválido")})
                continue

            async def emit(event: dict):
                await ws.send_json(event)

            try:
                resp = await _run_turn(req, on_event=emit)
                await ws.send_json({"type": "final", **resp.model_dump()})
            except Exception as exc:
                log.exception("Fallo en /ws/chat")
                await ws.send_json({"type": "error", "detail": _llm_error_message(exc)})
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Frontend estático (se monta al final para no tapar las rutas /api)
# ---------------------------------------------------------------------------
if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
