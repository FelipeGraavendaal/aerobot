"""
Despachador de riesgos climáticos: Assessment -> directriz humana de 4 líneas.

Contrato de salida (validado):
    [🔴 NIVEL DE RIESGO]
    [LUGAR AFECTADO]
    [TIEMPO DE IMPACTO]
    [ACCIÓN RECOMENDADA]

Las líneas 1-3 son deterministas (motor de reglas). El LLM redacta la línea 4
a partir de una acción sugerida; si su salida rompe el contrato, se REPARA
con los bloques deterministas o se usa la acción del catálogo.
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import Any

from .rules import Assessment, action_for, level_block, place_block, time_block

log = logging.getLogger("aerobot.alerts.dispatcher")

DISPATCHER_PROMPT = """Ahora actúas como el despachador de riesgos climáticos de AeroBot. Tu tarea es transformar variables de telemetría que tocan un Lugar de Interés en una directriz humana inmediata. No saludes, no uses relleno. Estructura el mensaje con: [🔴 NIVEL DE RIESGO] + [LUGAR AFECTADO] + [TIEMPO DE IMPACTO] + [ACCIÓN RECOMENDADA].

## Contrato de salida (obligatorio)
- Exactamente 4 líneas, cada una entre corchetes, en este orden. Nada antes ni después.
- Línea 1: copia EXACTA de `bloque_nivel`.
- Línea 2: copia EXACTA de `bloque_lugar`.
- Línea 3: copia EXACTA de `bloque_tiempo`.
- Línea 4: la acción. Empieza con un verbo en infinitivo, máximo 30 palabras, concreta y ejecutable por quien opera ese tipo de lugar. Sin saludos, sin "se recomienda", sin justificaciones, sin emojis, sin pronósticos adicionales.
- Parte de `accion_sugerida`. Puedes precisarla con los datos entregados (magnitudes, horas) o fusionar acciones de peligros simultáneos, pero nunca inventes datos ni cambies el nivel, el lugar o el tiempo.
- Si el nivel dice SIMULACRO, mantenlo."""

_GREETING = re.compile(r"^\[?\s*(hola|estimad|buen[oa]s|saludos|atenci[oó]n:|se recomienda|te recomiendo|recomendamos)", re.I)
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")


def _payload(a: Assessment) -> dict[str, Any]:
    return {
        "bloque_nivel": level_block(a),
        "bloque_lugar": place_block(a),
        "bloque_tiempo": time_block(a),
        "accion_sugerida": action_for(a),
        "tipo_de_lugar": a.poi.type,
        "peligros": [
            {"tipo": h.kind, "nivel": h.level, "detalle": h.detail, "observado": h.observed,
             "fuente": h.source, "eta_min": h.eta_min}
            for h in a.hazards[:4]
        ],
    }


def valid_action_line(line: str) -> bool:
    line = line.strip()
    if not (line.startswith("[") and line.endswith("]")):
        return False
    body = line[1:-1].strip()
    words = body.split()
    return 2 <= len(words) <= 35 and not _GREETING.match(body) and not _EMOJI.search(body)


def repair(raw: str, a: Assessment) -> tuple[str, bool]:
    """Fuerza el contrato. Devuelve (mensaje, usó_acción_del_llm)."""
    lines = [l.strip() for l in (raw or "").strip().splitlines() if l.strip()]
    action = lines[3] if len(lines) >= 4 else (lines[-1] if lines else "")
    used_llm = valid_action_line(action)
    if not used_llm:
        action = f"[{action_for(a)}]"
    return "\n".join([level_block(a), place_block(a), time_block(a), action]), used_llm


class Dispatcher:
    def __init__(self, llm_client: Any | None, model: str, use_llm: bool):
        self.llm = llm_client
        self.model = model
        self.use_llm = use_llm and llm_client is not None
        self._cache: OrderedDict[str, tuple[str, str]] = OrderedDict()

    @staticmethod
    def signature(a: Assessment) -> str:
        kinds = ",".join(f"{h.kind}:{h.level}" for h in a.hazards[:3])
        return f"{a.poi.id}|{a.level}|{kinds}|{time_block(a)}|{a.simulated}"

    async def dispatch(self, a: Assessment) -> tuple[str, str]:
        """Devuelve (mensaje, motor) donde motor ∈ {'claude', 'reglas'}."""
        sig = self.signature(a)
        if sig in self._cache:
            return self._cache[sig]
        result = (repair("", a)[0], "reglas")
        if self.use_llm and a.level > 0:
            try:
                resp = await self.llm.messages.create(
                    model=self.model, max_tokens=300, system=DISPATCHER_PROMPT,
                    messages=[{"role": "user", "content": json.dumps(_payload(a), ensure_ascii=False, default=str)}],
                )
                text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
                msg, used = repair(text, a)
                result = (msg, "claude" if used else "reglas")
            except Exception as exc:  # el despacho nunca falla por el LLM
                log.warning("Despachador LLM falló (%s); se usa el catálogo de reglas.", exc)
        self._cache[sig] = result
        if len(self._cache) > 300:
            self._cache.popitem(last=False)
        return result
