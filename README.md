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
│   ├── app/alerts/              # POIs, motor de reglas, recolector, despachador, simulacros
│   ├── tests/                   # 18 tests sin red (agente, tools, alertas, REST y WebSocket)
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

## Alertas: despachador de riesgos climáticos

Transforma la telemetría que toca un **Lugar de Interés (POI)** en una directriz inmediata de 4 líneas:

```
[🔴 RIESGO ALTO · Núcleo de 57 dBZ a 14 km, aproximándose (probable granizo)]
[Aeropuerto AMB (SCEL), Pudahuel]
[15:25–16:30 hora local · inicia en 25 min]
[Suspender operaciones en plataforma y llevar aeronaves a hangar; demorar despegues]
```

- **Nivel, lugar y tiempo son deterministas** (`alerts/rules.py`): umbrales explícitos por tipo de lugar (aeropuerto, puerto, obra, evento, ruta, agrícola, urbano). El LLM solo redacta la acción; si rompe el contrato (saludos, emojis, >35 palabras, líneas alteradas) se repara con los bloques del motor o con el catálogo de acciones.
- **Niveles:** 🔴 RIESGO ALTO · 🟠 RIESGO MODERADO · 🟡 PRECAUCIÓN. El potencial convectivo de modelos nunca llega a rojo y la visibilidad pronosticada se limita a PRECAUCIÓN hasta que un METAR la confirme.
- **Tiempo de impacto:** `EN CURSO` solo con observación (METAR/radar); los pronósticos dicen `DESDE AHORA … según pronóstico` o la ventana en hora local. Las celdas de radar calculan ETA con el viento rector de 700 hPa.
- **Escaneo automático** cada `AEROBOT_ALERT_SCAN_MIN` minutos (15 por defecto) con solo 2 peticiones a Open-Meteo para todos los POI + METAR de aeropuertos. Nunca se generan alertas reales con datos simulados: las fuentes caídas se reportan en `data_gaps`.
- **Simulacros** etiquetados `SIMULACRO` para probar la cadena completa.

| Endpoint | Uso |
|---|---|
| `GET /api/alerts` · `POST /api/alerts/scan` | Último escaneo / forzar escaneo |
| `POST /api/alerts/evaluate` | Telemetría externa (sensor, grúa, estación propia) -> directriz |
| `GET /api/alerts/scenarios` · `POST /api/alerts/simulate/{id}` | Simulacros |
| `GET/POST /api/pois` · `DELETE /api/pois/{id}` | Lugares de Interés |

Ejemplo de integración (anemómetro de una grúa):

```bash
curl -X POST https://TU-APP/api/alerts/evaluate -H 'content-type: application/json' \
  -d '{"poi_id":"stgo-centro","telemetry":[{"gust_kt":47,"source":"anemómetro grúa 2"}]}'
```

En el chat, AeroBot usa la herramienta `dispatch_risk_alert` ("¿qué alerta hay para el aeropuerto de Santiago?").

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
