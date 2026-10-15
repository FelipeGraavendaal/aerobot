"""
Cliente HTTP compartido (httpx.AsyncClient) con:
  - reintentos con backoff ante errores de red / 429 / 5xx,
  - caché en memoria con TTL para no martillar APIs gratuitas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("aerobot.http")

_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_MAX = 512


class UpstreamError(RuntimeError):
    """Error al consultar una API externa (tras agotar reintentos)."""


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_s),
            headers={"User-Agent": "AeroBot/1.0 (+https://github.com/sociale/aerobot)"},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _cache_key(url: str, params: dict | None) -> str:
    return url + "?" + json.dumps(params or {}, sort_keys=True, default=str)


async def get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    ttl: float = 0,
    retries: int = 2,
) -> Any:
    """GET que devuelve JSON (o None si la respuesta viene vacía / 204)."""
    key = _cache_key(url, params)
    if ttl > 0:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await get_client().get(url, params=params, headers=headers)
            if resp.status_code == 204 or not resp.content.strip():
                data = None
            elif resp.status_code in (429,) or resp.status_code >= 500:
                raise UpstreamError(f"{resp.status_code} {resp.text[:200]}")
            else:
                resp.raise_for_status()
                data = resp.json()
            if ttl > 0:
                if len(_cache) >= _CACHE_MAX:
                    _cache.pop(next(iter(_cache)))
                _cache[key] = (time.monotonic() + ttl, data)
            return data
        except (httpx.TransportError, UpstreamError) as exc:
            last_exc = exc
            log.warning("GET %s falló (intento %d): %s", url, attempt + 1, exc)
            if attempt < retries:
                await asyncio.sleep(0.6 * (2**attempt))
        except (httpx.HTTPStatusError, ValueError) as exc:  # 4xx o JSON inválido: no reintentar
            raise UpstreamError(f"{url}: {exc}") from exc
    raise UpstreamError(f"{url}: {last_exc}")
