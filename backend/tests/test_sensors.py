"""Tests de validación de sensores (sin red)."""
import os

os.environ["AEROBOT_FORCE_MOCK_LLM"] = "1"
os.environ["AEROBOT_ALERT_SCAN_MIN"] = "0"
os.environ["AEROBOT_POI_FILE"] = "/tmp/aerobot-test-pois.json"

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.sensors.consensus import Consensus
from app.sensors.interpret import rule_based
from app.sensors.models import Reading, Sensor
from app.sensors.predict import predict
from app.sensors.qc import check_sensor, overall
from app.services import http as http_mod

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
HOURS = [NOW + timedelta(hours=h) for h in range(-6, 13)]
MODELS = ["gfs", "ecmwf", "icon", "gem", "jma", "meteofrance", "ukmo", "metno"]


def cons(temp_by_hour, spread=0.5):
    members = {}
    for k, m in enumerate(MODELS):
        off = (k - 3.5) * spread / 2
        members[m] = {"temp_c": [temp_by_hour(t) + off for t in HOURS], "wind_kt": [8 + off for _ in HOURS]}
    return Consensus(HOURS, members, ["test"])


def sensor(sid, temp, lat=-33.4, lon=-70.7, history=None):
    readings = [Reading(t, {"temp_c": v}) for t, v in (history or [])] + [Reading(NOW, {"temp_c": temp, "wind_kt": 8})]
    return Sensor(sid, sid, "metar", lat, lon, readings=readings)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def fail(*_a, **_k):
        raise http_mod.UpstreamError("offline (test)")
    for mod in ("app.services.aviation", "app.services.radar", "app.services.models", "app.alerts.collector",
                "app.sensors.sources", "app.sensors.consensus"):
        monkeypatch.setattr(f"{mod}.get_json", fail)


flat = lambda t: 20.0  # noqa: E731


def test_consistent_reading():
    s = sensor("a", 20.4)
    checks = check_sensor(s, [s], {"a": cons(flat)})
    assert overall(checks) == "consistente"


def test_fault_when_neighbors_agree_with_models():
    s, n1, n2 = sensor("a", 29.0), sensor("b", 20.2, lon=-70.75), sensor("c", 19.8, lon=-70.65)
    c = {k: cons(flat) for k in "abc"}
    checks = check_sensor(s, [s, n1, n2], c)
    t = next(x for x in checks if x.variable == "temp_c")
    assert t.verdict == "falla_probable" and t.neighbors


def test_local_phenomenon_when_neighbors_confirm():
    s, n1, n2 = sensor("a", 26.0), sensor("b", 25.5, lon=-70.75), sensor("c", 26.4, lon=-70.65)
    c = {k: cons(flat) for k in "abc"}
    t = next(x for x in check_sensor(s, [s, n1, n2], c) if x.variable == "temp_c")
    assert t.verdict == "fenomeno_local"


def test_stuck_value_only_when_models_expect_change():
    hist = [(NOW - timedelta(hours=h), 18.0) for h in (4, 3, 2, 1)]
    s = sensor("a", 18.0, history=hist)
    warming = lambda t: 14.0 + (t - NOW).total_seconds() / 3600 + 4  # noqa: E731  sube 1 °C/h
    t = next(x for x in check_sensor(s, [s], {"a": cons(warming)}) if x.variable == "temp_c")
    assert "valor_pegado" in t.flags and t.verdict == "falla_probable"
    t2 = next(x for x in check_sensor(s, [s], {"a": cons(lambda _: 18.0)}) if x.variable == "temp_c")
    assert "valor_pegado" not in t2.flags  # los modelos también esperan 18 °C constantes


def test_range_check():
    s = sensor("a", 80.0)
    assert overall(check_sensor(s, [s], {"a": cons(flat)})) == "falla_probable"


def test_prediction_bias_decays():
    s = sensor("a", 23.0)  # +3 °C sobre el consenso (vecino lo confirma)
    n = sensor("b", 22.8, lon=-70.75)
    c = {k: cons(flat) for k in "ab"}
    checks = check_sensor(s, [s, n], c)
    p = predict(c["a"], checks, NOW)["temp_c"]
    v = {r["h"]: r["value"] for r in p["series"]}
    assert p["trusted_observation"] and v[0] == pytest.approx(23.0, abs=0.1)
    assert 20.0 < v[6] < v[3] < v[0] and v[12] == pytest.approx(20 + 3 * 2.718 ** -2, abs=0.15)


def test_rule_based_interpretation_has_four_lines():
    s = sensor("a", 20.2)
    c = {"a": cons(flat)}
    checks = check_sensor(s, [s], c)
    from dataclasses import asdict
    rep = {"status": overall(checks), "checks": [asdict(x) for x in checks], "prediction": predict(c["a"], checks, NOW)}
    text = rule_based(rep)
    assert [l.split(":")[0] for l in text.splitlines()] == ["Lectura", "Diagnóstico", "Predicción", "Acción"]


def test_custom_sensor_feeds_alerts_offline():
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/sensors", json={"slug": "grua-1", "name": "Anemómetro grúa 1", "lat": -33.44, "lon": -70.65,
                                         "poi_id": "stgo-centro"})
        assert r.status_code == 201
        rep = c.post("/api/sensors/custom:grua-1/readings", json={"gust_kt": 58, "wind_kt": 30}).json()
        # Sin modelos (offline) el sensor queda "sin referencia" (confianza 0.6) y aún puede alertar.
        assert rep["status"] == "sin_referencia" and rep["alert"]["level"] == 3
        assert "Anemómetro grúa 1" in " ".join(rep["alert"]["sources"])
        assert any(a.get("origin") == "sensor" for a in c.get("/api/alerts").json()["alerts"])
        chat = c.post("/api/chat", json={"message": "¿El sensor de SCEL está fallando?"}).json()
        assert any(t["name"] == "inspect_sensor" for t in chat["tool_trace"])
