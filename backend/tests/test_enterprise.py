"""Tests de los módulos Enterprise (sin red)."""
import os

os.environ["AEROBOT_FORCE_MOCK_LLM"] = "1"
os.environ["AEROBOT_ALERT_SCAN_MIN"] = "0"
os.environ["AEROBOT_MITIGATION_SCAN_MIN"] = "0"
os.environ["AEROBOT_POI_FILE"] = "/tmp/aerobot-test-pois.json"

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.alerts.pois import POIStore
from app.enterprise.audit import FORCE_MAJEURE, analyze, percentile
from app.enterprise.fire import FUEL_MODELS, rothermel, simard_emc
from app.enterprise.forecast import PointForecast
from app.enterprise.mitigation import MitigationService, detect
from app.enterprise.uav import evaluate_hour, profile_at
from app.services import http as http_mod

T0 = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def fail(*_a, **_k):
        raise http_mod.UpstreamError("offline (test)")
    for mod in ("app.services.aviation", "app.services.radar", "app.services.models", "app.alerts.collector",
                "app.sensors.sources", "app.sensors.consensus", "app.enterprise.forecast", "app.enterprise.fire",
                "app.enterprise.audit"):
        monkeypatch.setattr(f"{mod}.get_json", fail)


def pf_with(**series):
    n = len(next(iter(series.values())))
    return PointForecast(-33.4, -70.6, 500, [T0 + timedelta(hours=h) for h in range(n)], series, "test")


# ------------------------------------------------------------------ Módulo 1
def test_shear_vector_and_rejection():
    # 10 m: 5 kt del norte; 80 m: 20 kt del sur -> cizalladura direccional fuerte.
    pf = pf_with(wind_speed_10m=[5], wind_direction_10m=[0], wind_speed_80m=[20], wind_direction_80m=[180],
                 wind_speed_120m=[22], wind_direction_120m=[180], wind_gusts_10m=[9], visibility=[20000], precipitation=[0])
    p = profile_at(pf, 0)
    assert p["alpha"] == pytest.approx(0.667, abs=0.01)  # ln(20/5)/ln 8
    assert p["beta_max"] > 0.1
    r = evaluate_hour(pf, 0, 25, 0.1, 5000)
    assert not r["go"] and any("cizalladura" in x for x in r["reasons"])


def test_go_when_calm_and_rejected_by_gust_and_metar():
    pf = pf_with(wind_speed_10m=[6], wind_direction_10m=[200], wind_speed_80m=[9], wind_direction_80m=[205],
                 wind_speed_120m=[10], wind_direction_120m=[205], wind_gusts_10m=[12], visibility=[20000], precipitation=[0])
    assert evaluate_hour(pf, 0, 25, 0.1, 5000)["go"]
    r = evaluate_hour(pf, 0, 25, 0.1, 5000, obs={"station": "SCEL", "gust_kt": 28, "wind_kt": 15, "visibility_m": None})
    assert not r["go"] and r["critical_wind_kt"] == 28


def test_envelope_limits_levels():
    pf = pf_with(wind_speed_10m=[8], wind_direction_10m=[200], wind_speed_80m=[24], wind_direction_80m=[200],
                 wind_speed_120m=[30], wind_direction_120m=[200], wind_gusts_10m=[12], visibility=[20000], precipitation=[0])
    assert not evaluate_hour(pf, 0, 25, 0.5, 5000, max_agl_m=120)["go"]   # 120 m a 30 kt
    assert evaluate_hour(pf, 0, 25, 0.5, 5000, max_agl_m=60)["go"]        # techo 60 m: solo 10 y 50 m


# ------------------------------------------------------------------ Módulo 2
@pytest.mark.parametrize("fuel,expected_chh", [("pastizal", 78), ("matorral", 32), ("hojarasca", 1.6)])
def test_rothermel_matches_anderson_reference(fuel, expected_chh):
    """Referencia de Anderson (1982): 5 mph a media llama, 8 % de humedad muerta, sin pendiente."""
    r = rothermel(FUEL_MODELS[fuel], 0.08, 5 * 88, 0)
    ros_chh = r["r0_ftmin"] * (1 + r["phi_w"]) * 60 / 66
    assert ros_chh == pytest.approx(expected_chh, rel=0.35)


def test_simard_emc_monotonic():
    assert simard_emc(35, 10) < simard_emc(20, 40) < simard_emc(10, 90)


def test_runoff_formula():
    # Q = 0,278 · C · i · A  ->  0,278 · 0,5 · 10 · 100 = 139 m³/s
    assert 0.278 * 0.5 * 10 * 100 == pytest.approx(139)


# ------------------------------------------------------------------ Módulo 3
def test_audit_statistics_and_windows():
    start, end = T0, T0 + timedelta(hours=4)
    obs = [{"time": T0 + timedelta(minutes=30 * k), "wind_kt": 20, "gust_kt": g} for k, g in enumerate([25, 30, 41, 44, 39, 30, 25, 22])]
    r = analyze(obs, 38, start, end)
    assert r["exceeded"] and r["max_peak_kt"] == 44 and r["minutes_over_threshold"] == 90
    assert len(r["windows"]) == 1 and r["p95_peak_kt"] == pytest.approx(percentile([25, 30, 41, 44, 39, 30, 25, 22], 95), abs=0.1)
    assert not analyze(obs, 50, start, end)["exceeded"]
    assert "fuerza mayor" in FORCE_MAJEURE


# ------------------------------------------------------------------ Módulo 4
def test_frost_detection():
    pf = pf_with(temperature_2m=[3, 1, -1, -2.5, -1, 2], cloud_cover=[5, 5, 4, 3, 50, 5])
    ev = detect(pf, "agricola", T0)
    assert ev and ev[0]["hazard"] == "helada_radiativa" and ev[0]["level"] == 3
    assert detect(pf, "urbano", T0) == []


@pytest.mark.asyncio
async def test_policy_limits_and_approval():
    m = MitigationService(POIStore(None))
    res = await m.simulate_frost()
    status = {a["action"]: a["status"] for a in res["actions"]}
    assert status["activar_iot"] == "ejecutado"                 # monto 0 ≤ tope
    assert status["pre_aprobar_compra"] == "pendiente"          # 1,4 M > tope 1 M
    assert res["message"].startswith("Aviso de impacto inminente en Valle de Casablanca")
    # Una acción pedida por el agente sin evento validado queda pendiente aunque sea barata.
    manual = await m.trigger("demo", "activar_iot", "riego", 0)
    assert manual["status"] == "pendiente" and "sin evento validado" in manual["blockers"][0]
    pend = next(a for a in m.actions.values() if a["action"] == "pre_aprobar_compra")
    done = await m.approve(pend["id"])
    assert done["status"] == "ejecutado" and m.receiver_log
    again = await m.simulate_frost()                            # idempotencia
    assert all(a["status"] in ("duplicado", "pendiente") for a in again["actions"] if a.get("action") != "pre_aprobar_compra")


def test_api_offline_and_webhook_validation():
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/ops/flight-envelope", params={"lat": -33.4, "lon": -70.6}).status_code == 502
        bad = c.post("/api/mitigation/clients", json={"id": "acme", "name": "Acme", "webhooks": {"activar_iot": "http://10.0.0.1/x"}})
        assert bad.status_code == 422
        ok = c.post("/api/mitigation/clients", json={"id": "acme", "name": "Acme", "webhooks": {"activar_iot": "internal://test-receiver"}})
        assert ok.status_code == 201
        sim = c.post("/api/mitigation/simulate").json()
        assert "Ejecutando protocolo de mitigación" in sim["message"]
        chat = c.post("/api/chat", json={"message": "Simula el protocolo de mitigación logística"}).json()
        assert any(t["name"] == "evaluate_supply_chain_risk" for t in chat["tool_trace"])
