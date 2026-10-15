"""Tests del despachador de riesgos (sin red)."""
import os

os.environ["AEROBOT_FORCE_MOCK_LLM"] = "1"
os.environ["AEROBOT_ALERT_SCAN_MIN"] = "0"
os.environ["AEROBOT_POI_FILE"] = "/tmp/aerobot-test-pois.json"

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.alerts.collector import cell_eta_min
from app.alerts.dispatcher import Dispatcher, repair
from app.alerts.pois import DEFAULT_POIS, POI
from app.alerts.rules import Telemetry, assess, deterministic_message, time_block
from app.services import http as http_mod

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)  # 15:00 en Santiago
SCEL = next(p for p in DEFAULT_POIS if p.id == "scel")
STGO = next(p for p in DEFAULT_POIS if p.id == "stgo-centro")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def fail(*_a, **_k):
        raise http_mod.UpstreamError("offline (test)")

    for mod in ("app.services.aviation", "app.services.radar", "app.services.models", "app.alerts.collector"):
        monkeypatch.setattr(f"{mod}.get_json", fail)


def test_four_line_contract_and_levels():
    a = assess(SCEL, [Telemetry(valid_at=NOW, observed=True, source="METAR SCEL", gust_kt=47)], now=NOW)
    msg = deterministic_message(a).splitlines()
    assert len(msg) == 4 and all(l.startswith("[") and l.endswith("]") for l in msg)
    assert msg[0].startswith("[🔴 RIESGO ALTO")           # aeropuerto: 45 kt ya es rojo
    assert msg[1] == "[Aeropuerto AMB (SCEL), Pudahuel]"
    assert msg[2].startswith("[EN CURSO")
    # El mismo viento en un POI urbano es solo naranja (umbral 45/55).
    b = assess(STGO, [Telemetry(valid_at=NOW, observed=True, gust_kt=47)], now=NOW)
    assert b.level == 2


def test_forecast_window_in_local_time():
    snaps = [Telemetry(valid_at=NOW + timedelta(hours=h), precip_mm_h=18) for h in (2, 3, 4)]
    a = assess(STGO, snaps, now=NOW)
    assert a.level == 2
    assert time_block(a) == "[17:00–20:00 hora local · inicia en 2 h]"


def test_forecast_visibility_is_capped():
    a = assess(SCEL, [Telemetry(valid_at=NOW, observed=False, visibility_m=80)], now=NOW)
    assert a.level == 1 and "pronóstico" in time_block(a)


def test_radar_cell_needs_to_approach():
    far = Telemetry(valid_at=NOW, observed=True, dbz=52, cell_distance_km=40, cell_eta_min=None)
    near = Telemetry(valid_at=NOW, observed=True, dbz=52, cell_distance_km=40, cell_eta_min=30)
    assert assess(STGO, [far], now=NOW).level == 0
    a = assess(STGO, [near], now=NOW)
    assert a.level == 2 and "inicia en 30 min" in time_block(a)


def test_cell_eta_geometry():
    # Celda al oeste (270°) y viento rector DESDE el oeste a 30 kt -> se acerca.
    assert cell_eta_min(40, 270, 30, 270) == pytest.approx(43, abs=1)
    # Viento desde el este: la celda se aleja.
    assert cell_eta_min(40, 270, 30, 90) is None


def test_repair_keeps_valid_llm_action_and_rejects_greetings():
    a = assess(SCEL, [Telemetry(valid_at=NOW, observed=True, thunder=True)], now=NOW)
    good = "[x]\n[y]\n[z]\n[Suspender carga de combustible y retirar personal de plataforma ya]"
    msg, used = repair(good, a)
    assert used and msg.splitlines()[0].startswith("[🔴") and msg.endswith("plataforma ya]")
    msg, used = repair("Hola equipo!\n[Hola, se recomienda tener cuidado]", a)
    assert not used and "Activar alerta de rayos" in msg


def test_simulated_flag_reaches_level_block():
    a = assess(SCEL, [Telemetry(valid_at=NOW, observed=True, simulated=True, hail=True)], now=NOW)
    assert "SIMULACRO" in deterministic_message(a).splitlines()[0]


@pytest.mark.asyncio
async def test_dispatcher_without_llm_uses_rules():
    d = Dispatcher(None, "x", use_llm=False)
    a = assess(SCEL, [Telemetry(valid_at=NOW, observed=True, gust_kt=50)], now=NOW)
    msg, engine = await d.dispatch(a)
    assert engine == "reglas" and len(msg.splitlines()) == 4


def test_api_end_to_end():
    from app.main import app

    with TestClient(app) as c:
        assert len(c.get("/api/pois").json()) >= 6
        sims = c.get("/api/alerts/scenarios").json()
        a = c.post(f"/api/alerts/simulate/{sims[0]['id']}").json()
        assert a["simulated"] and len(a["lines"]) == 4

        r = c.post("/api/pois", json={"name": "Faena Test", "lat": -33.4, "lon": -70.6, "type": "obra"})
        assert r.status_code == 201
        pid = r.json()["id"]
        ev = c.post("/api/alerts/evaluate", json={"poi_id": pid, "telemetry": [{"gust_kt": 46}]}).json()
        assert ev["level"] == 3 and "grúas" in ev["message"]

        scan = c.post("/api/alerts/scan").json()  # sin red: sin alertas reales, con brechas declaradas
        assert all(x["simulated"] for x in scan["alerts"]) and scan["data_gaps"]
        assert c.delete(f"/api/pois/{pid}").status_code == 204
        assert c.delete("/api/pois/scel").status_code == 403

        chat = c.post("/api/chat", json={"message": "¿Qué alerta hay en Punta Arenas?"}).json()
        assert any(t["name"] == "dispatch_risk_alert" for t in chat["tool_trace"])


def test_separate_episodes_are_not_merged():
    casab = next(p for p in DEFAULT_POIS if p.id == "casablanca")
    tonight = [Telemetry(valid_at=NOW + timedelta(hours=h), temp_c=-1) for h in (8, 9, 10)]
    tomorrow = [Telemetry(valid_at=NOW + timedelta(hours=h), temp_c=-4) for h in (30, 31)]
    a = assess(casab, tonight + tomorrow, now=NOW)
    start, end = a.window
    assert a.level == 3 and start == NOW + timedelta(hours=30) and end == NOW + timedelta(hours=32)
    # En una ruta, la helada sola es PRECAUCIÓN.
    paso = next(p for p in DEFAULT_POIS if p.id == "los-libertadores")
    assert assess(paso, tomorrow, now=NOW).level == 1
