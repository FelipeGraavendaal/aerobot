"""
Bucle agéntico de AeroBot (Anthropic Messages API + tool use).

Flujo por turno:
    usuario ──► LLM ──(stop_reason == "tool_use")──► backend ejecuta tools en paralelo
                 ▲                                            │
                 └────────── tool_result (JSON) ◄─────────────┘
    ... se repite hasta que el LLM responde texto final (stop_reason "end_turn")
    o se alcanza max_iterations (entonces se fuerza una respuesta sin tools).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .prompts import SYSTEM_PROMPT
from .tools import TOOL_DEFINITIONS, EventCallback, ToolContext, execute_tool

log = logging.getLogger("aerobot.agent")


@dataclass
class AgentResult:
    reply: str
    map_commands: list[dict] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)
    iterations: int = 0
    usage: dict = field(default_factory=dict)


def build_user_content(message: str, map_state: dict | None) -> str:
    """Adjunta el contexto del mapa al mensaje para que 'aquí' tenga significado."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not map_state:
        return f"{message}\n\n<contexto_mapa>hora_actual={now}; sin estado de mapa</contexto_mapa>"
    parts = [
        f"hora_actual={now}",
        f"centro_lat={map_state.get('lat'):.4f}" if map_state.get("lat") is not None else None,
        f"centro_lon={map_state.get('lon'):.4f}" if map_state.get("lon") is not None else None,
        f"zoom={map_state.get('zoom'):.1f}" if map_state.get("zoom") is not None else None,
        f"capa_activa={map_state.get('active_layer') or 'none'}",
    ]
    sel = map_state.get("selected_point")
    if sel:
        parts.append(f"punto_seleccionado_lat={sel['lat']:.4f}; punto_seleccionado_lon={sel['lon']:.4f}")
    return f"{message}\n\n<contexto_mapa>{'; '.join(p for p in parts if p)}</contexto_mapa>"


def _block_type(block: Any) -> str:
    return block["type"] if isinstance(block, dict) else block.type


def _extract_text(content: list[Any]) -> str:
    texts = [(b["text"] if isinstance(b, dict) else b.text) for b in content if _block_type(b) == "text"]
    return "\n".join(t for t in texts if t).strip()


class AeroBotAgent:
    def __init__(self, llm_client: Any, model: str, max_tokens: int = 2048, max_iterations: int = 6):
        # llm_client: anthropic.AsyncAnthropic o MockAnthropic (misma interfaz .messages.create)
        self.llm = llm_client
        self.model = model
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations

    async def _call_llm(self, messages: list[dict], allow_tools: bool = True):
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS, messages=messages,
        )
        if not allow_tools:
            kwargs["tool_choice"] = {"type": "none"}
        return await self.llm.messages.create(**kwargs)

    async def run(self, history: list[dict], message: str, map_state: dict | None = None,
                  on_event: EventCallback | None = None) -> tuple[AgentResult, list[dict]]:
        """
        Ejecuta un turno completo. No muta `history`: devuelve el historial
        nuevo para que el llamador lo persista solo si todo salió bien.
        """
        ctx = ToolContext(on_event=on_event)
        messages = list(history) + [{"role": "user", "content": build_user_content(message, map_state)}]
        usage = {"input_tokens": 0, "output_tokens": 0}
        reply = ""

        for iteration in range(1, self.max_iterations + 1):
            await ctx.emit({"type": "thinking", "iteration": iteration})
            response = await self._call_llm(messages)
            if getattr(response, "usage", None):
                usage["input_tokens"] += getattr(response.usage, "input_tokens", 0) or 0
                usage["output_tokens"] += getattr(response.usage, "output_tokens", 0) or 0

            # El turno del asistente se guarda íntegro (texto + bloques tool_use).
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if _block_type(b) == "tool_use"]

            if response.stop_reason != "tool_use" or not tool_uses:
                reply = _extract_text(response.content)
                break

            # Texto intermedio (p. ej. "Voy a revisar el radar…") se emite como progreso.
            interim = _extract_text(response.content)
            if interim:
                await ctx.emit({"type": "interim_text", "text": interim})

            # Ejecuta todas las herramientas solicitadas en paralelo.
            results = await asyncio.gather(*(execute_tool(b.name, dict(b.input or {}), ctx) for b in tool_uses))
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(res, ensure_ascii=False, default=str),
                        "is_error": is_err,
                    }
                    for b, (res, is_err) in zip(tool_uses, results)
                ],
            })
        else:
            # Se agotaron las iteraciones: pedimos una síntesis final sin herramientas.
            log.warning("Máximo de iteraciones alcanzado; forzando respuesta final.")
            response = await self._call_llm(messages, allow_tools=False)
            messages.append({"role": "assistant", "content": response.content})
            reply = _extract_text(response.content)
            iteration = self.max_iterations + 1

        result = AgentResult(
            reply=reply or "No pude generar una respuesta. Intenta reformular la consulta.",
            map_commands=ctx.map_commands, tool_trace=ctx.trace, iterations=iteration, usage=usage,
        )
        return result, messages


def trim_history(messages: list[dict], max_messages: int) -> list[dict]:
    """
    Recorta el historial sin romper pares tool_use/tool_result: solo corta en
    un mensaje de usuario de texto plano (inicio de un turno).
    """
    if len(messages) <= max_messages:
        return messages
    start = len(messages) - max_messages
    while start < len(messages):
        m = messages[start]
        if m["role"] == "user" and isinstance(m["content"], str):
            return messages[start:]
        start += 1
    return []
