"""
Módulo 4 · Orquestación de mitigación (supply chain) con webhooks.

1. Detector determinista a 72 h por Lugar de Interés:
     helada radiativa  T < 0 °C con nubosidad < 10 %        (agrícola)
     viento fuerte     ráfaga ≥ 38 kt                       (obra, puerto, evento)
     lluvia intensa    ≥ 30 mm/24 h (alto ≥ 60 mm)          (obra, urbano, evento, agrícola)
     nevazón           ≥ 10 cm/24 h                         (ruta, aeropuerto)
2. Protocolo por peligro y tipo de lugar: lista de acciones (webhook + ítem + monto).
3. Política "automático con límites":
     se ejecuta sola  ⇔ evento de riesgo ALTO detectado por el motor (no por el LLM)
                        ∧ acción permitida para el cliente ∧ monto ≤ tope del cliente
     en otro caso     ⇒ queda PENDIENTE de aprobación humana.
   Los simulacros solo llegan al receptor interno de prueba.
4. Cada webhook va firmado (HMAC-SHA256 en X-AeroBot-Signature) y queda en bitácora,
   con idempotencia por (evento, acción).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..services.http import get_client
from .forecast import point_forecast

log = logging.getLogger("aerobot.mitigation")

INTERNAL_RECEIVER = "internal://test-receiver"
ACTIONS = ("pre_aprobar_compra", "activar_iot", "alertar_proveedor")


@dataclass
class Client:
    id: str
    name: str
    webhooks: dict[str, str]                       # acción -> URL (o internal://test-receiver)
    secret: str = ""
    auto_max_amount_clp: int = 1_000_000
    auto_actions: list[str] = field(default_factory=lambda: list(ACTIONS))


PROTOCOLS: dict[str, list[dict]] = {
    "helada_radiativa": [
        {"action": "activar_iot", "item": "Riego por aspersión anti-heladas (encendido 2 h antes del mínimo)", "amount_clp": 0},
        {"action": "pre_aprobar_compra", "item": "Mantas térmicas antiheladas 2.000 m²", "amount_clp": 1_400_000},
        {"action": "alertar_proveedor", "item": "Despacho urgente de combustible para calefactores", "amount_clp": 350_000},
    ],
    "viento_fuerte": [
        {"action": "alertar_proveedor", "item": "Reprogramar arriendo y montaje de grúa", "amount_clp": 0},
        {"action": "activar_iot", "item": "Aviso a operadores y bloqueo de izaje en el sistema de faena", "amount_clp": 0},
    ],
    "lluvia_intensa": [
        {"action": "pre_aprobar_compra", "item": "Bombas de achique + lonas de protección", "amount_clp": 450_000},
        {"action": "alertar_proveedor", "item": "Reprogramar despacho de hormigón", "amount_clp": 0},
    ],
    "nevazon": [
        {"action": "pre_aprobar_compra", "item": "Sal / salmuera para calzada (20 t)", "amount_clp": 2_600_000},
        {"action": "alertar_proveedor", "item": "Maquinaria de despeje en espera", "amount_clp": 0},
    ],
}
HAZARD_LABEL = {"helada_radiativa": "helada por radiación", "viento_fuerte": "viento fuerte",
                "lluvia_intensa": "lluvia intensa", "nevazon": "nevazón"}
APPLIES = {"helada_radiativa": {"agricola"}, "viento_fuerte": {"obra", "puerto", "evento"},
           "lluvia_intensa": {"obra", "urbano", "evento", "agricola"}, "nevazon": {"ruta", "aeropuerto"}}


def detect(pf, poi_type: str, now: datetime) -> list[dict]:
    """Eventos de impacto en las próximas 72 h a partir del pronóstico de un punto."""
    idx = [i for i, t in enumerate(pf.times) if now <= t <= now + timedelta(hours=72)]
    g = pf.get
    events = []

    def add(hazard, hits, level, peak):
        if hits:
            events.append({"hazard": hazard, "level": level, "start": pf.times[hits[0]].isoformat(),
                           "end": (pf.times[hits[-1]] + timedelta(hours=1)).isoformat(), "peak": peak,
                           "lead_h": round((pf.times[hits[0]] - now).total_seconds() / 3600, 1)})

    if poi_type in APPLIES["helada_radiativa"]:
        hits = [i for i in idx if g("temperature_2m", i) is not None and g("cloud_cover", i) is not None
                and g("temperature_2m", i) < 0 and g("cloud_cover", i) < 10]
        if hits:
            tmin = min(g("temperature_2m", i) for i in hits)
            add("helada_radiativa", hits, 3 if tmin <= -2 else 2, f"mínima {tmin:.1f} °C con cielo despejado")
    if poi_type in APPLIES["viento_fuerte"]:
        hits = [i for i in idx if (g("wind_gusts_10m", i) or 0) >= 38]
        if hits:
            add("viento_fuerte", hits, 3, f"ráfaga máx. {max(g('wind_gusts_10m', i) for i in hits):.0f} kt")
    for hazard, var, lim2, lim3, unit in (("lluvia_intensa", "precipitation", 30, 60, "mm"), ("nevazon", "snowfall", 10, 25, "cm")):
        if poi_type not in APPLIES[hazard] or not pf.series.get(var):
            continue
        best, best_i = 0.0, None
        for k, i in enumerate(idx):
            tot = sum(g(var, j) or 0 for j in idx[k:k + 24])
            if tot > best:
                best, best_i = tot, k
        if best >= lim2:
            hits = [i for i in idx[best_i:best_i + 24] if (g(var, i) or 0) > 0]
            add(hazard, hits, 3 if best >= lim3 else 2, f"{best:.0f} {unit} en 24 h")
    return events


class MitigationService:
    def __init__(self, poi_store):
        self.pois = poi_store
        self.clients: dict[str, Client] = {"demo": Client("demo", "Cliente demo (receptor de prueba)",
                                                          {a: INTERNAL_RECEIVER for a in ACTIONS})}
        self.events: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}         # id -> acción (pendiente/ejecutada/rechazada/fallida)
        self.receiver_log: list[dict] = []
        self._done: set[str] = set()

    # ---------------------------------------------------------------- clientes
    def upsert_client(self, **kw) -> Client:
        c = Client(**kw)
        bad = [u for u in c.webhooks.values() if not (u == INTERNAL_RECEIVER or u.startswith("https://"))]
        if bad:
            raise ValueError(f"Las URLs de webhook deben ser https:// o {INTERNAL_RECEIVER}: {bad}")
        unknown = set(c.webhooks) - set(ACTIONS)
        if unknown:
            raise ValueError(f"Acciones desconocidas: {unknown}. Válidas: {ACTIONS}")
        self.clients[c.id] = c
        return c

    # --------------------------------------------------------------- webhooks
    async def _send(self, client: Client, payload: dict, simulated: bool) -> dict:
        url = client.webhooks.get(payload["action"])
        if not url:
            return {"ok": False, "detail": f"El cliente no tiene webhook para '{payload['action']}'"}
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        if url == INTERNAL_RECEIVER or simulated:
            self.receiver_log = (self.receiver_log + [{"received_at": datetime.now(timezone.utc).isoformat(),
                                                       "url": url, "payload": payload}])[-100:]
            return {"ok": True, "status": 200, "detail": "recibido por el receptor interno de prueba"
                    + (" (simulacro: no se envió al sistema real)" if simulated and url != INTERNAL_RECEIVER else "")}
        headers = {"Content-Type": "application/json", "X-AeroBot-Event": payload.get("event_id") or "manual"}
        if client.secret:
            headers["X-AeroBot-Signature"] = "sha256=" + hmac.new(client.secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            r = await get_client().post(url, content=body, headers=headers, timeout=15)
            return {"ok": 200 <= r.status_code < 300, "status": r.status_code, "detail": r.text[:200]}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    async def trigger(self, client_id: str, action: str, item: str, amount_clp: int = 0, quantity: str | None = None,
                      event_id: str | None = None, poi_id: str | None = None, reason: str | None = None,
                      requested_by: str = "agente") -> dict:
        """trigger_supply_chain_webhook: aplica la política de límites y ejecuta o deja pendiente."""
        client = self.clients.get(client_id)
        if client is None:
            raise ValueError(f"Cliente '{client_id}' no configurado")
        if action not in ACTIONS:
            raise ValueError(f"Acción inválida '{action}'. Válidas: {ACTIONS}")
        ev = self.events.get(event_id) if event_id else None
        simulated = bool(ev and ev.get("simulated"))
        key = f"{event_id}|{action}|{item}"
        if event_id and key in self._done:
            return {"status": "duplicado", "action": action, "item": item, "detail": "Esta acción ya se ejecutó para este evento"}
        if event_id:
            pend = next((a for a in self.actions.values() if a["status"] == "pendiente" and a["event_id"] == event_id
                         and a["action"] == action and a["item"] == item), None)
            if pend:
                return pend
        blockers = []
        if not ev:
            blockers.append("sin evento validado por el motor de reglas")
        elif ev["level"] < 3:
            blockers.append(f"riesgo {ev['level']}/3 (se auto-ejecuta solo con riesgo alto)")
        if action not in client.auto_actions:
            blockers.append(f"'{action}' no está habilitada para ejecución automática")
        if amount_clp > client.auto_max_amount_clp:
            blockers.append(f"monto ${amount_clp:,.0f} supera el tope automático ${client.auto_max_amount_clp:,.0f}".replace(",", "."))
        rec = {"id": uuid.uuid4().hex[:10], "client_id": client_id, "action": action, "item": item, "amount_clp": amount_clp,
               "quantity": quantity, "event_id": event_id, "poi_id": poi_id or (ev or {}).get("poi_id"), "reason": reason,
               "requested_by": requested_by, "simulated": simulated, "created_at": datetime.now(timezone.utc).isoformat()}
        if blockers:
            rec.update(status="pendiente", blockers=blockers)
            self.actions[rec["id"]] = rec
            return rec
        return await self._execute(rec, approved_by="política automática")

    async def _execute(self, rec: dict, approved_by: str) -> dict:
        client = self.clients[rec["client_id"]]
        payload = {k: rec[k] for k in ("action", "item", "amount_clp", "quantity", "event_id", "poi_id", "reason", "simulated")}
        payload.update(client_id=client.id, issued_at=datetime.now(timezone.utc).isoformat(), approved_by=approved_by,
                       event=self.events.get(rec["event_id"]))
        res = await self._send(client, payload, rec["simulated"])
        rec.update(status="ejecutado" if res["ok"] else "fallido", response=res, approved_by=approved_by,
                   executed_at=datetime.now(timezone.utc).isoformat())
        if res["ok"] and rec.get("event_id"):
            self._done.add(f"{rec['event_id']}|{rec['action']}|{rec['item']}")
        self.actions[rec["id"]] = rec
        return rec

    async def approve(self, action_id: str, approved_by: str = "usuario") -> dict:
        rec = self.actions.get(action_id)
        if not rec or rec["status"] != "pendiente":
            raise KeyError(action_id)
        return await self._execute(rec, approved_by=approved_by)

    def reject(self, action_id: str) -> dict:
        rec = self.actions.get(action_id)
        if not rec or rec["status"] != "pendiente":
            raise KeyError(action_id)
        rec["status"] = "rechazado"
        return rec

    # ------------------------------------------------------------- protocolo
    def _message(self, poi, ev: dict, recs: list[dict]) -> str:
        tz = ZoneInfo(poi.timezone)
        start = datetime.fromisoformat(ev["start"]).astimezone(tz).strftime("%d-%m %H:%M")
        lines = [f"Aviso de impacto inminente en {poi.name}. Ejecutando protocolo de mitigación:",
                 f"Evento: {HAZARD_LABEL[ev['hazard']]} ({ev['peak']}) desde {start} hora local, en {ev['lead_h']:.0f} h."
                 + (" SIMULACRO." if ev.get("simulated") else "")]
        for r in recs:
            tag = {"ejecutado": "[EJECUTADO]", "pendiente": "[PENDIENTE DE APROBACIÓN]", "fallido": "[FALLIDO]",
                   "duplicado": "[YA EJECUTADO]"}.get(r.get("status"), "[?]")
            extra = f" — {'; '.join(r['blockers'])}" if r.get("blockers") else ""
            amount = f" (${r['amount_clp']:,.0f})".replace(",", ".") if r.get("amount_clp") else ""
            lines.append(f"{tag} {r.get('action', '')}: {r.get('item', '')}{amount}{extra}")
        return "\n".join(lines)

    async def run_protocol(self, poi, ev: dict, client_id: str = "demo") -> dict:
        ev_id = ev.setdefault("event_id", hashlib.sha1(f"{poi.id}|{ev['hazard']}|{ev['start'][:13]}|{ev.get('simulated')}".encode()).hexdigest()[:10])
        ev["poi_id"] = poi.id
        self.events[ev_id] = ev
        recs = []
        for step in PROTOCOLS[ev["hazard"]]:
            recs.append(await self.trigger(client_id, step["action"], step["item"], step["amount_clp"], event_id=ev_id,
                                           poi_id=poi.id, reason=f"{HAZARD_LABEL[ev['hazard']]}: {ev['peak']}",
                                           requested_by="motor de mitigación"))
        return {"event": ev, "message": self._message(poi, ev, recs), "actions": recs}

    async def scan(self, client_id: str = "demo") -> list[dict]:
        now = datetime.now(timezone.utc)
        out, sem = [], asyncio.Semaphore(3)

        async def one(poi):
            async with sem:
                try:
                    pf = await point_forecast(poi.lat, poi.lon, hours=73)
                except Exception as exc:
                    return [{"poi_id": poi.id, "error": f"pronóstico no disponible ({type(exc).__name__})"}]
                res = []
                for ev in detect(pf, poi.type, now):
                    ev["source"] = pf.source
                    res.append(await self.run_protocol(poi, ev, client_id))
                return res

        for r in await asyncio.gather(*(one(p) for p in self.pois.list())):
            out.extend(r)
        return out

    async def simulate_frost(self, poi_id: str = "casablanca", client_id: str = "demo") -> dict:
        poi = self.pois.get(poi_id)
        if poi is None:
            raise KeyError(poi_id)
        start = (datetime.now(timezone.utc) + timedelta(hours=50)).replace(minute=0, second=0, microsecond=0)
        ev = {"hazard": "helada_radiativa", "level": 3, "start": start.isoformat(), "end": (start + timedelta(hours=5)).isoformat(),
              "peak": "mínima −3,5 °C con nubosidad 4 %", "lead_h": 50.0, "simulated": True, "source": "simulacro"}
        return await self.run_protocol(poi, ev, client_id)

    def state(self) -> dict:
        return {"clients": [{**asdict(c), "secret": "***" if c.secret else ""} for c in self.clients.values()],
                "events": list(self.events.values())[-50:],
                "pending": [a for a in self.actions.values() if a["status"] == "pendiente"],
                "log": sorted([a for a in self.actions.values() if a["status"] != "pendiente"],
                              key=lambda a: a.get("executed_at") or a["created_at"], reverse=True)[:50],
                "receiver_log": self.receiver_log[-20:][::-1]}
