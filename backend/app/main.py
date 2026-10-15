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

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .agent.mock_llm import MockAnthropic
from .agent.runner import AeroBotAgent, AgentResult, trim_history
from .config import settings
from .schemas import ChatRequest, ChatResponse
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
    log.info("AeroBot listo · modo=%s · modelo=%s", mode, settings.anthropic_model if mode == "anthropic" else "mock")
    yield
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
