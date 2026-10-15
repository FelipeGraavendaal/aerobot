"""
Interpretación del agente sobre un sensor.

El veredicto (QC) y los números (consenso, predicción) son deterministas.
Claude los convierte en un diagnóstico legible; si no hay LLM o la respuesta
rompe el formato, se usa la redacción por reglas.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .models import VARIABLES, VERDICT_LABEL

log = logging.getLogger("aerobot.sensors.interpret")

INTERPRETER_PROMPT = """Eres el intérprete de sensores de AeroBot, meteorólogo experto en control de calidad de estaciones y en pronóstico a corto plazo.
Recibes en JSON: la última lectura del sensor, el consenso de hasta 8 modelos (GFS, ECMWF, ICON, GEM, JMA, Météo-France, UKMO, MET Norway), los chequeos de vecinos, el veredicto de control de calidad (determinista, no lo cambies) y una predicción corregida por sesgo para las próximas 12 h.

Responde en español, sin saludos ni relleno, EXACTAMENTE con estas 4 líneas:
Lectura: <qué mide el sensor frente a la mediana y el rango de los modelos, con cifras y unidades>
Diagnóstico: <si la diferencia es un fenómeno local real, una falla del sensor o es consistente, y por qué: vecinos, saltos, valor pegado, dispersión entre modelos>
Predicción: <valores esperados en 3 y 6 h con su banda, indicando que incorpora el sesgo observado; menciona el desacuerdo entre modelos si es alto>
Acción: <una acción concreta: confiar en el sensor para alertas, revisarlo/recalibrarlo, o vigilar>

Reglas: usa solo los números entregados; máximo 130 palabras en total; no inventes causas sin evidencia en los datos."""

_LINE = re.compile(r"^(Lectura|Diagnóstico|Predicción|Acción):\s*\S", re.M)


def _pick(series: list[dict], h: int) -> dict | None:
    return next((r for r in series if r["h"] == h), None)


def rule_based(report: dict) -> str:
    checks = report["checks"]
    main = sorted(checks, key=lambda c: -abs(c.get("z") or 0))[:2]
    def fmt(c):
        unit = VARIABLES.get(c["variable"], c["variable"])
        if c.get("consensus_median") is None:
            return f"{unit}: {c['observed']} (sin modelos)"
        rng = f", rango {c['consensus_min']}–{c['consensus_max']}" if c.get("consensus_min") is not None else ""
        return f"{unit}: {c['observed']} vs mediana {c['consensus_median']}{rng} (z={c['z']})"
    lectura = "; ".join(fmt(c) for c in main) or "sin variables comparables"
    worst = report["status"]
    if worst == "consistente":
        zmax = max(checks, key=lambda c: abs(c.get("z") or 0), default=None)
        extra = f" (máx. {abs(zmax['z']):.1f}σ en {VARIABLES[zmax['variable']].split(' (')[0].lower()})" if zmax and zmax.get("z") is not None else ""
        diag = f"{VERDICT_LABEL[worst]}: todas las variables dentro de ±2σ del consenso{extra}."
    else:
        reasons = "; ".join(f"{VARIABLES[c['variable']].split(' (')[0].lower()}: {c['reason']}"
                            for c in checks if c["verdict"] == worst)[:240]
        diag = f"{VERDICT_LABEL[worst]}. {reasons}."
    pred_parts = []
    for var in ("temp_c", "wind_kt", "gust_kt"):
        p = report["prediction"].get(var)
        if not p:
            continue
        h3, h6 = _pick(p["series"], 3), _pick(p["series"], 6)
        if h3 and h6:
            pred_parts.append(f"{VARIABLES[var].split(' (')[0].lower()} {h3['value']} (+3 h, {h3['low']}–{h3['high']}) y "
                              f"{h6['value']} (+6 h)" + (f", sesgo {p['bias_applied']:+}" if p["bias_applied"] else ""))
    pred = "; ".join(pred_parts) or "sin modelos suficientes"
    action = {
        "falla_probable": "Excluir el sensor de las alertas y programar revisión o recalibración",
        "dudoso": "Usar con cautela y confirmar con la próxima lectura antes de alertar",
        "fenomeno_local": "Confiar en el sensor: los modelos subestiman el fenómeno local; priorizar su lectura en alertas",
        "desviacion": "Vigilar la tendencia; el sensor sigue siendo utilizable",
        "consistente": "Confiar en el sensor para alertas",
        "sin_referencia": "Confiar con cautela: no hay modelos para contrastar",
    }[worst]
    return f"Lectura: {lectura}\nDiagnóstico: {diag}\nPredicción: {pred}\nAcción: {action}"


def _compact(report: dict) -> dict:
    pred = {}
    for var, p in report["prediction"].items():
        pred[var] = {"sesgo": p["bias_applied"], "serie": [r for r in p["series"] if r["h"] in (0, 1, 3, 6, 9, 12)]}
    return {
        "sensor": report["sensor"], "estado": report["status"], "confianza": report["confidence"],
        "fuentes_modelos": report["consensus_sources"],
        "chequeos": [{k: c[k] for k in ("variable", "observed", "consensus_median", "consensus_min", "consensus_max",
                                        "spread", "z", "flags", "verdict", "reason", "neighbors", "members")}
                     for c in report["checks"]],
        "prediccion": pred,
    }


async def interpret(report: dict, llm: Any | None, model: str, use_llm: bool) -> tuple[str, str]:
    if use_llm and llm is not None:
        try:
            resp = await llm.messages.create(
                model=model, max_tokens=450, system=INTERPRETER_PROMPT,
                messages=[{"role": "user", "content": json.dumps(_compact(report), ensure_ascii=False, default=str)}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
            if len(_LINE.findall(text)) == 4:
                return text, "claude"
            log.info("Interpretación del LLM fuera de formato; se usa la de reglas.")
        except Exception as exc:
            log.warning("Intérprete LLM falló: %s", exc)
    return rule_based(report), "reglas"
