"""
Configuración centralizada de AeroBot.

Todas las variables se leen desde el entorno (o desde un archivo .env en
backend/). Ninguna es obligatoria: sin ANTHROPIC_API_KEY el backend arranca
en modo "mock" (LLM simulado) para poder desarrollar el frontend sin costo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
load_dotenv(BACKEND_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass(frozen=True)
class Settings:
    # --- LLM -----------------------------------------------------------
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    max_tokens: int = int(os.getenv("AEROBOT_MAX_TOKENS", "2048"))
    max_agent_iterations: int = int(os.getenv("AEROBOT_MAX_ITERATIONS", "6"))
    # Fuerza el LLM simulado aunque exista API key (útil para tests/demo).
    force_mock_llm: bool = _bool("AEROBOT_FORCE_MOCK_LLM", False)

    # --- APIs meteorológicas --------------------------------------------
    # Open-Meteo es gratuito sin key; con key comercial se usa el host "customer-".
    open_meteo_api_key: str | None = os.getenv("OPEN_METEO_API_KEY") or None
    checkwx_api_key: str | None = os.getenv("CHECKWX_API_KEY") or None
    # Si una API falla, las herramientas devuelven datos simulados marcados
    # con source="mock" en lugar de un error duro.
    allow_mock_fallback: bool = _bool("AEROBOT_ALLOW_MOCK_FALLBACK", True)
    http_timeout_s: float = float(os.getenv("AEROBOT_HTTP_TIMEOUT", "12"))

    # --- Servidor ------------------------------------------------------
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.getenv("AEROBOT_CORS_ORIGINS", "*").split(",") if o.strip()
    )
    frontend_dir: Path = Path(os.getenv("AEROBOT_FRONTEND_DIR", str(PROJECT_DIR / "frontend")))
    max_history_messages: int = int(os.getenv("AEROBOT_MAX_HISTORY", "30"))

    # --- Alertas ---------------------------------------------------------
    # Minutos entre escaneos automáticos de los Lugares de Interés (0 = desactivado).
    alert_scan_minutes: float = float(os.getenv("AEROBOT_ALERT_SCAN_MIN", "15"))
    poi_file: Path | None = Path(os.environ["AEROBOT_POI_FILE"]) if os.getenv("AEROBOT_POI_FILE") else BACKEND_DIR / "data" / "pois.json"

    @property
    def open_meteo_base(self) -> str:
        if self.open_meteo_api_key:
            return "https://customer-api.open-meteo.com/v1/forecast"
        return "https://api.open-meteo.com/v1/forecast"

    @property
    def use_mock_llm(self) -> bool:
        return self.force_mock_llm or not self.anthropic_api_key


settings = Settings()
