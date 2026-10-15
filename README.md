# AeroBot · Mapa meteorológico táctico con agente IA

Aplicación tipo *Windy* con un agente (Claude + *tool use*) que interpreta METAR/TAF, ecos de radar y modelos GFS/ECMWF, y **controla el mapa** del usuario para mostrar lo que explica.

## Estructura

```
aerobot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI: /api/chat, /ws/chat, /api/radar/frames, /api/wind-grid, estáticos
│   │   ├── config.py            # Variables de entorno (.env)
│   │   ├── schemas.py           # Contratos Pydantic (ChatRequest, ChatResponse, MapCommand…)
│   │   ├── agent/
│   │   │   ├── prompts.py       # System Prompt de AeroBot (núcleo + reglas operativas)
│   │   │   ├── tools.py         # Esquemas de las 4 tools + despachador execute_tool()
│   │   │   ├── runner.py        # Bucle agéntico: LLM -> tool_use -> ejecución -> tool_result -> LLM
│   │   │   └── mock_llm.py      # LLM simulado (misma interfaz que AsyncAnthropic) para modo demo
│   │   └── services/
│   │       ├── http.py          # httpx compartido: reintentos, backoff y caché TTL
│   │       ├── aviation.py      # METAR/TAF (AviationWeather.gov -> CheckWX -> mock)
│   │       ├── radar.py         # dBZ estimado (Z-R) + frames RainViewer
│   │       ├── models.py        # GFS vs ECMWF (Open-Meteo) + malla de viento
│   │       └── geo.py           # Haversine, destino geodésico, u/v
│   ├── tests/test_aerobot.py    # 8 tests sin red (unitarios, tools, REST y WebSocket)
│   ├── requirements.txt
│   ├── pytest.ini
│   └── .env.example
├── frontend/
│   ├── index.html               # Mapa + panel de chat
│   ├── css/styles.css           # Tema oscuro, responsive (bottom-sheet en móvil)
│   └── js/
│       ├── app.js               # Orquestación UI <-> mapa <-> agente
│       ├── map.js               # MapLibre GL: capas radar / satellite_ir / wind_particles
│       ├── wind.js              # Partículas de viento animadas en canvas
│       └── chat.js              # WebSocket con fallback REST + render Markdown/[ALERTA]
└── README.md
```

## Puesta en marcha

Requisitos: Python 3.10+.

```bash
cd aerobot/backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # agrega ANTHROPIC_API_KEY
uvicorn app.main:app --reload --port 8000
```

Abre **http://localhost:8000**. El backend sirve también el frontend (no requiere build).

- **Sin `ANTHROPIC_API_KEY`** arranca en *modo demo*: un LLM simulado decide las tools por palabras clave, pero el bucle agéntico, las APIs reales y el control del mapa funcionan igual.
- Define `ANTHROPIC_MODEL` con el modelo Claude más reciente disponible en tu cuenta.
- Tests: `cd backend && pytest -q`

## Flujo del agente

```
Usuario ─► /ws/chat ─► AeroBotAgent.run()
                          │  messages + <contexto_mapa> (centro, zoom, capa, punto seleccionado)
                          ▼
                     Claude (tools=4) ──stop_reason="tool_use"──► execute_tool() en paralelo
                          ▲                                            │
                          └──────── tool_result (JSON) ◄───────────────┘
                          │                 └─ trigger_map_layer_update emite "map_command"
                          ▼                    por WebSocket → el mapa vuela y cambia de capa
                     stop_reason="end_turn" ─► evento "final" (texto + comandos + traza)
```

- Máximo `AEROBOT_MAX_ITERATIONS` ciclos; si se agotan, se fuerza una síntesis con `tool_choice: none`.
- El historial por sesión se recorta sin romper pares `tool_use`/`tool_result`.
- `POST /api/chat` devuelve lo mismo en una sola respuesta: `{reply, map_commands[], tool_trace[], session_id, mode, usage}`.

## Fuentes de datos

| Tool | Fuente | Key | Notas |
|---|---|---|---|
| `fetch_aviation_weather` | AviationWeather.gov (NOAA) | No | Respaldo CheckWX si `CHECKWX_API_KEY`. Categoría VFR/MVFR/IFR/LIFR calculada. |
| `analyze_radar_echoes` | Open-Meteo nowcast 15 min + RainViewer | No | RainViewer entrega imágenes, no valores; el dBZ se **estima** con Z-R (Marshall-Palmer / WSR-88D convectiva) sobre 17 puntos polares. |
| `compare_atmospheric_models` | Open-Meteo (`gfs_seamless`, `ecmwf_ifs025`) | Opcional | ECMWF no publica CIN ni LI: se informa en `data_gaps`. Cizalladura bulk 10 m–500 hPa derivada. |
| Capa `satellite_ir` | NASA GIBS GOES-East ABI B13 | No | Cubre América; para Asia/Oceanía cambia a `Himawari_AHI_Band13_Clean_Infrared`. |
| Mapa base | OpenFreeMap (vectorial) | No | — |

**Resiliencia:** si una API falla, la tool devuelve datos simulados con `source: "mock"` y un `warning`; el System Prompt prohíbe emitir `[ALERTA]` con esos datos. Desactívalo con `AEROBOT_ALLOW_MOCK_FALLBACK=0`.

**Importante:** la cuota gratuita de Open-Meteo se comparte por IP y puede responder 429. En producción usa `OPEN_METEO_API_KEY` (plan comercial; también exigido para uso comercial).

## Producción (siguientes pasos)

- Sesiones en Redis en lugar de memoria; `uvicorn --workers N` detrás de Nginx con soporte WebSocket.
- Autenticación y rate limiting por usuario en `/api/chat` y `/ws/chat`.
- Muestreo de reflectividad nativa (p. ej. MRMS o radares nacionales) para reemplazar el dBZ estimado.
- AeroBot es una herramienta de apoyo: no reemplaza la información meteorológica oficial para la operación aérea.
