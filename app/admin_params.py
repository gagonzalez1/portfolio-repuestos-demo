"""Panel admin de parámetros del bot — /admin/params.

Lo que hace:
    - Una sola tabla `agent_config` (id=1, JSONB con la configuración).
    - Una pantalla web con tres bloques: encendido/apagado total,
      mensaje "fuera de horario" + sub-toggle "responder", y horarios
      por día de semana con lista variable de rangos.
    - Login separado del demo: env var `ADMIN_PARAMETROS`.

Lo que NO hace:
    - No toca lógica del bot (agent.py, woocommerce.py).
    - No toca el schema de `conversations` ni `messages`.
    - No tiene auditoría (descartada del alcance v1).
    - No tiene feriados (descartado del alcance v1).

Variables de entorno requeridas:
    ADMIN_PARAMETROS — contraseña que pone Nico para entrar a /admin/params.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.bot_status import (
    default_config,
    evaluate_status,
    get_cached_config_sync,
    invalidate_cache,
    set_cache,
    validate_config_payload,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constantes ─────────────────────────────────────────────────────────────

PARAMS_COOKIE_NAME = "params_session"
COOKIE_TTL_SECONDS = 60 * 60 * 24  # 24 h

AGENT_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    config JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT only_one_row CHECK (id = 1)
);
"""


# ── Sesiones admin en memoria ──────────────────────────────────────────────
# Se pierden si se reinicia el server — OK, Nico vuelve a entrar.

_params_tokens: set[str] = set()


def _get_admin_password() -> str:
    return get_settings().admin_parametros


def require_params_admin(params_session: Optional[str] = Cookie(None)) -> bool:
    if not params_session or params_session not in _params_tokens:
        raise HTTPException(status_code=401, detail="admin params auth required")
    return True


# ── Inicialización de schema + carga del config ───────────────────────────

async def init_agent_config_schema(db) -> dict[str, Any]:
    """Crea la tabla agent_config si no existe. Inserta la fila default si está vacía.

    Devuelve el config actual (para que main.py lo cachee en startup).
    Idempotente: si la tabla ya existe y tiene la fila, devuelve esa fila.
    """
    pool = db._pool
    if pool is None:
        logger.warning("init_agent_config_schema: db pool no inicializado, skipping")
        return default_config()
    async with pool.acquire() as conn:
        await conn.execute(AGENT_CONFIG_SCHEMA)
        # Insertar fila default si no existe (demo disponible 24/7).
        row = await conn.fetchrow("SELECT config FROM agent_config WHERE id = 1")
        if row is None:
            default = default_config()
            await conn.execute(
                "INSERT INTO agent_config (id, config) VALUES (1, $1::jsonb)",
                json.dumps(default),
            )
            logger.info("agent_config: fila default de portfolio insertada")
            set_cache(default)
            return default
        # Existe: cachear y devolver
        config = row["config"]
        if isinstance(config, str):
            # Por si el codec no convirtió (defensive)
            config = json.loads(config)
        set_cache(config)
        logger.info("agent_config: cargado desde DB y cacheado")
        return config


async def get_config_from_db(db) -> dict[str, Any]:
    """Lee el config desde Postgres, sin cache. Para refresh manual."""
    pool = db._pool
    if pool is None:
        return default_config()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config FROM agent_config WHERE id = 1")
        if row is None:
            return default_config()
        config = row["config"]
        if isinstance(config, str):
            config = json.loads(config)
        return config


async def save_config_to_db(db, config: dict[str, Any]) -> None:
    """Guarda el config (upsert en id=1). Invalida cache."""
    pool = db._pool
    if pool is None:
        raise RuntimeError("db pool no inicializado")
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_config (id, config, updated_at)
               VALUES (1, $1::jsonb, NOW())
               ON CONFLICT (id) DO UPDATE
                 SET config = EXCLUDED.config, updated_at = NOW()""",
            json.dumps(config),
        )
    invalidate_cache()
    set_cache(config)


# ── Schemas ───────────────────────────────────────────────────────────────

class LoginPayload(BaseModel):
    password: str


# ── Routes: auth ──────────────────────────────────────────────────────────

@router.get("/admin/params", response_class=HTMLResponse)
async def params_root(params_session: Optional[str] = Cookie(None)):
    if params_session and params_session in _params_tokens:
        return RedirectResponse("/admin/params/panel", status_code=302)
    return HTMLResponse(LOGIN_HTML)


@router.post("/admin/params/api/login")
async def params_login(payload: LoginPayload):
    expected = _get_admin_password()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_PARAMETROS no configurada en el server",
        )
    if not secrets.compare_digest(payload.password, expected):
        raise HTTPException(status_code=401, detail="contraseña incorrecta")

    token = secrets.token_urlsafe(32)
    _params_tokens.add(token)
    resp = JSONResponse({"ok": True, "redirect": "/admin/params/panel"})
    resp.set_cookie(
        PARAMS_COOKIE_NAME, token,
        max_age=COOKIE_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=get_settings().secure_cookies,
        path="/",
    )
    return resp


@router.post("/admin/params/api/logout")
async def params_logout(params_session: Optional[str] = Cookie(None)):
    if params_session:
        _params_tokens.discard(params_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(PARAMS_COOKIE_NAME, path="/")
    return resp


# ── Routes: panel ─────────────────────────────────────────────────────────

@router.get("/admin/params/panel", response_class=HTMLResponse)
async def params_panel(params_session: Optional[str] = Cookie(None)):
    if not params_session or params_session not in _params_tokens:
        return RedirectResponse("/admin/params", status_code=302)
    return HTMLResponse(PANEL_HTML)


@router.get("/admin/params/api/config")
async def params_get_config(request: Request, _: bool = Depends(require_params_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    config = await get_config_from_db(db)
    return {"config": config}


@router.get("/admin/params/api/debug")
async def params_debug(request: Request, _: bool = Depends(require_params_admin)):
    """Diagnóstico: muestra qué tiene el cache en memoria vs qué hay en DB.

    Sirve para detectar si después de guardar quedó algo desincronizado
    entre lo que ve el webhook (cache) y lo que está persistido (DB).
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    cached = get_cached_config_sync()
    db_config = await get_config_from_db(db)
    cached_status, _ = evaluate_status(cached) if cached else (None, None)
    db_status, _ = evaluate_status(db_config)
    return {
        "cache": {
            "present": cached is not None,
            "kill_switch": cached.get("kill_switch") if cached else None,
            "respond_when_off": cached.get("respond_when_off") if cached else None,
            "status_if_now": cached_status,
        },
        "db": {
            "kill_switch": db_config.get("kill_switch"),
            "respond_when_off": db_config.get("respond_when_off"),
            "status_if_now": db_status,
        },
        "synced": (
            cached is not None
            and cached.get("kill_switch") == db_config.get("kill_switch")
        ),
    }


@router.post("/admin/params/api/cache/refresh")
async def params_refresh_cache(request: Request, _: bool = Depends(require_params_admin)):
    """Fuerza la lectura de DB y actualiza el cache del worker actual.

    Útil si el webhook está usando una versión vieja del config.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    invalidate_cache()
    fresh = await get_config_from_db(db)
    set_cache(fresh)
    return {"ok": True, "kill_switch": fresh.get("kill_switch")}


@router.post("/admin/params/api/config")
async def params_set_config(
    payload: dict[str, Any],
    request: Request,
    _: bool = Depends(require_params_admin),
):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    ok, err, normalized = validate_config_payload(payload)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    await save_config_to_db(db, normalized)
    return {"ok": True, "config": normalized}


# ── Presets de horario ────────────────────────────────────────────────────
# Atajos para casos comunes: 24/7, comercial L-V, cerrado siempre.
# Modifican SOLO el campo schedule. No tocan kill_switch, respond_when_off ni
# off_message — quedan como estaban.

_SCHEDULE_PRESETS: dict[str, dict[str, list[dict]]] = {
    # 24/7: cada día con un único rango 00:00-23:59 (en HH:MM no existe 24:00,
    # así que 23:59 es lo más cercano a "todo el día"; pierde 1 minuto entre
    # 23:59 y 00:00 del día siguiente que para uso comercial es irrelevante).
    "247": {
        d: [{"from": "00:00", "to": "23:59"}]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    },
    # Comercial: L-V 08:30-13:00 + 14:00-17:00, sáb y dom cerrados.
    "comercial": {
        "mon": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "tue": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "wed": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "thu": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "fri": [{"from": "08:30", "to": "13:00"}, {"from": "14:00", "to": "17:00"}],
        "sat": [],
        "sun": [],
    },
    # Cerrado siempre: ningún día tiene rangos. Si el cliente manda mensaje,
    # dispara off_horario (off_message si respond_when_off=true, sino silencio).
    "cerrado": {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
}


@router.post("/admin/params/api/preset")
async def params_apply_preset(
    payload: dict[str, Any],
    request: Request,
    _: bool = Depends(require_params_admin),
):
    """Aplica un preset de schedule sin tocar el resto del config.

    Body: {"name": "247" | "comercial" | "cerrado"}
    Devuelve el config completo después del cambio.
    """
    name = (payload.get("name") or "").strip().lower()
    if name not in _SCHEDULE_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"preset desconocido: {name!r} (válidos: {', '.join(_SCHEDULE_PRESETS)})",
        )
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    current = await get_config_from_db(db)
    current["schedule"] = _SCHEDULE_PRESETS[name]
    # Re-validamos por las dudas (asegura que el preset no se desincronice
    # con la validación si algún día se tocan reglas).
    ok, err, normalized = validate_config_payload(current)
    if not ok:
        raise HTTPException(status_code=500, detail=f"preset inválido: {err}")
    await save_config_to_db(db, normalized)
    return {"ok": True, "preset": name, "config": normalized}


# ── HTML ──────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel privado — MotorIA Demo</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5; min-height: 100vh; display: grid; place-items: center;
  }
  .card {
    background: white; padding: 32px; border-radius: 12px; width: 360px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.08);
  }
  h1 { margin: 0 0 8px; font-size: 22px; color: #e67e22; }
  .subtitle { margin: 0 0 24px; color: #666; font-size: 14px; }
  label { display: block; margin: 8px 0 4px; font-size: 13px; color: #444; }
  input[type=password] {
    width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px;
    font-size: 15px;
  }
  button {
    width: 100%; padding: 12px; background: #e67e22; color: white;
    border: none; border-radius: 6px; font-size: 15px; cursor: pointer; margin-top: 16px;
  }
  button:hover { background: #d35400; }
  button:disabled { background: #888; cursor: wait; }
  .err { color: #c0392b; font-size: 13px; margin-top: 8px; min-height: 18px; }
</style>
</head>
<body>
<div class="card">
  <h1>Panel privado — MotorIA Demo</h1>
  <p class="subtitle">Acceso restringido. Ingresá la contraseña.</p>
  <form id="loginForm" autocomplete="off">
    <label for="pwd">Contraseña</label>
    <input id="pwd" type="password" required autofocus>
    <button type="submit" id="btn">Entrar</button>
    <div class="err" id="err"></div>
  </form>
</div>
<script>
  const form = document.getElementById('loginForm');
  const errBox = document.getElementById('err');
  const btn = document.getElementById('btn');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errBox.textContent = '';
    btn.disabled = true; btn.textContent = 'Entrando...';
    try {
      const r = await fetch('/admin/params/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: document.getElementById('pwd').value }),
      });
      if (r.ok) {
        const d = await r.json();
        window.location.href = d.redirect || '/admin/params/panel';
      } else {
        const d = await r.json().catch(() => ({}));
        errBox.textContent = d.detail || 'Error de autenticación';
      }
    } catch (e) {
      errBox.textContent = 'Error de red';
    } finally {
      btn.disabled = false; btn.textContent = 'Entrar';
    }
  });
</script>
</body>
</html>"""


PANEL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Configuración del agente — MotorIA Demo</title>
<style>
  * { box-sizing: border-box; }
  :root {
    --accent: #f39c12;
    --accent-strong: #e67e22;
    --accent-soft: #fff4e0;
    --danger: #e74c3c;
    --ok: #27ae60;
    --header-bg: #1f2024;
    --text: #2c2c2c;
    --muted: #888;
    --border: #e0e0e0;
    --card-bg: #ffffff;
    --bg: #fafafa;
  }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 0; min-height: 100vh;
  }
  header {
    background: var(--header-bg); color: white; padding: 14px 24px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 2px 6px rgba(0,0,0,0.08);
  }
  header h1 { margin: 0; font-size: 17px; font-weight: 600; }
  header .actions { display: flex; gap: 10px; align-items: center; }
  header button {
    background: #3a3b40; color: white; border: 1px solid #4a4b50;
    padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    font-weight: 500;
  }
  header button:hover { background: #4a4b50; }
  /* Botón Guardar: siempre en naranja, pulsa cuando hay cambios sin guardar. */
  header #btnSave {
    background: var(--accent); color: white; border: 1px solid var(--accent-strong);
    font-weight: 600; padding: 7px 18px;
  }
  header #btnSave:hover { background: var(--accent-strong); }
  header #btnSave.dirty { animation: pulse 1.6s infinite; }
  header #btnSave:disabled { background: #ccc; color: #777; cursor: wait; border-color: #ccc; animation: none; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(243, 156, 18, 0.65); }
    70% { box-shadow: 0 0 0 10px rgba(243, 156, 18, 0); }
    100% { box-shadow: 0 0 0 0 rgba(243, 156, 18, 0); }
  }
  header .save-msg { font-size: 12px; color: rgba(255,255,255,0.85); margin-right: 4px; }
  header .save-msg.err { color: #ffb4b4; }
  main { max-width: 960px; margin: 24px auto; padding: 0 16px 24px; }
  .section {
    background: var(--card-bg); border-radius: 10px; padding: 22px 26px; margin-bottom: 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid var(--border);
  }
  .section h2 {
    margin: 0 0 8px; font-size: 17px; color: var(--accent-strong); font-weight: 700;
  }
  .section p.help { margin: 0 0 16px; color: #777; font-size: 13px; line-height: 1.5; }
  .switch-row { display: flex; align-items: center; gap: 16px; padding: 10px 0; }
  /* La label de texto ocupa el espacio sobrante; el toggle queda fijo en 44px */
  .switch-row > label:not(.toggle) { flex: 1 1 auto; font-size: 14px; cursor: pointer; font-weight: 500; min-width: 0; }
  .switch-row .desc { color: var(--muted); font-size: 12px; display: block; margin-top: 3px; font-weight: 400; }
  /* Toggle styling — tamaño iOS estándar 44x26, no se estira */
  .toggle { position: relative; display: inline-block; flex: 0 0 44px; width: 44px; height: 26px; cursor: pointer; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; cursor: pointer; inset: 0; background: #d4d4d4;
    transition: background-color .2s ease; border-radius: 26px;
  }
  .slider:before {
    content: ""; position: absolute; height: 22px; width: 22px;
    left: 2px; top: 2px; background: white; transition: transform .2s ease; border-radius: 50%;
    box-shadow: 0 1px 3px rgba(0,0,0,0.18);
  }
  .toggle input:checked + .slider { background: var(--accent); }
  .toggle input:checked + .slider:before { transform: translateX(18px); }
  /* Mensaje OFF */
  textarea {
    width: 100%; min-height: 100px; padding: 10px; border: 1px solid var(--border); border-radius: 6px;
    font-family: inherit; font-size: 14px; resize: vertical; color: var(--text);
  }
  textarea:focus { outline: none; border-color: var(--accent); }
  .char-count { text-align: right; font-size: 12px; color: var(--muted); margin-top: 4px; }
  /* Presets de horario */
  .presets-row {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin-bottom: 14px; padding: 10px 12px; background: #fafafa;
    border-radius: 6px; border: 1px solid var(--border);
  }
  .presets-label { font-size: 12px; color: var(--muted); font-weight: 500; margin-right: 4px; }
  .btn-preset {
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
    background: white; color: var(--text); border: 1px solid var(--border);
    transition: background .15s, color .15s, border-color .15s;
  }
  .btn-preset:hover { background: var(--accent-soft); color: var(--accent-strong); border-color: var(--accent); }
  .btn-preset:disabled { opacity: 0.5; cursor: wait; }
  /* Horarios tabla */
  .schedule-table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    font-size: 13px;
  }
  .schedule-table th, .schedule-table td {
    padding: 10px 12px; text-align: left; vertical-align: middle;
    border-bottom: 1px solid var(--border);
  }
  .schedule-table thead th {
    background: #fafafa; font-weight: 600; color: #555; font-size: 12px;
    text-transform: none; letter-spacing: 0;
  }
  .schedule-table tbody tr:last-child td { border-bottom: none; }
  .schedule-table tbody tr.extra td { border-top: none; }
  .schedule-table td.day-cell {
    font-weight: 600; color: var(--text); white-space: nowrap; width: 130px;
  }
  .schedule-table td.day-cell .day-icon {
    display: inline-block; margin-left: 4px; opacity: 0.6;
  }
  .schedule-table th.col-slot, .schedule-table td.slot {
    width: 200px; text-align: center;
  }
  /* Si ningún día tiene un Turno 2, ocultamos la columna entera para que la
     tabla "respire" más. Se vuelve a mostrar apenas el usuario agregue un
     segundo rango a algún día. */
  .schedule-table.hide-turno2 th.col-turno2,
  .schedule-table.hide-turno2 td.slot.turno2 { display: none; }
  .schedule-table th.col-actions, .schedule-table td.actions-cell {
    width: 70px; text-align: center; white-space: nowrap;
  }
  .schedule-table th.col-status, .schedule-table td.status-cell {
    width: 220px; font-size: 12px; color: var(--muted); white-space: nowrap;
  }
  .schedule-table td.status-cell.open { color: var(--ok); font-weight: 600; }
  .schedule-table td.status-cell.closed { color: var(--muted); }
  /* Range pill dentro de una celda de turno */
  .range-pill {
    display: inline-flex; align-items: center; gap: 4px;
    border: 1px solid var(--border); border-radius: 6px; padding: 3px 4px 3px 8px;
    background: #fafafa;
  }
  .range-pill input[type="time"] {
    border: none; background: transparent; font-size: 13px; padding: 2px 2px;
    width: 74px;
  }
  .range-pill input[type="time"]:focus { outline: 1px solid var(--accent); border-radius: 3px; }
  .range-pill .dash { color: var(--muted); font-size: 13px; }
  .range-pill .btn-remove-x {
    background: transparent; border: none; color: #999; cursor: pointer;
    padding: 4px; display: inline-flex; align-items: center; justify-content: center;
    border-radius: 4px; transition: color .15s, background .15s;
  }
  .range-pill .btn-remove-x svg { width: 14px; height: 14px; display: block; }
  .range-pill .btn-remove-x:hover { color: var(--danger); background: #fdecea; }
  .slot-empty {
    color: var(--muted); font-style: italic; font-size: 12px;
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 30px;
  }
  .btn-add {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 6px; cursor: pointer;
    font-size: 18px; line-height: 1; background: #f0f0f0; color: #555;
    border: none; font-weight: 500; transition: background .15s, color .15s;
  }
  .btn-add:hover { background: var(--accent-soft); color: var(--accent-strong); }
  .status-pill {
    display: inline-block; padding: 5px 14px; border-radius: 20px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.4px;
  }
  .status-pill.on { background: #d4edda; color: #155724; }
  .status-pill.off { background: var(--danger); color: white; }
  /* Footer con marca */
  .brand-footer {
    text-align: right; margin: 24px 0 8px; font-size: 11px; color: var(--muted);
    letter-spacing: 1.5px; font-weight: 600;
  }
</style>
</head>
<body>
<header>
  <h1>Configuración del agente — MotorIA Demo</h1>
  <div class="actions">
    <span id="globalStatus" class="status-pill off">CARGANDO...</span>
    <span id="saveMsg" class="save-msg"></span>
    <button id="btnSave" type="button">Guardar cambios</button>
    <button id="btnLogout">Salir</button>
  </div>
</header>
<main>

  <div class="section">
    <h2>Estado general</h2>
    <p class="help">
      Usa esta opción para pausar el bot. Los mensajes que los clientes envíen
      seguirán quedando guardados en el sistema.
    </p>
    <div class="switch-row">
      <label for="killSwitch">
        Apagar bot
        <span class="desc">Si lo activas, el bot dejará de contestar los mensajes.</span>
      </label>
      <label class="toggle">
        <input type="checkbox" id="killSwitch">
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <div class="section">
    <h2>Mensaje cuando el bot esté apagado</h2>
    <p class="help">
      Configura el mensaje que recibirán los clientes cuando el negocio esté
      cerrado o el bot esté apagado.
    </p>
    <div class="switch-row">
      <label for="respondWhenOff">
        Enviar mensaje automático
        <span class="desc">Si lo desactivas, los clientes no recibirán ninguna respuesta automática fuera de horario.</span>
      </label>
      <label class="toggle">
        <input type="checkbox" id="respondWhenOff" checked>
        <span class="slider"></span>
      </label>
    </div>
    <label for="offMessage" style="display:block; margin-top:14px; font-size:13px; color:#444;">Mensaje:</label>
    <textarea id="offMessage" maxlength="2000" placeholder="¡Hola! Estamos fuera de horario..."></textarea>
    <div class="char-count"><span id="charCount">0</span> / 2000</div>
  </div>

  <div class="section">
    <h2>Horarios de atención</h2>
    <p class="help">
      Definí los rangos horarios en que el bot va a responder normalmente. Cada
      día puede tener uno o varios rangos (por ejemplo: mañana 08:30–13:00 y
      tarde 14:00–17:00). Si un día no tiene rangos, el bot estará cerrado todo
      ese día.
    </p>
    <div class="presets-row">
      <span class="presets-label">Atajos:</span>
      <button type="button" class="btn-preset" data-preset="247">📅 24/7 (siempre abierto)</button>
      <button type="button" class="btn-preset" data-preset="comercial">🏪 Comercial L-V 8:30–13 / 14–17</button>
      <button type="button" class="btn-preset" data-preset="cerrado">🚫 Cerrado todos los días</button>
    </div>
    <table class="schedule-table">
      <thead>
        <tr>
          <th>Día</th>
          <th class="col-slot col-turno1">Turno 1</th>
          <th class="col-slot col-turno2">Turno 2</th>
          <th class="col-actions"></th>
          <th class="col-status">Estado</th>
        </tr>
      </thead>
      <tbody id="scheduleBody"></tbody>
    </table>
  </div>

  <div class="brand-footer">MOTORIA DEMO · METAIA</div>

</main>

<script>
  const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  const DAY_NAMES = {
    mon: 'Lunes', tue: 'Martes', wed: 'Miércoles', thu: 'Jueves',
    fri: 'Viernes', sat: 'Sábado', sun: 'Domingo',
  };

  const elKill = document.getElementById('killSwitch');
  const elRespond = document.getElementById('respondWhenOff');
  const elMsg = document.getElementById('offMessage');
  const elCharCount = document.getElementById('charCount');
  const elScheduleBody = document.getElementById('scheduleBody');
  const elStatus = document.getElementById('globalStatus');
  const elSaveMsg = document.getElementById('saveMsg');
  const btnSave = document.getElementById('btnSave');
  const btnLogout = document.getElementById('btnLogout');

  // Renderizado en tabla. Cada día = 1 fila principal con 2 slots (turno 1 / turno 2).
  // Si hay 3+ rangos, se agregan filas extra debajo con la columna día vacía.
  function renderDays(schedule) {
    elScheduleBody.innerHTML = '';
    for (const dk of DAY_KEYS) {
      const ranges = schedule[dk] || [];
      renderDayRow(dk, ranges);
    }
    updateTurno2Visibility();
  }

  // Oculta la columna "Turno 2" si ningún día tiene un segundo rango cargado.
  // La columna se muestra automáticamente en cuanto el usuario agrega un
  // segundo rango a cualquier día.
  function updateTurno2Visibility() {
    const table = document.querySelector('.schedule-table');
    if (!table) return;
    const anyDayHasTurno2 = DAY_KEYS.some(dk => collectDayRanges(dk).length >= 2);
    table.classList.toggle('hide-turno2', !anyDayHasTurno2);
  }

  function renderDayRow(dk, ranges) {
    // Fila principal: día + slot 0 + slot 1 + acciones + status
    // Los slots impares (1, 3, 5...) son la columna "Turno 2" — se ocultan
    // cuando ningún día tiene un Turno 2 cargado.
    const tr = document.createElement('tr');
    tr.dataset.day = dk;
    tr.classList.add('main-row');
    tr.innerHTML = `
      <td class="day-cell">${DAY_NAMES[dk]} <span class="day-icon">📅</span></td>
      <td class="slot turno1" data-idx="0"></td>
      <td class="slot turno2" data-idx="1"></td>
      <td class="actions-cell"></td>
      <td class="status-cell"></td>
    `;
    elScheduleBody.appendChild(tr);

    // Slots 0 y 1
    fillSlot(tr.querySelector('.slot[data-idx="0"]'), ranges[0]);
    fillSlot(tr.querySelector('.slot[data-idx="1"]'), ranges[1]);

    // Rangos extra (3+): filas adicionales con la columna día vacía
    for (let i = 2; i < ranges.length; i += 2) {
      const trExtra = document.createElement('tr');
      trExtra.dataset.day = dk;
      trExtra.classList.add('extra');
      trExtra.innerHTML = `
        <td></td>
        <td class="slot turno1" data-idx="${i}"></td>
        <td class="slot turno2" data-idx="${i+1}"></td>
        <td></td>
        <td></td>
      `;
      elScheduleBody.appendChild(trExtra);
      fillSlot(trExtra.querySelector(`.slot[data-idx="${i}"]`), ranges[i]);
      if (ranges[i+1]) {
        fillSlot(trExtra.querySelector(`.slot[data-idx="${i+1}"]`), ranges[i+1]);
      }
    }

    renderDayActions(dk);
    updateDayStatus(dk);
  }

  // Ícono SVG papelera (Heroicons outline trash) — sutil, 14x14.
  const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';

  function fillSlot(cell, range) {
    if (!range) {
      cell.innerHTML = '<span class="slot-empty">(vacío)</span>';
      return;
    }
    cell.innerHTML = '';
    const pill = document.createElement('span');
    pill.className = 'range-pill';
    pill.innerHTML = `
      <input type="time" class="from" value="${range.from || '09:00'}">
      <span class="dash">–</span>
      <input type="time" class="to" value="${range.to || '13:00'}">
      <button type="button" class="btn-remove-x" title="Quitar este rango" aria-label="Quitar rango">${TRASH_SVG}</button>
    `;
    pill.querySelector('.btn-remove-x').onclick = () => removeRange(cell);
    cell.appendChild(pill);
  }

  function removeRange(cell) {
    const dk = cell.closest('tr').dataset.day;
    const ranges = collectDayRanges(dk);
    const idx = parseInt(cell.dataset.idx, 10);
    ranges.splice(idx, 1);
    redrawDay(dk, ranges);
    markDirty();
  }

  function addRange(dk) {
    const ranges = collectDayRanges(dk);
    // Default razonable según si es el primer rango o no
    const defaults = ranges.length === 0
      ? { from: '08:30', to: '13:00' }
      : { from: '14:00', to: '17:00' };
    ranges.push(defaults);
    redrawDay(dk, ranges);
    markDirty();
  }

  // Borra todas las filas del día y las re-renderiza con los rangos pasados.
  function redrawDay(dk, ranges) {
    elScheduleBody.querySelectorAll(`tr[data-day="${dk}"]`).forEach(r => r.remove());
    // Encontrar la siguiente fila del día siguiente para insertar antes y mantener orden.
    const idx = DAY_KEYS.indexOf(dk);
    let anchor = null;
    for (let i = idx + 1; i < DAY_KEYS.length; i++) {
      const next = elScheduleBody.querySelector(`tr[data-day="${DAY_KEYS[i]}"]`);
      if (next) { anchor = next; break; }
    }
    const tr = document.createElement('tr');
    tr.dataset.day = dk;
    tr.classList.add('main-row');
    tr.innerHTML = `
      <td class="day-cell">${DAY_NAMES[dk]} <span class="day-icon">📅</span></td>
      <td class="slot turno1" data-idx="0"></td>
      <td class="slot turno2" data-idx="1"></td>
      <td class="actions-cell"></td>
      <td class="status-cell"></td>
    `;
    if (anchor) elScheduleBody.insertBefore(tr, anchor); else elScheduleBody.appendChild(tr);
    fillSlot(tr.querySelector('.slot[data-idx="0"]'), ranges[0]);
    fillSlot(tr.querySelector('.slot[data-idx="1"]'), ranges[1]);
    let insertAfter = tr;
    for (let i = 2; i < ranges.length; i += 2) {
      const trExtra = document.createElement('tr');
      trExtra.dataset.day = dk;
      trExtra.classList.add('extra');
      trExtra.innerHTML = `
        <td></td>
        <td class="slot turno1" data-idx="${i}"></td>
        <td class="slot turno2" data-idx="${i+1}"></td>
        <td></td>
        <td></td>
      `;
      insertAfter.after(trExtra);
      insertAfter = trExtra;
      fillSlot(trExtra.querySelector(`.slot[data-idx="${i}"]`), ranges[i]);
      if (ranges[i+1]) fillSlot(trExtra.querySelector(`.slot[data-idx="${i+1}"]`), ranges[i+1]);
    }
    renderDayActions(dk);
    updateDayStatus(dk);
    updateTurno2Visibility();
  }

  function renderDayActions(dk) {
    const mainRow = elScheduleBody.querySelector(`tr.main-row[data-day="${dk}"]`);
    if (!mainRow) return;
    const cell = mainRow.querySelector('.actions-cell');
    cell.innerHTML = '';
    const btnAdd = document.createElement('button');
    btnAdd.type = 'button';
    btnAdd.className = 'btn-add';
    btnAdd.textContent = '+';
    btnAdd.title = 'Agregar rango horario';
    btnAdd.setAttribute('aria-label', 'Agregar rango');
    btnAdd.onclick = () => addRange(dk);
    cell.appendChild(btnAdd);
  }

  function updateDayStatus(dk) {
    const mainRow = elScheduleBody.querySelector(`tr.main-row[data-day="${dk}"]`);
    if (!mainRow) return;
    const cell = mainRow.querySelector('.status-cell');
    const ranges = collectDayRanges(dk);
    if (ranges.length === 0) {
      cell.textContent = 'Cerrado';
      cell.className = 'status-cell closed';
    } else {
      const parts = ranges.map(r => `${r.from}–${r.to}`);
      cell.textContent = parts.join(' · ');
      cell.className = 'status-cell open';
    }
  }

  function collectDayRanges(dk) {
    const arr = [];
    elScheduleBody.querySelectorAll(`tr[data-day="${dk}"] .slot`).forEach(slot => {
      const pill = slot.querySelector('.range-pill');
      if (!pill) return;
      arr.push({
        from: pill.querySelector('.from').value,
        to: pill.querySelector('.to').value,
      });
    });
    return arr;
  }

  function collectSchedule() {
    const out = {};
    for (const dk of DAY_KEYS) {
      out[dk] = collectDayRanges(dk);
    }
    return out;
  }

  function updateGlobalStatus() {
    if (elKill.checked) {
      elStatus.textContent = 'BOT APAGADO';
      elStatus.className = 'status-pill off';
    } else {
      elStatus.textContent = 'BOT ACTIVO';
      elStatus.className = 'status-pill on';
    }
  }

  function updateCharCount() {
    elCharCount.textContent = elMsg.value.length;
  }

  // Dirty-state tracking ────────────────────────────────────────────────
  // Marcamos el botón "Guardar cambios" en naranja pulsante cuando hay
  // cambios sin guardar. Sirve para que Nico no se olvide de apretar Save.
  let isDirty = false;
  function markDirty() {
    if (!isDirty) {
      isDirty = true;
      btnSave.classList.add('dirty');
      btnSave.textContent = 'Guardar cambios *';
    }
    elSaveMsg.textContent = '';
    elSaveMsg.className = 'save-msg';
  }
  function clearDirty() {
    isDirty = false;
    btnSave.classList.remove('dirty');
    btnSave.textContent = 'Guardar cambios';
  }
  window.addEventListener('beforeunload', (e) => {
    if (isDirty) {
      e.preventDefault();
      // Texto ignorado por browsers modernos pero forzamos el confirm nativo.
      e.returnValue = 'Tenés cambios sin guardar.';
      return e.returnValue;
    }
  });

  async function loadConfig() {
    elSaveMsg.textContent = '';
    try {
      const r = await fetch('/admin/params/api/config');
      if (r.status === 401) { window.location.href = '/admin/params'; return; }
      const data = await r.json();
      const cfg = data.config || {};
      elKill.checked = !!cfg.kill_switch;
      elRespond.checked = cfg.respond_when_off !== false;
      elMsg.value = cfg.off_message || '';
      renderDays(cfg.schedule || {});
      updateGlobalStatus();
      updateCharCount();
      clearDirty();
    } catch (e) {
      elSaveMsg.textContent = 'Error cargando configuración';
      elSaveMsg.className = 'save-msg err';
    }
  }

  async function saveConfig() {
    btnSave.disabled = true;
    elSaveMsg.textContent = 'Guardando...';
    elSaveMsg.className = 'save-msg';
    const payload = {
      kill_switch: elKill.checked,
      respond_when_off: elRespond.checked,
      off_message: elMsg.value,
      schedule: collectSchedule(),
    };
    try {
      const r = await fetch('/admin/params/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        elSaveMsg.textContent = '✓ Guardado';
        elSaveMsg.className = 'save-msg';
        clearDirty();
        setTimeout(() => { elSaveMsg.textContent = ''; }, 4000);
      } else {
        const d = await r.json().catch(() => ({}));
        elSaveMsg.textContent = 'Error: ' + (d.detail || r.statusText);
        elSaveMsg.className = 'save-msg err';
      }
    } catch (e) {
      elSaveMsg.textContent = 'Error de red';
      elSaveMsg.className = 'save-msg err';
    } finally {
      btnSave.disabled = false;
    }
  }

  // Event listeners
  elKill.addEventListener('change', () => { updateGlobalStatus(); markDirty(); });
  elRespond.addEventListener('change', markDirty);
  elMsg.addEventListener('input', () => { updateCharCount(); markDirty(); });
  btnSave.addEventListener('click', saveConfig);
  btnLogout.addEventListener('click', async (e) => {
    if (isDirty && !confirm('Hay cambios sin guardar. ¿Salir igual?')) return;
    await fetch('/admin/params/api/logout', { method: 'POST' });
    window.location.href = '/admin/params';
  });
  // Dirty + actualización del summary cuando cambia una hora directamente
  elScheduleBody.addEventListener('input', (e) => {
    if (e.target.matches('.from, .to')) {
      const tr = e.target.closest('tr');
      if (tr) updateDayStatus(tr.dataset.day);
      markDirty();
    }
  });

  // Atajos de schedule (24/7, comercial, cerrado).
  // Si hay cambios sin guardar, pedimos confirmación porque el preset los pisa.
  document.querySelectorAll('.btn-preset').forEach(btn => {
    btn.addEventListener('click', async () => {
      const preset = btn.dataset.preset;
      if (isDirty && !confirm('Tenés cambios sin guardar. El atajo los va a pisar. ¿Continuar?')) return;
      const allButtons = document.querySelectorAll('.btn-preset');
      allButtons.forEach(b => b.disabled = true);
      elSaveMsg.textContent = 'Aplicando atajo...';
      elSaveMsg.className = 'save-msg';
      try {
        const r = await fetch('/admin/params/api/preset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: preset }),
        });
        if (r.ok) {
          // Recargar el config completo para reflejar el cambio.
          await loadConfig();
          elSaveMsg.textContent = '✓ Atajo aplicado y guardado';
          elSaveMsg.className = 'save-msg';
          setTimeout(() => { elSaveMsg.textContent = ''; }, 4000);
        } else {
          const d = await r.json().catch(() => ({}));
          elSaveMsg.textContent = 'Error: ' + (d.detail || r.statusText);
          elSaveMsg.className = 'save-msg err';
        }
      } catch (e) {
        elSaveMsg.textContent = 'Error de red';
        elSaveMsg.className = 'save-msg err';
      } finally {
        allButtons.forEach(b => b.disabled = false);
      }
    });
  });

  loadConfig();
</script>
</body>
</html>"""
