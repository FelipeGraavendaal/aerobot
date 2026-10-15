"""
Motor de reglas DETERMINISTA del despachador de riesgos.

El nivel de riesgo, el lugar y la ventana de impacto los calcula este
módulo con umbrales explícitos; el LLM solo redacta la directriz. Así una
alerta roja nunca depende de la "opinión" del modelo.

Flujo:
    Telemetry (instantánea: observación o una hora de pronóstico)
        └─ hazards_from_telemetry() -> [Hazard]
    [Telemetry...] de un POI
        └─ assess() -> Assessment (nivel máx., peligros agregados, ventana)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .pois import POI

# --------------------------------------------------------------------------
# Niveles
# --------------------------------------------------------------------------
LEVELS = {
    3: {"emoji": "🔴", "label": "RIESGO ALTO", "color": "rojo"},
    2: {"emoji": "🟠", "label": "RIESGO MODERADO", "color": "naranja"},
    1: {"emoji": "🟡", "label": "PRECAUCIÓN", "color": "amarillo"},
    0: {"emoji": "🟢", "label": "SIN RIESGO", "color": "verde"},
}

KT_TO_KMH = 1.852

# Umbrales: (amarillo, naranja, rojo). Ajustables por tipo de POI más abajo.
THRESHOLDS = {
    "gust_kt": (35, 45, 55),           # ráfagas
    "dbz": (40, 50, 55),               # reflectividad (≥55: probable granizo)
    "precip_mm_h": (5, 15, 30),        # intensidad horaria
    "precip_24h_mm": (30, 60, 100),    # acumulado
    "heat_c": (32, 35, 38),
    "frost_c": (0, -2, -5),            # menor o igual
    "snow_cm_h": (0.5, 2, 5),
    "visibility_m": (3000, 1500, 600), # menor o igual
    "ceiling_ft": (1000, 500, 200),    # menor o igual (solo aeropuertos)
}

# Los aeropuertos y puertos son más sensibles al viento.
TYPE_OVERRIDES = {
    "aeropuerto": {"gust_kt": (25, 35, 45)},
    "puerto": {"gust_kt": (25, 33, 41)},
    "obra": {"gust_kt": (25, 35, 45)},
    "evento": {"gust_kt": (25, 35, 45)},
}


@dataclass
class Telemetry:
    """Variables que 'tocan' un POI en un instante. Todas opcionales."""
    valid_at: datetime
    observed: bool = False            # True = observación (METAR/radar); False = pronóstico
    source: str = "sensor"
    simulated: bool = False
    gust_kt: float | None = None
    wind_kt: float | None = None
    dbz: float | None = None
    cell_distance_km: float | None = None
    cell_eta_min: float | None = None
    thunder: bool = False
    hail: bool = False
    precip_mm_h: float | None = None
    precip_24h_mm: float | None = None
    snow_cm_h: float | None = None
    temp_c: float | None = None
    visibility_m: float | None = None
    ceiling_ft: float | None = None
    cape_jkg: float | None = None
    shear_kt: float | None = None


@dataclass
class Hazard:
    kind: str           # rafagas | tormenta | granizo | precipitacion | convectivo | visibilidad | techo | calor | helada | nieve
    level: int
    detail: str         # magnitud legible
    start: datetime     # UTC
    end: datetime       # UTC
    observed: bool
    source: str
    eta_min: float | None = None


@dataclass
class Assessment:
    poi: POI
    level: int
    hazards: list[Hazard]
    generated_at: datetime
    simulated: bool = False
    sources: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Hazard | None:
        return self.hazards[0] if self.hazards else None

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        relevant = [h for h in self.hazards if h.level == self.level]
        if not relevant:
            return None
        return min(h.start for h in relevant), max(h.end for h in relevant)


def _grade(value: float | None, thresholds: tuple, lower_is_worse: bool = False) -> int:
    if value is None:
        return 0
    y, o, r = thresholds
    if lower_is_worse:
        return 3 if value <= r else 2 if value <= o else 1 if value <= y else 0
    return 3 if value >= r else 2 if value >= o else 1 if value >= y else 0


def _th(poi: POI, key: str) -> tuple:
    return TYPE_OVERRIDES.get(poi.type, {}).get(key, THRESHOLDS[key])


def hazards_from_telemetry(t: Telemetry, poi: POI) -> list[Hazard]:
    """Aplica los umbrales a una instantánea de telemetría."""
    out: list[Hazard] = []
    start = t.valid_at
    end = t.valid_at + timedelta(hours=1)
    src = t.source + (" (simulado)" if t.simulated else "")

    def add(kind, level, detail, eta=None):
        if level > 0:
            s = start + timedelta(minutes=eta) if eta else start
            out.append(Hazard(kind, level, detail, s, max(end, s + timedelta(minutes=45)), t.observed, src, eta))

    lvl = _grade(t.gust_kt, _th(poi, "gust_kt"))
    if lvl:
        add("rafagas", lvl, f"ráfagas {t.gust_kt:.0f} kt ({t.gust_kt * KT_TO_KMH:.0f} km/h)")

    # Ecos de radar: cuentan si la celda está sobre el POI o se aproxima (ETA ≤ 120 min).
    if t.dbz is not None:
        on_site = t.cell_distance_km is None or t.cell_distance_km <= 10
        approaching = t.cell_eta_min is not None and t.cell_eta_min <= 120
        if on_site or approaching:
            lvl = _grade(t.dbz, _th(poi, "dbz"))
            where = "" if on_site else f" a {t.cell_distance_km:.0f} km, aproximándose"
            eta = None if on_site else t.cell_eta_min
            if t.dbz >= 55 or t.hail:
                add("granizo", 3, f"núcleo de {t.dbz:.0f} dBZ{where} (probable granizo)", eta)
            elif lvl:
                add("precipitacion" if t.dbz < 45 else "tormenta", lvl, f"eco de {t.dbz:.0f} dBZ{where}", eta)

    if t.hail and t.dbz is None:
        add("granizo", 3, "granizo reportado")
    if t.thunder:
        sensitive = poi.type in ("aeropuerto", "evento", "obra", "puerto")
        if t.observed:
            add("tormenta", 3 if sensitive else 2, "tormenta eléctrica observada")
        else:  # un código de tormenta en el pronóstico es potencial, no certeza
            add("tormenta", 2 if sensitive else 1, "tormenta eléctrica pronosticada")

    lvl = _grade(t.precip_mm_h, _th(poi, "precip_mm_h"))
    if lvl:
        add("precipitacion", lvl, f"lluvia de {t.precip_mm_h:.1f} mm/h")
    lvl = _grade(t.precip_24h_mm, _th(poi, "precip_24h_mm"))
    if lvl:
        add("precipitacion", lvl, f"acumulado de {t.precip_24h_mm:.0f} mm en 24 h")

    lvl = _grade(t.snow_cm_h, _th(poi, "snow_cm_h"))
    if lvl:
        add("nieve", min(3, lvl + (1 if poi.type == "ruta" else 0)), f"nevada de {t.snow_cm_h:.1f} cm/h")

    lvl = _grade(t.temp_c, _th(poi, "heat_c"))
    if lvl:
        add("calor", lvl, f"temperatura de {t.temp_c:.0f} °C")
    lvl = _grade(t.temp_c, _th(poi, "frost_c"), lower_is_worse=True)
    if lvl:
        # La helada es crítica para cultivos; en otros lugares solo implica precaución (hielo en calzada).
        lvl = min(3, lvl + 1) if poi.type == "agricola" else 1
        add("helada", lvl, f"temperatura de {t.temp_c:.1f} °C")

    lvl = _grade(t.visibility_m, _th(poi, "visibility_m"), lower_is_worse=True)
    if lvl and poi.type not in ("aeropuerto", "ruta", "puerto"):
        lvl -= 1
    if lvl and not t.observed:
        lvl = 1  # la visibilidad de los modelos es poco confiable: solo PRECAUCIÓN hasta que un METAR la confirme
    if lvl:
        add("visibilidad", lvl, f"visibilidad de {t.visibility_m:.0f} m" + ("" if t.observed else " (pronóstico)"))
    if poi.type == "aeropuerto":
        lvl = _grade(t.ceiling_ft, _th(poi, "ceiling_ft"), lower_is_worse=True)
        if lvl:
            add("techo", lvl, f"techo de nubes a {t.ceiling_ft:.0f} ft")

    # Potencial convectivo (pronóstico): nunca rojo por sí solo, porque aún no es observación.
    if t.cape_jkg is not None and not t.observed:
        shear = t.shear_kt or 0
        if t.cape_jkg >= 2500 and shear >= 40:
            add("convectivo", 2, f"CAPE {t.cape_jkg:.0f} J/kg y cizalladura {shear:.0f} kt (tormentas severas posibles)")
        elif t.cape_jkg >= 1000 and shear >= 25:
            add("convectivo", 1, f"CAPE {t.cape_jkg:.0f} J/kg y cizalladura {shear:.0f} kt (tormentas posibles)")
    return out


_SEVERITY_ORDER = ["granizo", "tormenta", "rafagas", "nieve", "precipitacion", "techo",
                   "visibilidad", "helada", "calor", "convectivo"]


def _first_peak_episode(items: list[Hazard]) -> Hazard:
    """
    Agrupa las instancias de un mismo peligro en episodios contiguos (huecos ≤ 1 h)
    y devuelve el PRIMER episodio que alcanza el nivel máximo, con su peor magnitud.
    Así una helada esta noche y otra mañana no se fusionan en una ventana de 24 h.
    """
    items = sorted(items, key=lambda h: h.start)
    peak = max(h.level for h in items)
    episodes: list[list[Hazard]] = []
    for h in items:
        if episodes and h.start <= max(x.end for x in episodes[-1]) + timedelta(hours=1):
            episodes[-1].append(h)
        else:
            episodes.append([h])
    ep = next(e for e in episodes if any(h.level == peak for h in e))
    worst = next(h for h in ep if h.level == peak)
    etas = [h.eta_min for h in ep if h.eta_min is not None]
    return Hazard(worst.kind, peak, worst.detail, min(h.start for h in ep), max(h.end for h in ep),
                  any(h.observed for h in ep), worst.source, min(etas) if etas else None)


def assess(poi: POI, snapshots: list[Telemetry], now: datetime | None = None,
           data_gaps: list[str] | None = None) -> Assessment:
    """Agrega todas las instantáneas: por tipo de peligro, primer episodio de nivel máximo."""
    now = now or datetime.now(timezone.utc)
    by_kind: dict[str, list[Hazard]] = {}
    for t in snapshots:
        for h in hazards_from_telemetry(t, poi):
            by_kind.setdefault(h.kind, []).append(h)

    hazards = sorted((_first_peak_episode(v) for v in by_kind.values()),
                     key=lambda h: (-h.level, h.start, _SEVERITY_ORDER.index(h.kind)))
    level = hazards[0].level if hazards else 0
    simulated = any(t.simulated for t in snapshots)
    sources = sorted({t.source + (" (simulado)" if t.simulated else "") for t in snapshots})
    return Assessment(poi, level, hazards, now, simulated, sources, data_gaps or [])


# --------------------------------------------------------------------------
# Bloques del mensaje (deterministas)
# --------------------------------------------------------------------------
def _fmt_delta(minutes: float) -> str:
    minutes = max(0, round(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def level_block(a: Assessment) -> str:
    lv = LEVELS[a.level]
    sim = " · SIMULACRO" if a.simulated else ""
    cause = f" · {a.primary.detail[:1].upper()}{a.primary.detail[1:]}" if a.primary else ""
    return f"[{lv['emoji']} {lv['label']}{sim}{cause}]"


def place_block(a: Assessment) -> str:
    return f"[{a.poi.name}]"


def time_block(a: Assessment) -> str:
    win = a.window
    if not win:
        return "[Sin impacto previsto]"
    tz = ZoneInfo(a.poi.timezone)
    start, end = win
    to_local = lambda d: d.astimezone(tz).strftime("%H:%M")  # noqa: E731
    mins = (start - a.generated_at).total_seconds() / 60
    day = ""
    if end.astimezone(tz).date() != a.generated_at.astimezone(tz).date():
        day = " (mañana)" if (end.astimezone(tz).date() - a.generated_at.astimezone(tz).date()).days == 1 else ""
    if mins <= 5:
        observed = any(h.observed for h in a.hazards if h.level == a.level)
        if observed:
            return f"[EN CURSO · hasta ~{to_local(end)}{day} hora local]"
        return f"[DESDE AHORA · hasta ~{to_local(end)}{day} hora local · según pronóstico]"
    return f"[{to_local(start)}–{to_local(end)}{day} hora local · inicia en {_fmt_delta(mins)}]"


# Catálogo de acciones por peligro y tipo de lugar (respaldo determinista y guía para el LLM).
ACTIONS: dict[str, dict[str, str]] = {
    "granizo": {
        "_": "Resguardar personas y vehículos bajo techo ahora; alejarse de ventanales",
        "aeropuerto": "Suspender operaciones en plataforma y llevar aeronaves a hangar; demorar despegues",
        "agricola": "Activar mallas antigranizo y retirar personal del campo",
        "evento": "Evacuar el recinto abierto hacia estructuras techadas",
    },
    "tormenta": {
        "_": "Buscar refugio bajo techo y evitar zonas abiertas y estructuras metálicas",
        "aeropuerto": "Activar alerta de rayos: suspender carga de combustible y personal en plataforma",
        "obra": "Detener grúas y trabajos en altura; evacuar andamios",
        "evento": "Suspender la actividad y evacuar escenarios y zonas abiertas",
        "puerto": "Suspender faenas de grúas y personal en muelle",
    },
    "rafagas": {
        "_": "Asegurar objetos sueltos y suspender trabajos en altura",
        "aeropuerto": "Evaluar viento cruzado por pista, preparar alterno y amarrar aeronaves livianas",
        "puerto": "Suspender maniobras de atraque y operación de grúas portacontenedores",
        "obra": "Detener grúas torre y trabajos en altura",
        "evento": "Desmontar o lastrar toldos, pantallas y estructuras temporales",
        "ruta": "Reducir velocidad; restringir vehículos altos y con remolque",
    },
    "nieve": {
        "_": "Evitar desplazamientos no esenciales",
        "ruta": "Exigir cadenas y evaluar cierre preventivo del paso",
        "aeropuerto": "Activar plan de remoción de nieve y deshielo de aeronaves",
    },
    "precipitacion": {
        "_": "Despejar sumideros y evitar pasos bajo nivel y cauces",
        "ruta": "Reducir velocidad; evitar cruces de esteros y pasos bajo nivel",
        "obra": "Proteger excavaciones y suspender hormigonado",
        "evento": "Habilitar zonas techadas y revisar instalaciones eléctricas",
    },
    "techo": {"_": "Aplicar procedimientos IFR; prever demoras de llegada y alternos"},
    "visibilidad": {
        "_": "Extremar precaución en desplazamientos",
        "aeropuerto": "Activar procedimientos de baja visibilidad (LVP)",
        "ruta": "Encender luces y aumentar distancia de seguimiento",
        "puerto": "Suspender maniobras de practicaje hasta mejorar visibilidad",
    },
    "helada": {
        "_": "Proteger cañerías y advertir hielo en calzada",
        "agricola": "Activar control de heladas (aspersión, calefactores o ventiladores) antes de la hora indicada",
        "ruta": "Esparcir sal y advertir hielo negro en calzada",
    },
    "calor": {
        "_": "Suspender esfuerzo físico al aire libre entre 12:00 y 17:00 y asegurar hidratación",
        "obra": "Reprogramar faenas pesadas fuera de 12:00–17:00 y establecer pausas de hidratación",
    },
    "convectivo": {
        "_": "Vigilar el radar cada 15 min y tener listo el plan de resguardo",
        "evento": "Preparar plan de evacuación y definir responsable de suspender la actividad",
        "aeropuerto": "Planificar combustible extra y alternos por posible convección",
    },
}


def action_for(a: Assessment) -> str:
    if not a.primary:
        return "Sin acción requerida"
    h = a.primary
    acts = ACTIONS.get(h.kind, {})
    action = acts.get(a.poi.type, acts.get("_", "Mantener vigilancia"))
    # Si hay un segundo peligro distinto del mismo nivel, se añade su acción breve.
    second = next((x for x in a.hazards[1:] if x.level == a.level and x.kind != h.kind), None)
    if second:
        extra = ACTIONS.get(second.kind, {})
        extra_action = extra.get(a.poi.type, extra.get("_"))
        if extra_action and extra_action != action:
            action += "; " + extra_action[0].lower() + extra_action[1:]
    return action


def deterministic_message(a: Assessment) -> str:
    return "\n".join([level_block(a), place_block(a), time_block(a), f"[{action_for(a)}]"])
