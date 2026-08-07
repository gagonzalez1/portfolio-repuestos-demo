"""Lógica de estado del bot — encendido / apagado / fuera de horario.

Este módulo es PURO (sin DB ni red). Recibe un dict de config y un datetime,
y decide si el bot debe responder normalmente, mandar un mensaje OFF o quedarse
en silencio.

La fuente de verdad del config vive en la tabla `agent_config` (manejada por
`app/admin_params.py`). El webhook llama `evaluate_status(config, now)` antes de
procesar el mensaje del cliente.

Hay un cache opcional en memoria (`get_cached_config` / `invalidate_cache`) para
no pegarle a Postgres en cada webhook. TTL 30 s — aceptable si Nico hace un
cambio en el panel: el siguiente mensaje del cliente puede caer en la ventana
de 30 s vieja, pero se autocorrige rápido. Si Railway escala a múltiples
workers, cada uno tendrá su propio cache (misma ventana de 30 s).

Formato del config esperado (JSONB en agent_config.config):

    {
      "kill_switch": false,
      "respond_when_off": true,
      "off_message": "Estamos cerrados. Te respondemos apenas reabrimos.",
      "schedule": {
        "mon": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "tue": [...],
        ...
        "sun": []
      }
    }

Días sin rangos → cerrado todo el día. Rangos con from >= to son ignorados
silenciosamente (validar en el form al guardar; acá no levantamos excepción
para no romper el webhook por un config malo).

─────────────────────────────────────────────────────────────────────────────
SEMÁNTICA DE LOS DOS SWITCHES (acordada con el dueño, 13-may-2026):

    "Apagar bot" (kill_switch)        — desactiva la inteligencia. El bot no
                                        usa el LLM ni busca productos. No
                                        gasta tokens.
    "Enviar mensaje automático"       — decide si manda el off_message
    (respond_when_off)                  cuando el bot NO está atendiendo
                                        activamente (sea por kill switch o
                                        por estar fuera de horario).

Tabla de las 6 combinaciones:

    # | Apagar bot | Enviar mensaje | Horario  | Comportamiento
    --+------------+----------------+----------+----------------------------
    1 |    OFF     |     OFF        | dentro   | LLM normal (busca repuestos)
    2 |    OFF     |     OFF        | fuera    | Silencio total
    3 |    OFF     |     ON         | dentro   | LLM normal (no usa off_msg)
    4 |    OFF     |     ON         | fuera    | Manda off_message
    5 |    ON      |     OFF        | cualquiera| Silencio total
    6 |    ON      |     ON         | cualquiera| Manda off_message

En las 6 combinaciones, el mensaje del cliente SIEMPRE se guarda en la DB
para que el dueño lo vea cuando vuelva al panel.

`evaluate_status()` retorna (status, message_to_send). El webhook usa el
status para decidir si llama o no al agente, y `message_to_send` para
decidir qué texto mandar al cliente (None = silencio).
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ── Constantes ─────────────────────────────────────────────────────────────

TZ_BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
CACHE_TTL_SECONDS = 30

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# Estados posibles. Devolverlos como string para que sea fácil chequear desde
# el webhook (no necesita importar el módulo para comparar).
STATUS_ON = "on"
STATUS_OFF_KILL = "off_kill"
STATUS_OFF_HORARIO = "off_horario"


# ── Defaults ───────────────────────────────────────────────────────────────

def default_config() -> dict[str, Any]:
    """Config inicial cuando la tabla está recién creada.

    La demo pública arranca disponible 24/7. El operador conserva dos cortes:
    ``DEMO_ENABLED`` a nivel de entorno y este kill switch desde el panel.
    """
    return {
        "kill_switch": False,
        "respond_when_off": True,
        "off_message": (
            "La demostración está pausada temporalmente. "
            "Podés volver a intentarlo más tarde."
        ),
        "schedule": {
            day: [{"from": "00:00", "to": "23:59"}] for day in _DAY_KEYS
        },
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_hhmm(text: str) -> dtime | None:
    """Parsea 'HH:MM' a datetime.time. Retorna None si no matchea."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return dtime(h, m)


def _day_key_for(now: datetime) -> str:
    """Devuelve la clave del día ('mon', 'tue', ...) según el weekday."""
    return _DAY_KEYS[now.weekday()]


def _is_within_any_range(now_time: dtime, ranges: list[dict]) -> bool:
    """¿La hora actual cae dentro de algún rango de la lista?"""
    for r in ranges or []:
        start = _parse_hhmm(r.get("from", ""))
        end = _parse_hhmm(r.get("to", ""))
        if start is None or end is None:
            continue
        if start >= end:
            # Rango inválido (cerrado o cruzando medianoche). Ignorar.
            # No soportamos cruce de medianoche para mantener la UI simple;
            # si Nico necesita "domingo 22:00 a lunes 02:00" lo puede
            # dividir en "domingo 22:00-23:59" + "lunes 00:00-02:00".
            continue
        if start <= now_time < end:
            return True
    return False


# ── Función principal ─────────────────────────────────────────────────────

def evaluate_status(
    config: dict[str, Any] | None,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """Decide el estado del bot dado un config y un datetime.

    Retorna una tupla:
        (status, message_to_send)

    Donde:
        - status: STATUS_ON / STATUS_OFF_KILL / STATUS_OFF_HORARIO
        - message_to_send: texto a enviar al cliente, o None si silencio total.

    Reglas:
        1. Config faltante o vacío → tratar como kill switch (modo paranoid).
        2. kill_switch=true: el bot NO ejecuta al agente (no gasta tokens, no
           busca productos, no contesta nada inteligente). Pero si
           respond_when_off=true y hay off_message, manda ese mensaje
           automático — útil para avisar "estamos pausados" sin tener que
           apagar el "mensaje fuera de horario". Si respond_when_off=false →
           silencio total.
        3. Si el horario actual cae dentro de algún rango del día → STATUS_ON.
        4. Si está fuera de horario y respond_when_off=true → mandar off_message.
        5. Si está fuera de horario y respond_when_off=false → silencio.

    El `now` por default es timezone-aware en TZ Argentina; pasalo explícito
    en tests con datetime sintético para evitar dependencia del reloj.
    """
    if not config:
        # Sin config seguimos seguros: no respondemos. Mejor mudo que con
        # comportamiento indefinido. El admin tiene que cargar config.
        return STATUS_OFF_KILL, None

    # Helper: obtener el mensaje OFF si está habilitado a nivel global.
    def _off_message_or_silence() -> str | None:
        if config.get("respond_when_off") is True:
            msg = (config.get("off_message") or "").strip()
            return msg or None
        return None

    if config.get("kill_switch") is True:
        return STATUS_OFF_KILL, _off_message_or_silence()

    if now is None:
        now = datetime.now(tz=TZ_BUENOS_AIRES)
    elif now.tzinfo is None:
        # naive datetime → asumirlo en TZ AR (típicamente vienen de tests)
        now = now.replace(tzinfo=TZ_BUENOS_AIRES)
    else:
        now = now.astimezone(TZ_BUENOS_AIRES)

    schedule = config.get("schedule") or {}
    day_key = _day_key_for(now)
    ranges = schedule.get(day_key, [])

    if _is_within_any_range(now.time(), ranges):
        return STATUS_ON, None

    # Fuera de horario: usa el mismo helper.
    msg = _off_message_or_silence()
    if msg is None and config.get("respond_when_off") is True:
        logger.warning(
            "[bot_status] respond_when_off=true pero off_message está vacío — silencio"
        )
    return STATUS_OFF_HORARIO, msg


# ── Cache en memoria ──────────────────────────────────────────────────────

# Un solo slot. El config es global (una sola tienda).
_cache: dict[str, Any] = {"config": None, "expires_at": 0.0}


def get_cached_config_sync() -> dict[str, Any] | None:
    """Devuelve el config cacheado si todavía es válido, o None si expiró.

    El llamador es responsable de leer DB y cachear con `set_cache` cuando esto
    devuelve None. Mantengo la lectura de DB fuera de este módulo para que
    siga siendo testeable sin dependencias.
    """
    if _cache["config"] is not None and time.monotonic() < _cache["expires_at"]:
        return _cache["config"]
    return None


def set_cache(config: dict[str, Any]) -> None:
    """Guarda el config en cache por CACHE_TTL_SECONDS."""
    _cache["config"] = config
    _cache["expires_at"] = time.monotonic() + CACHE_TTL_SECONDS


def invalidate_cache() -> None:
    """Limpia el cache. Llamar después de un save en el panel admin."""
    _cache["config"] = None
    _cache["expires_at"] = 0.0


# ── Validación del payload (para usar desde el POST del admin) ────────────

def validate_config_payload(payload: Any) -> tuple[bool, str | None, dict | None]:
    """Valida que un payload recibido del frontend sea un config válido.

    Retorna (ok, error_msg, normalized_config).

    Reglas:
        - top-level keys requeridas: kill_switch (bool), respond_when_off (bool),
          off_message (str), schedule (dict con 7 días)
        - cada día es lista de {from, to} con HH:MM válidos
        - en cada día, no debe haber rangos solapados
        - rangos donde from >= to → error
    """
    if not isinstance(payload, dict):
        return False, "payload debe ser un objeto", None

    out: dict[str, Any] = {}

    # kill_switch
    ks = payload.get("kill_switch", False)
    if not isinstance(ks, bool):
        return False, "kill_switch debe ser booleano", None
    out["kill_switch"] = ks

    # respond_when_off
    rwo = payload.get("respond_when_off", True)
    if not isinstance(rwo, bool):
        return False, "respond_when_off debe ser booleano", None
    out["respond_when_off"] = rwo

    # off_message
    msg = payload.get("off_message", "")
    if not isinstance(msg, str):
        return False, "off_message debe ser texto", None
    if len(msg) > 2000:
        return False, "off_message excede 2000 caracteres", None
    out["off_message"] = msg.strip()

    # schedule
    sched = payload.get("schedule") or {}
    if not isinstance(sched, dict):
        return False, "schedule debe ser un objeto", None
    normalized_sched: dict[str, list[dict]] = {}
    for day_key in _DAY_KEYS:
        ranges = sched.get(day_key, [])
        if not isinstance(ranges, list):
            return False, f"schedule.{day_key} debe ser lista", None
        normalized_ranges: list[dict] = []
        # Convertir y validar cada rango
        parsed: list[tuple[dtime, dtime]] = []
        for i, r in enumerate(ranges):
            if not isinstance(r, dict):
                return False, f"schedule.{day_key}[{i}] debe ser objeto", None
            start = _parse_hhmm(r.get("from", ""))
            end = _parse_hhmm(r.get("to", ""))
            if start is None or end is None:
                return False, f"schedule.{day_key}[{i}] horas inválidas", None
            if start >= end:
                return (
                    False,
                    f"schedule.{day_key}[{i}] 'desde' debe ser menor que 'hasta'",
                    None,
                )
            parsed.append((start, end))
            normalized_ranges.append(
                {"from": start.strftime("%H:%M"), "to": end.strftime("%H:%M")}
            )
        # Chequear overlap: ordenar por inicio y verificar
        parsed_sorted = sorted(parsed, key=lambda t: t[0])
        for i in range(1, len(parsed_sorted)):
            prev_end = parsed_sorted[i - 1][1]
            cur_start = parsed_sorted[i][0]
            if cur_start < prev_end:
                return (
                    False,
                    f"schedule.{day_key} tiene rangos solapados",
                    None,
                )
        normalized_sched[day_key] = normalized_ranges
    out["schedule"] = normalized_sched

    return True, None, out
