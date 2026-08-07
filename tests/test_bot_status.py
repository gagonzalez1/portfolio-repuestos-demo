"""Tests unitarios de app/bot_status.py.

Sin DB ni red. Inyectamos datetime sintético para evitar dependencia del reloj.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_status import (  # noqa: E402
    STATUS_OFF_HORARIO,
    STATUS_OFF_KILL,
    STATUS_ON,
    TZ_BUENOS_AIRES,
    default_config,
    evaluate_status,
    invalidate_cache,
    set_cache,
    get_cached_config_sync,
    validate_config_payload,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _ar(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """datetime timezone-aware en horario argentino."""
    return datetime(year, month, day, hour, minute, tzinfo=TZ_BUENOS_AIRES)


def _config_basico() -> dict:
    """Config típico: bot encendido lun-vie 08:30-13:00 y 14:00-17:00, sáb y dom cerrados."""
    return {
        "kill_switch": False,
        "respond_when_off": True,
        "off_message": "Estamos cerrados, te respondo apenas reabrimos.",
        "schedule": {
            "mon": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
            "tue": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
            "wed": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
            "thu": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
            "fri": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
            "sat": [],
            "sun": [],
        },
    }


# ── evaluate_status ───────────────────────────────────────────────────────

def test_dentro_de_horario_mañana_lunes():
    """Lunes 10:00 → ON."""
    status, msg = evaluate_status(_config_basico(), _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_ON
    assert msg is None


def test_dentro_de_horario_tarde_miercoles():
    """Miércoles 15:30 → ON."""
    status, msg = evaluate_status(_config_basico(), _ar(2026, 5, 13, 15, 30))
    assert status == STATUS_ON
    assert msg is None


def test_borde_inicio_inclusivo():
    """Lunes 08:30 → ON (start inclusive)."""
    status, _ = evaluate_status(_config_basico(), _ar(2026, 5, 11, 8, 30))
    assert status == STATUS_ON


def test_borde_fin_exclusivo():
    """Lunes 13:00 → OFF (end exclusive)."""
    status, _ = evaluate_status(_config_basico(), _ar(2026, 5, 11, 13, 0))
    assert status == STATUS_OFF_HORARIO


def test_durante_siesta_entre_bloques():
    """Lunes 13:30 (entre los dos bloques) → OFF."""
    status, msg = evaluate_status(_config_basico(), _ar(2026, 5, 11, 13, 30))
    assert status == STATUS_OFF_HORARIO
    assert msg is not None and "cerrados" in msg.lower()


def test_madrugada_fuera_de_rango():
    """Lunes 03:00 → OFF."""
    status, _ = evaluate_status(_config_basico(), _ar(2026, 5, 11, 3, 0))
    assert status == STATUS_OFF_HORARIO


def test_sabado_sin_rangos_cerrado():
    """Sábado 10:00 → OFF (día sin rangos)."""
    status, _ = evaluate_status(_config_basico(), _ar(2026, 5, 16, 10, 0))
    assert status == STATUS_OFF_HORARIO


def test_domingo_sin_rangos_cerrado():
    """Domingo 12:00 → OFF."""
    status, _ = evaluate_status(_config_basico(), _ar(2026, 5, 17, 12, 0))
    assert status == STATUS_OFF_HORARIO


def test_kill_switch_silencio_aunque_sea_horario():
    """Lunes 10:00 + kill_switch=True + respond_when_off=False → OFF_KILL sin mensaje.

    El test original asumía respond_when_off implícito; lo dejamos explícito
    porque ahora respond_when_off=True con kill_switch=True manda off_message.
    """
    cfg = _config_basico()
    cfg["kill_switch"] = True
    cfg["respond_when_off"] = False
    status, msg = evaluate_status(cfg, _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_OFF_KILL
    assert msg is None


# ── Tabla completa de las 6 combinaciones (ver bot_status.py docstring) ──

def test_combinatoria_1_normal_dentro_horario():
    """Caso #1: kill=OFF + respond=OFF + dentro de horario → LLM normal."""
    cfg = _config_basico()
    cfg["respond_when_off"] = False
    status, msg = evaluate_status(cfg, _ar(2026, 5, 11, 10, 0))  # lunes 10:00
    assert status == STATUS_ON
    assert msg is None


def test_combinatoria_2_normal_fuera_horario_sin_mensaje():
    """Caso #2: kill=OFF + respond=OFF + fuera de horario → silencio."""
    cfg = _config_basico()
    cfg["respond_when_off"] = False
    status, msg = evaluate_status(cfg, _ar(2026, 5, 17, 12, 0))  # domingo
    assert status == STATUS_OFF_HORARIO
    assert msg is None


def test_combinatoria_3_normal_dentro_horario_con_respond():
    """Caso #3: kill=OFF + respond=ON + dentro de horario → LLM normal (no usa off_msg)."""
    cfg = _config_basico()
    cfg["respond_when_off"] = True
    status, msg = evaluate_status(cfg, _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_ON
    assert msg is None


def test_combinatoria_4_normal_fuera_horario_con_respond():
    """Caso #4: kill=OFF + respond=ON + fuera de horario → manda off_message."""
    cfg = _config_basico()
    cfg["respond_when_off"] = True
    status, msg = evaluate_status(cfg, _ar(2026, 5, 17, 12, 0))
    assert status == STATUS_OFF_HORARIO
    assert msg == cfg["off_message"]


def test_combinatoria_5_apagado_sin_respond():
    """Caso #5: kill=ON + respond=OFF → silencio absoluto en cualquier horario."""
    cfg = _config_basico()
    cfg["kill_switch"] = True
    cfg["respond_when_off"] = False
    for now in [_ar(2026, 5, 11, 10, 0), _ar(2026, 5, 17, 12, 0)]:
        status, msg = evaluate_status(cfg, now)
        assert status == STATUS_OFF_KILL
        assert msg is None


def test_combinatoria_6_apagado_con_respond():
    """Caso #6: kill=ON + respond=ON → manda off_message en cualquier horario."""
    cfg = _config_basico()
    cfg["kill_switch"] = True
    cfg["respond_when_off"] = True
    for now in [_ar(2026, 5, 11, 10, 0), _ar(2026, 5, 17, 12, 0)]:
        status, msg = evaluate_status(cfg, now)
        assert status == STATUS_OFF_KILL
        assert msg == cfg["off_message"]


def test_combinatoria_6_off_message_vacio_fallback_silencio():
    """Caso #6 edge: si off_message está vacío, fallback a silencio."""
    cfg = _config_basico()
    cfg["kill_switch"] = True
    cfg["respond_when_off"] = True
    cfg["off_message"] = "   "
    status, msg = evaluate_status(cfg, _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_OFF_KILL
    assert msg is None


def test_respond_when_off_false_no_responde():
    """Fuera de horario + respond_when_off=False → STATUS_OFF_HORARIO sin mensaje."""
    cfg = _config_basico()
    cfg["respond_when_off"] = False
    status, msg = evaluate_status(cfg, _ar(2026, 5, 17, 12, 0))  # domingo
    assert status == STATUS_OFF_HORARIO
    assert msg is None


def test_respond_when_off_true_responde_con_off_message():
    """Fuera de horario + respond_when_off=True → manda off_message."""
    cfg = _config_basico()
    status, msg = evaluate_status(cfg, _ar(2026, 5, 17, 12, 0))
    assert status == STATUS_OFF_HORARIO
    assert msg == cfg["off_message"]


def test_off_message_vacio_no_manda_string_vacio():
    """Si off_message está vacío, silencio (no mandar '')."""
    cfg = _config_basico()
    cfg["off_message"] = ""
    status, msg = evaluate_status(cfg, _ar(2026, 5, 17, 12, 0))
    assert status == STATUS_OFF_HORARIO
    assert msg is None


def test_config_vacio_modo_paranoid():
    """Sin config → tratar como kill switch (no responder)."""
    status, msg = evaluate_status(None, _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_OFF_KILL
    assert msg is None

    status, msg = evaluate_status({}, _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_OFF_KILL
    assert msg is None


def test_rangos_multiples_mismo_dia():
    """Día con 3 rangos: 08-10, 12-14, 16-20. Probar cada uno."""
    cfg = _config_basico()
    cfg["schedule"]["mon"] = [
        {"from": "08:00", "to": "10:00"},
        {"from": "12:00", "to": "14:00"},
        {"from": "16:00", "to": "20:00"},
    ]
    # En cada bloque
    assert evaluate_status(cfg, _ar(2026, 5, 11, 9, 0))[0] == STATUS_ON
    assert evaluate_status(cfg, _ar(2026, 5, 11, 13, 0))[0] == STATUS_ON
    assert evaluate_status(cfg, _ar(2026, 5, 11, 18, 0))[0] == STATUS_ON
    # Fuera
    assert evaluate_status(cfg, _ar(2026, 5, 11, 11, 0))[0] == STATUS_OFF_HORARIO
    assert evaluate_status(cfg, _ar(2026, 5, 11, 15, 0))[0] == STATUS_OFF_HORARIO
    assert evaluate_status(cfg, _ar(2026, 5, 11, 21, 0))[0] == STATUS_OFF_HORARIO


def test_rango_invalido_se_ignora_no_levanta():
    """Rango con from >= to debe ser ignorado sin romper el webhook."""
    cfg = _config_basico()
    cfg["schedule"]["mon"] = [
        {"from": "17:00", "to": "08:00"},  # inválido (cruzaría medianoche)
        {"from": "10:00", "to": "12:00"},  # válido
    ]
    # 11:00 cae en el rango válido
    assert evaluate_status(cfg, _ar(2026, 5, 11, 11, 0))[0] == STATUS_ON
    # 18:00 fuera de cualquier rango válido
    assert evaluate_status(cfg, _ar(2026, 5, 11, 18, 0))[0] == STATUS_OFF_HORARIO


def test_naive_datetime_asumido_en_AR():
    """datetime sin tzinfo se asume en TZ AR."""
    cfg = _config_basico()
    naive = datetime(2026, 5, 11, 10, 0)  # lunes 10am sin tz
    status, _ = evaluate_status(cfg, naive)
    assert status == STATUS_ON


def test_datetime_otro_tz_se_convierte():
    """datetime en otro TZ se convierte a AR antes de evaluar."""
    cfg = _config_basico()
    # Lunes 13:00 UTC = lunes 10:00 AR (durante el horario)
    utc_dt = datetime(2026, 5, 11, 13, 0, tzinfo=ZoneInfo("UTC"))
    status, _ = evaluate_status(cfg, utc_dt)
    assert status == STATUS_ON


# ── default_config ────────────────────────────────────────────────────────

def test_default_config_starts_available():
    """Default arranca APAGADO (paranoid). Nico tiene que encender explícitamente."""
    cfg = default_config()
    assert cfg["kill_switch"] is False
    assert cfg["respond_when_off"] is True
    assert "schedule" in cfg


def test_default_config_es_evaluable():
    """El default no rompe evaluate_status."""
    status, _ = evaluate_status(default_config(), _ar(2026, 5, 11, 10, 0))
    assert status == STATUS_ON


# ── Cache ─────────────────────────────────────────────────────────────────

def test_cache_set_y_get():
    invalidate_cache()
    assert get_cached_config_sync() is None
    set_cache({"kill_switch": False})
    assert get_cached_config_sync() == {"kill_switch": False}


def test_cache_invalidate():
    set_cache({"kill_switch": False})
    invalidate_cache()
    assert get_cached_config_sync() is None


# ── validate_config_payload ───────────────────────────────────────────────

def test_validate_payload_basico_ok():
    ok, err, cfg = validate_config_payload(_config_basico())
    assert ok is True
    assert err is None
    assert cfg["kill_switch"] is False
    assert cfg["schedule"]["mon"] == [
        {"from": "08:30", "to": "13:00"},
        {"from": "14:00", "to": "17:00"},
    ]


def test_validate_payload_no_es_dict():
    ok, err, _ = validate_config_payload("nope")
    assert ok is False
    assert "objeto" in err


def test_validate_payload_kill_switch_no_bool():
    payload = _config_basico()
    payload["kill_switch"] = "true"
    ok, err, _ = validate_config_payload(payload)
    assert ok is False
    assert "kill_switch" in err


def test_validate_payload_rango_invalido_from_mayor_que_to():
    payload = _config_basico()
    payload["schedule"]["mon"] = [{"from": "13:00", "to": "08:00"}]
    ok, err, _ = validate_config_payload(payload)
    assert ok is False
    assert "menor" in err.lower()


def test_validate_payload_rangos_solapados_misma_dia():
    payload = _config_basico()
    payload["schedule"]["mon"] = [
        {"from": "08:00", "to": "12:00"},
        {"from": "11:00", "to": "14:00"},  # se solapa con el anterior
    ]
    ok, err, _ = validate_config_payload(payload)
    assert ok is False
    assert "solapados" in err.lower()


def test_validate_payload_hora_invalida():
    payload = _config_basico()
    payload["schedule"]["mon"] = [{"from": "25:00", "to": "26:00"}]
    ok, err, _ = validate_config_payload(payload)
    assert ok is False
    assert "horas inválidas" in err.lower() or "invalida" in err.lower()


def test_validate_payload_off_message_demasiado_largo():
    payload = _config_basico()
    payload["off_message"] = "x" * 2001
    ok, err, _ = validate_config_payload(payload)
    assert ok is False
    assert "2000" in err


def test_validate_payload_dia_vacio_ok():
    payload = _config_basico()
    payload["schedule"]["sat"] = []
    ok, err, cfg = validate_config_payload(payload)
    assert ok is True
    assert cfg["schedule"]["sat"] == []


# ── Runner standalone ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError:
            fails += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
        except Exception:
            fails += 1
            print(f"  ERROR {t.__name__}")
            traceback.print_exc()
    print()
    print(f"{len(tests) - fails}/{len(tests)} passing")
    sys.exit(0 if fails == 0 else 1)
