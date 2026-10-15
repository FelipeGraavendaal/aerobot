"""
Tests sin red: las llamadas HTTP externas se fuerzan a fallar para validar
la lógica pura y el respaldo con datos simulados; el bucle agéntico se
prueba con el LLM simulado (misma interfaz que AsyncAnthropic).

Ejecutar:  cd backend && pytest -q
"""
import os

os.environ["AEROBOT_FORCE_MOCK_LLM"] = "1"
os.environ["AEROBOT_ALERT_SCAN_MIN"] = "0"
os.environ["AEROBOT_POI_FILE"] = "/tmp/aerobot-test-pois.json"

import pytest
from fastapi.testclient import TestClient

from app.agent import tools as tools_mod
from app.agent.runner import trim_history
from app.services import http as http_mod
from app.services.aviation import flight_category, significant_weather
from app.services.radar import classify_dbz, rain_rate_to_dbz


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def fail(*_a, **_k):
        raise http_mod.UpstreamError("offline (test)")

    for mod in ("app.services.aviation", "app.services.radar", "app.services.models", "app.alerts.collector"):
        monkeypatch.setattr(f"{mod}.get_json", fail)


# ---------------------------------------------------------------- unitarios
def test_zr_relation():
    assert rain_rate_to_dbz(0) is None
    assert rain_rate_to_dbz(1.0) == pytest.approx(23.0, abs=0.1)      # 10log10(200)
    assert rain_rate_to_dbz(10.0) == pytest.approx(39.0, abs=0.1)
    assert rain_rate_to_dbz(10.0, convective=True) == pytest.approx(38.8, abs=0.2)
    assert classify_dbz(56) == "núcleo severo (posible granizo)"


def test_flight_category():
    assert flight_category(None, 10) == "VFR"
    assert flight_category(2500, 10) == "MVFR"
    assert flight_category(800, 10) == "IFR"
    assert flight_category(3000, 0.5) == "LIFR"


def test_significant_weather():
    wx = significant_weather("METAR SCEL 151800Z 18012G28KT 3000 +TSRA BKN015CB 12/10 Q1008")
    assert any("tormenta" in w and "fuerte" in w for w in wx)


def test_trim_history_keeps_turn_boundaries():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": []},
            {"role": "user", "content": [{"type": "tool_result"}]}, {"role": "assistant", "content": []},
            {"role": "user", "content": "b"}, {"role": "assistant", "content": []}]
    out = trim_history(msgs, 4)
    assert out[0] == {"role": "user", "content": "b"}


# ------------------------------------------------------------- herramientas
@pytest.mark.asyncio
async def test_tools_fallback_to_mock():
    ctx = tools_mod.ToolContext()
    for name, args in [
        ("fetch_aviation_weather", {"lat": -33.4, "lon": -70.8}),
        ("analyze_radar_echoes", {"lat": -33.4, "lon": -70.8, "radius_km": 40}),
        ("compare_atmospheric_models", {"lat": -33.4, "lon": -70.8, "hours_ahead": 24}),
    ]:
        res, err = await tools_mod.execute_tool(name, args, ctx)
        assert not err and res["source"] == "mock", name


@pytest.mark.asyncio
async def test_map_tool_validation_and_command():
    ctx = tools_mod.ToolContext()
    res, err = await tools_mod.execute_tool("trigger_map_layer_update", {"layer_type": "lava", "focus_lat": 0, "focus_lon": 0, "zoom_level": 5}, ctx)
    assert err and "inválido" in res["error"]
    res, err = await tools_mod.execute_tool("trigger_map_layer_update", {"layer_type": "radar", "focus_lat": -33, "focus_lon": -70, "zoom_level": 30}, ctx)
    assert not err and ctx.map_commands[0]["zoom_level"] == 12.0


# --------------------------------------------------------------- end-to-end
def test_chat_endpoint_full_loop():
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/chat", json={"message": "Analiza el radar y los modelos en Santiago",
                                           "map_state": {"lat": -33.4, "lon": -70.6, "zoom": 6}})
        assert r.status_code == 200
        data = r.json()
        names = {t["name"] for t in data["tool_trace"]}
        assert {"analyze_radar_echoes", "compare_atmospheric_models", "trigger_map_layer_update"} <= names
        assert data["map_commands"][0]["layer_type"] == "radar"
        assert "SIMULADOS" in data["reply"] and "[ALERTA]" not in data["reply"]

        # Segundo turno en la misma sesión (historial con tool_use/tool_result válido)
        r2 = client.post("/api/chat", json={"message": "¿y el METAR?", "session_id": data["session_id"]})
        assert r2.status_code == 200 and r2.json()["session_id"] == data["session_id"]


def test_websocket_streams_map_command_before_final():
    from app.main import app

    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"message": "muestra el viento en Punta Arenas"})
        types = []
        while True:
            ev = ws.receive_json()
            types.append(ev["type"])
            if ev["type"] in ("final", "error"):
                break
        assert "map_command" in types and types.index("map_command") < types.index("final")
