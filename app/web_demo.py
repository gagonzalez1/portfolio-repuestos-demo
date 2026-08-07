"""Frontend público de la demo portfolio MotorIA.

Este módulo es un FRONTEND y nada más. NO contiene lógica del bot.

Lo único que hace:
    - Sirve una UI web estilo WhatsApp con badge "DEMO" para que el cliente
      o stakeholders prueben el agente desde cualquier dispositivo sin pasar
      por WhatsApp / Meta.
    - Acceso público mediante una sesión anónima de vida corta.
    - Dashboard admin con auth separada para ver todas las sesiones del demo
      en tiempo real (auto-refresh cada 3 s).
    - Botón de "🐛 esto no era lo que esperaba" en cada respuesta del bot
      que guarda feedback en una tabla auxiliar `demo_feedback`. Esa tabla
      la lee solo el dashboard admin — el bot no la conoce ni la toca.

CONSUMO del bot (NO modifica nada):
    - Llama a `agent.process_message(text, sender, conversation)`
    - Usa `db.get_or_create_conversation()` y `db.save_message()`
    - Filtra conversaciones por prefijo `web_demo_*` para el dashboard

NO toca:
    - app/agent.py, app/woocommerce.py, app/motor_catalog.py, app/bootstrap.py
    - El schema de tablas `conversations` ni `messages`
    - El handler del webhook de WhatsApp en main.py

Variable privada del panel:
    ADMIN_PASSWORD — contraseña del dashboard admin (no se comparte con visitantes).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.admin_params import get_config_from_db
from app.bot_status import (
    STATUS_ON,
    evaluate_status,
    get_cached_config_sync,
    set_cache,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constantes ─────────────────────────────────────────────────────────────

DEMO_SENDER_PREFIX = "wd_"  # corto a propósito: phone_number es VARCHAR(20)
DEMO_COOKIE_NAME = "demo_session"
ADMIN_COOKIE_NAME = "admin_session"
ADMIN_COOKIE_TTL_SECONDS = 60 * 60 * 8

# Tabla auxiliar — el bot ignora esta tabla. Solo el dashboard la lee/escribe.
DEMO_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_feedback (
    id SERIAL PRIMARY KEY,
    session_alias VARCHAR(50) NOT NULL,
    turn_number INTEGER NOT NULL,
    bot_message TEXT,
    user_message TEXT,
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_demo_feedback_alias ON demo_feedback(session_alias);
CREATE INDEX IF NOT EXISTS idx_demo_feedback_created ON demo_feedback(created_at DESC);
"""


# ── Sesiones en memoria ────────────────────────────────────────────────────
# Se pierden si se reinicia el server. OK porque cada visita al demo arranca
# fresh igual (decisión del producto: G=arrancar de cero).

_visitor_sessions: dict[str, dict] = {}
_admin_tokens: set[str] = set()
_rate_lock = asyncio.Lock()
_daily_usage: dict[str, object] = {
    "day": date.today(),
    "global": 0,
    "ips": {},
}
_metrics = {
    "visits": 0,
    "demo_starts": 0,
    "conversations_completed": 0,
    "cta_clicks": 0,
    "challenges_started": 0,
    "challenges_completed": 0,
}

DEMO_CHALLENGES = {
    "mondeo_junta": {
        "title": "La tapa del Mondeo",
        "difficulty": "Inicial",
        "brief": "Entró un Ford Mondeo 1.8 Zetec 16V para reparar la tapa. Necesitás encontrar una junta de descarbonización que ya incluya retenes.",
        "skills": ["pieza", "vehículo", "motor", "variante"],
        "target_id": "DEMO-0006",
        "hints": [
            "Empezá por la familia de producto: junta de descarbonización.",
            "Filtrá la aplicación por Ford y buscá Mondeo con motor Zetec 1.8 16V.",
            "Compará las variantes: el objetivo pide específicamente una opción con retenes.",
        ],
        "success": "Encontraste la junta compatible y verificaste la variante con retenes.",
    },
    "corsa_aros": {
        "title": "El armado del Corsa",
        "difficulty": "Intermedio",
        "brief": "Estás armando un Chevrolet Corsa 1.8 8V y el conjunto conserva medida estándar. Encontrá el juego de aros correcto.",
        "skills": ["familia", "cilindrada", "válvulas", "medida"],
        "target_id": "DEMO-0056",
        "hints": [
            "La familia que necesitás explorar es Aros.",
            "La aplicación debe corresponder a Chevrolet 1.8 de 8 válvulas.",
            "STD significa medida estándar; descartá las sobremedidas.",
        ],
        "success": "Elegiste los aros para el motor 1.8 8V conservando medida STD.",
    },
    "corsa_distribucion": {
        "title": "Distribución sin piezas sueltas",
        "difficulty": "Intermedio",
        "brief": "Un Chevrolet Corsa 1.6 8V necesita distribución completa. El objetivo es resolverla con un kit que incluya también la bomba de agua.",
        "skills": ["kit", "contenido", "vehículo", "motor"],
        "target_id": "DEMO-0176",
        "hints": [
            "No busques solamente una correa: el objetivo pide un kit completo.",
            "Usá Chevrolet y Corsa para reducir el catálogo.",
            "Revisá que el nombre confirme 1.6 8V y que incluya bomba.",
        ],
        "success": "Seleccionaste el kit de distribución que integra la bomba de agua.",
    },
    "volvo_reten": {
        "title": "La pieza sin etiqueta",
        "difficulty": "Avanzado",
        "brief": "Llegó un retén de árbol de levas sin caja. Solo tenés la medida 30 × 47 × 7 mm y la referencia de que pertenece a un Volvo S40 o V40.",
        "skills": ["medida", "tipo", "marca", "modelos"],
        "target_id": "DEMO-0175",
        "hints": [
            "Buscá por la medida exacta: 30x47x7.",
            "La pieza trabaja en el árbol de levas; evitá otros tipos de retenes.",
            "La compatibilidad debe mencionar Volvo S40 o V40.",
        ],
        "success": "Identificaste el retén por dimensiones y confirmaste su aplicación Volvo.",
    },
}


def _new_visitor_session(client_key: str) -> dict:
    """Crea una nueva sesión de visitante.

    Devuelve dict con:
      - alias: nombre de display (ej: "Visitante-4231")
      - sender_id: id corto que cabe en phone_number VARCHAR(20) (ej: "wd_4231")
      - created_at: timestamp ISO
    """
    session_id = secrets.token_hex(6)
    now = datetime.now(timezone.utc)
    return {
        "alias": f"Visitante-{session_id[-4:].upper()}",
        "sender_id": f"{DEMO_SENDER_PREFIX}{session_id}",
        "created_at": now,
        "expires_at": now + timedelta(seconds=get_settings().demo_session_ttl_seconds),
        "client_key": client_key,
        "message_count": 0,
        "counted_complete": False,
        "challenge": None,
    }


def _alias_from_sender_id(sender_id: str) -> str:
    """Reconstruye el alias display desde el sender_id corto.

    'wd_4231' → 'Visitante-4231'. Si no matchea el formato esperado,
    devuelve el sender_id tal cual (defensive).
    """
    if sender_id.startswith(DEMO_SENDER_PREFIX):
        suffix = sender_id[len(DEMO_SENDER_PREFIX):]
        if suffix.isalnum():
            return f"Visitante-{suffix[-4:].upper()}"
    return sender_id


def _get_demo_password() -> str:
    return get_settings().demo_password


def _get_admin_password() -> str:
    return get_settings().admin_password


# ── Auth dependencies ──────────────────────────────────────────────────────

def _client_key(request: Request) -> str:
    """Identificador diario no reversible; no persistimos la IP en la DB."""
    settings = get_settings()
    host = request.client.host if request.client else "unknown"
    if settings.demo_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            host = forwarded
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


def _session_is_valid(session: dict) -> bool:
    return datetime.now(timezone.utc) < session["expires_at"]


def require_visitor(request: Request, demo_session: Optional[str] = Cookie(None)) -> dict:
    """Dep para endpoints de chat. 401 si no hay cookie válida."""
    if not demo_session or demo_session not in _visitor_sessions:
        raise HTTPException(status_code=401, detail="auth required")
    session = _visitor_sessions[demo_session]
    if not _session_is_valid(session):
        _visitor_sessions.pop(demo_session, None)
        raise HTTPException(status_code=401, detail="session expired")
    if session["client_key"] != _client_key(request):
        raise HTTPException(status_code=401, detail="invalid session")
    return session


def _sanitize_message(value: str) -> str:
    # Quita controles invisibles salvo saltos/tab y limita espacios repetidos.
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value or "")
    return value.strip()


async def _consume_quota(session: dict) -> None:
    settings = get_settings()
    if not settings.demo_enabled:
        raise HTTPException(status_code=503, detail="La demo está pausada temporalmente.")
    async with _rate_lock:
        today = date.today()
        if _daily_usage["day"] != today:
            _daily_usage.update({"day": today, "global": 0, "ips": {}})
        ip_counts = _daily_usage["ips"]
        assert isinstance(ip_counts, dict)
        if session["message_count"] >= settings.demo_max_messages_per_session:
            raise HTTPException(status_code=429, detail="Alcanzaste el límite de esta conversación. Iniciá una nueva para seguir.")
        if ip_counts.get(session["client_key"], 0) >= settings.demo_max_messages_per_ip_day:
            raise HTTPException(status_code=429, detail="Alcanzaste el límite diario de la demo.")
        if int(_daily_usage["global"]) >= settings.demo_global_messages_per_day:
            raise HTTPException(status_code=503, detail="La demo alcanzó su cupo diario. Volvé mañana.")
        session["message_count"] += 1
        ip_counts[session["client_key"]] = ip_counts.get(session["client_key"], 0) + 1
        _daily_usage["global"] = int(_daily_usage["global"]) + 1


def require_admin(admin_session: Optional[str] = Cookie(None)) -> bool:
    if not admin_session or admin_session not in _admin_tokens:
        raise HTTPException(status_code=401, detail="admin auth required")
    return True


# ── Inicialización de schema (llamada desde lifespan de main.py) ───────────

async def init_demo_schema(db) -> None:
    """Crea la tabla auxiliar de feedback. Idempotente.

    Recibe el objeto Database existente y usa su pool. NO modifica la clase.
    """
    pool = db._pool  # acceso directo al pool — no podemos modificar Database
    if pool is None:
        logger.warning("init_demo_schema: db pool no inicializado, skipping")
        return
    async with pool.acquire() as conn:
        await conn.execute(DEMO_FEEDBACK_SCHEMA)
        retention_days = get_settings().demo_retention_days
        # Retención corta, limitada estrictamente a las conversaciones demo.
        await conn.execute(
            """DELETE FROM messages WHERE conversation_id IN (
                   SELECT id FROM conversations
                   WHERE phone_number LIKE $1
                     AND updated_at < NOW() - ($2 * INTERVAL '1 day')
               )""",
            f"{DEMO_SENDER_PREFIX}%", retention_days,
        )
        await conn.execute(
            """DELETE FROM conversations
               WHERE phone_number LIKE $1
                 AND updated_at < NOW() - ($2 * INTERVAL '1 day')""",
            f"{DEMO_SENDER_PREFIX}%", retention_days,
        )
        await conn.execute(
            "DELETE FROM demo_feedback WHERE created_at < NOW() - ($1 * INTERVAL '1 day')",
            retention_days,
        )
    logger.info("demo_feedback schema OK")


# ── Queries del demo (read-only para tablas del bot) ──────────────────────

async def _list_demo_sessions(pool) -> list[dict]:
    """Lista sesiones de demo con conteos de mensajes y flags."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id,
                c.phone_number,
                c.state,
                c.context,
                c.updated_at,
                COALESCE(mc.cnt, 0) AS msg_count,
                COALESCE(fc.cnt, 0) AS flag_count
            FROM conversations c
            LEFT JOIN (
                SELECT conversation_id, COUNT(*) AS cnt
                FROM messages GROUP BY conversation_id
            ) mc ON mc.conversation_id = c.id
            LEFT JOIN (
                SELECT session_alias, COUNT(*) AS cnt
                FROM demo_feedback GROUP BY session_alias
            ) fc ON fc.session_alias = c.phone_number
            WHERE c.phone_number LIKE $1
            ORDER BY c.updated_at DESC
            LIMIT 100
            """,
            f"{DEMO_SENDER_PREFIX}%",
        )
        result = []
        for r in rows:
            d = dict(r)
            d["alias"] = _alias_from_sender_id(d["phone_number"])
            d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
            result.append(d)
        return result


async def _get_session_messages(pool, conv_id: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, role, content, created_at FROM messages
               WHERE conversation_id = $1 ORDER BY created_at ASC, id ASC""",
            conv_id,
        )
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


async def _get_session_feedback(pool, sender_id: str) -> list[dict]:
    """Lista feedback de una sesión. Recibe sender_id (no el display alias)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, session_alias, turn_number, bot_message, user_message,
                      comment, created_at FROM demo_feedback
               WHERE session_alias = $1 ORDER BY created_at DESC""",
            sender_id,
        )
        return [
            {
                "id": r["id"],
                "alias": _alias_from_sender_id(r["session_alias"]),
                "turn_number": r["turn_number"],
                "bot_message": r["bot_message"],
                "user_message": r["user_message"],
                "comment": r["comment"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


async def _save_feedback(
    pool, alias: str, turn_number: int, bot_message: str, user_message: str, comment: str
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO demo_feedback
               (session_alias, turn_number, bot_message, user_message, comment)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            alias, turn_number, bot_message, user_message, comment,
        )
        return row["id"]


# ── Schemas (request/response) ─────────────────────────────────────────────

class LoginPayload(BaseModel):
    password: str


class ChatPayload(BaseModel):
    text: str


class FeedbackPayload(BaseModel):
    turn_number: int
    bot_message: str = ""
    user_message: str = ""
    comment: str = ""


class ChallengePayload(BaseModel):
    challenge_id: str


class ChallengeCheckPayload(BaseModel):
    challenge_id: str
    product_id: str


def _public_challenge(challenge_id: str, challenge: dict) -> dict:
    return {
        "id": challenge_id,
        "title": challenge["title"],
        "difficulty": challenge["difficulty"],
        "brief": challenge["brief"],
        "skills": challenge["skills"],
        "hint_count": len(challenge["hints"]),
    }


def _challenge_mismatch_message(selected: dict, target: dict) -> str:
    selected_category = set(selected.get("categories", []))
    target_category = set(target.get("categories", []))
    if selected_category.isdisjoint(target_category):
        return "Todavía no. La familia de producto no coincide con la necesidad de la misión."

    selected_attrs = selected.get("attributes", {})
    target_attrs = target.get("attributes", {})
    selected_brands = set(selected_attrs.get("Marca Vehiculo", []))
    target_brands = set(target_attrs.get("Marca Vehiculo", []))
    if target_brands and selected_brands.isdisjoint(target_brands):
        return "Vas por la familia correcta, pero la aplicación del vehículo no coincide."

    return "Estás cerca. Compará con más cuidado motor, medida y variante antes de volver a validar."


# ── Routes: portfolio público + chat anónimo ───────────────────────────

@router.get("/demo", response_class=HTMLResponse)
async def demo_root():
    _metrics["visits"] += 1
    return HTMLResponse(LOGIN_HTML)


@router.post("/demo/api/session")
async def demo_start_session(request: Request, demo_session: Optional[str] = Cookie(None)):
    """Crea una sesión anónima; no solicita ni persiste datos personales."""
    if not get_settings().demo_enabled:
        raise HTTPException(status_code=503, detail="La demo está pausada temporalmente.")
    if demo_session:
        _visitor_sessions.pop(demo_session, None)
    token = secrets.token_urlsafe(32)
    info = _new_visitor_session(_client_key(request))
    _visitor_sessions[token] = info
    _metrics["demo_starts"] += 1
    resp = JSONResponse({"alias": info["alias"], "redirect": "/demo/chat"})
    resp.set_cookie(
        DEMO_COOKIE_NAME, token,
        max_age=get_settings().demo_session_ttl_seconds,
        httponly=True, samesite="lax", secure=get_settings().secure_cookies, path="/",
    )
    return resp


# Alias compatible para clientes anteriores; la clave ya no se evalúa.
@router.post("/demo/api/login", include_in_schema=False)
async def demo_login_compat(request: Request):
    return await demo_start_session(request, None)


@router.post("/demo/api/new-conversation")
async def demo_new_conversation(request: Request, demo_session: Optional[str] = Cookie(None)):
    return await demo_start_session(request, demo_session)


@router.post("/demo/api/logout")
async def demo_logout(demo_session: Optional[str] = Cookie(None)):
    if demo_session:
        _visitor_sessions.pop(demo_session, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(DEMO_COOKIE_NAME, path="/")
    return resp


@router.get("/demo/chat", response_class=HTMLResponse)
async def demo_chat_page(demo_session: Optional[str] = Cookie(None)):
    if not demo_session or demo_session not in _visitor_sessions:
        return RedirectResponse("/demo", status_code=302)
    return HTMLResponse(CHAT_HTML)


@router.get("/demo/api/me")
async def demo_me(session: dict = Depends(require_visitor)):
    return {
        "alias": session["alias"],
        "messages_remaining": max(
            0, get_settings().demo_max_messages_per_session - session["message_count"]
        ),
    }


@router.get("/demo/api/catalog")
async def demo_catalog(
    request: Request,
    query: str = Query("", max_length=120),
    category: str = Query("", max_length=100),
    brand: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=48),
    session: dict = Depends(require_visitor),
):
    del session
    catalog = getattr(request.app.state, "catalog_client", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="catálogo no inicializado todavía")
    return await catalog.browse_products(
        query=_sanitize_message(query),
        category=_sanitize_message(category),
        brand=_sanitize_message(brand),
        page=page,
        per_page=per_page,
    )


@router.get("/demo/api/challenges")
async def demo_challenges(session: dict = Depends(require_visitor)):
    return {
        "items": [
            _public_challenge(challenge_id, challenge)
            for challenge_id, challenge in DEMO_CHALLENGES.items()
        ],
        "active": session.get("challenge"),
    }


@router.post("/demo/api/challenge/start")
async def demo_challenge_start(payload: ChallengePayload, session: dict = Depends(require_visitor)):
    challenge = DEMO_CHALLENGES.get(payload.challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="misión inexistente")
    session["challenge"] = {
        "id": payload.challenge_id,
        "hints_used": 0,
        "attempts": 0,
        "completed": False,
    }
    _metrics["challenges_started"] += 1
    return {
        "challenge": _public_challenge(payload.challenge_id, challenge),
        "mentor_message": "Misión iniciada. Explorá el catálogo y elegí un artículo. Si te trabás, pedime una pista.",
    }


@router.post("/demo/api/challenge/hint")
async def demo_challenge_hint(payload: ChallengePayload, session: dict = Depends(require_visitor)):
    challenge = DEMO_CHALLENGES.get(payload.challenge_id)
    state = session.get("challenge")
    if challenge is None:
        raise HTTPException(status_code=404, detail="misión inexistente")
    if not state or state.get("id") != payload.challenge_id:
        raise HTTPException(status_code=409, detail="iniciá esta misión antes de pedir pistas")
    hint_index = min(int(state["hints_used"]), len(challenge["hints"]) - 1)
    state["hints_used"] = min(hint_index + 1, len(challenge["hints"]))
    return {
        "hint": challenge["hints"][hint_index],
        "number": hint_index + 1,
        "remaining": max(0, len(challenge["hints"]) - state["hints_used"]),
    }


@router.post("/demo/api/challenge/check")
async def demo_challenge_check(
    payload: ChallengeCheckPayload,
    request: Request,
    session: dict = Depends(require_visitor),
):
    challenge = DEMO_CHALLENGES.get(payload.challenge_id)
    state = session.get("challenge")
    if challenge is None:
        raise HTTPException(status_code=404, detail="misión inexistente")
    if not state or state.get("id") != payload.challenge_id:
        raise HTTPException(status_code=409, detail="iniciá esta misión antes de validar")

    catalog = getattr(request.app.state, "catalog_client", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="catálogo no inicializado todavía")
    selected = await catalog.get_product(payload.product_id)
    target = await catalog.get_product(challenge["target_id"])
    if selected is None or target is None:
        raise HTTPException(status_code=404, detail="artículo inexistente")

    state["attempts"] += 1
    if payload.product_id != challenge["target_id"]:
        return {
            "correct": False,
            "message": _challenge_mismatch_message(selected, target),
            "attempts": state["attempts"],
        }

    if not state["completed"]:
        state["completed"] = True
        _metrics["challenges_completed"] += 1
    return {
        "correct": True,
        "message": challenge["success"],
        "attempts": state["attempts"],
        "product": {
            "id": target["id"],
            "name": target["name"],
            "price": target["price"],
            "stock_status": target["stock_status"],
        },
    }


@router.get("/demo/api/cta")
async def demo_cta():
    _metrics["cta_clicks"] += 1
    return RedirectResponse(get_settings().metaia_cta_url, status_code=302)


@router.post("/demo/api/chat")
async def demo_chat(payload: ChatPayload, request: Request, session: dict = Depends(require_visitor)):
    """Reusa agent.process_message() exactamente como el webhook de WhatsApp.

    Cero lógica del bot acá. Solo orquestación I/O.
    """
    text = _sanitize_message(payload.text)
    if not text:
        raise HTTPException(status_code=400, detail="texto vacío")
    if len(text) > get_settings().demo_max_message_length:
        raise HTTPException(
            status_code=413,
            detail=f"El mensaje supera {get_settings().demo_max_message_length} caracteres.",
        )
    await _consume_quota(session)

    db = getattr(request.app.state, "db", None)
    agent = getattr(request.app.state, "agent", None)
    if db is None or agent is None:
        raise HTTPException(status_code=503, detail="bot no inicializado todavía")

    sender_id = session["sender_id"]
    conversation = await db.get_or_create_conversation(sender_id)

    # Mismo guard que el webhook de WhatsApp: si el bot está apagado o fuera
    # de horario, el demo se comporta igual. Esto sirve para que el operador
    # pruebe el comportamiento del kill switch / horario antes de exponer a
    # WhatsApp real. Devolvemos un campo `bot_status` en el payload para que
    # el frontend pueda mostrar visualmente "🔇 Bot en silencio" sin afectar
    # el flujo principal.
    cached = get_cached_config_sync()
    if cached is None:
        cached = await get_config_from_db(db)
        set_cache(cached)
    status, off_msg = evaluate_status(cached)
    logger.info(
        f"[demo] bot_status: kill_switch={cached.get('kill_switch') if cached else None} "
        f"status={status}"
    )

    if status != STATUS_ON:
        await db.save_message(conversation["id"], "user", text)
        if off_msg:
            await db.save_message(conversation["id"], "assistant", off_msg)
            return {
                "alias": session["alias"],
                "parts": [off_msg],
                "bot_status": status,
            }
        return {
            "alias": session["alias"],
            "parts": [],
            "bot_status": status,
        }

    try:
        response_text = await agent.process_message(
            user_text=text,
            sender=sender_id,
            conversation=conversation,
        )
    except Exception as e:
        logger.error("[demo] error en process_message", exc_info=True)
        raise HTTPException(status_code=502, detail="No pudimos responder en este momento. Probá nuevamente.")

    # Persistir el turno (igual que el webhook)
    await db.save_message(conversation["id"], "user", text)
    await db.save_message(conversation["id"], "assistant", response_text)

    parts = (
        [p.strip() for p in response_text.split("---SPLIT---") if p.strip()]
        if "---SPLIT---" in response_text
        else [response_text]
    )

    if session["message_count"] >= 2 and not session["counted_complete"]:
        session["counted_complete"] = True
        _metrics["conversations_completed"] += 1
    return {
        "alias": session["alias"],
        "parts": parts,
        "bot_status": status,
        "messages_remaining": max(
            0, get_settings().demo_max_messages_per_session - session["message_count"]
        ),
    }


@router.post("/demo/api/feedback")
async def demo_feedback(payload: FeedbackPayload, request: Request, session: dict = Depends(require_visitor)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    pool = db._pool
    fid = await _save_feedback(
        pool,
        alias=session["sender_id"],  # usamos sender_id para que matchee el JOIN del dashboard
        turn_number=payload.turn_number,
        bot_message=payload.bot_message[:2000],
        user_message=payload.user_message[:2000],
        comment=payload.comment[:2000],
    )
    return {"id": fid, "ok": True}


# ── Routes: admin ──────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_root(admin_session: Optional[str] = Cookie(None)):
    if admin_session and admin_session in _admin_tokens:
        return RedirectResponse("/admin/dashboard", status_code=302)
    return HTMLResponse(ADMIN_LOGIN_HTML)


@router.post("/admin/api/login")
async def admin_login(payload: LoginPayload):
    expected = _get_admin_password()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD no configurada en el server")
    if not secrets.compare_digest(payload.password, expected):
        raise HTTPException(status_code=401, detail="contraseña incorrecta")

    token = secrets.token_urlsafe(32)
    _admin_tokens.add(token)
    resp = JSONResponse({"ok": True, "redirect": "/admin/dashboard"})
    resp.set_cookie(
        ADMIN_COOKIE_NAME, token,
        max_age=ADMIN_COOKIE_TTL_SECONDS, httponly=True, samesite="strict",
        secure=get_settings().secure_cookies, path="/",
    )
    return resp


@router.post("/admin/api/logout")
async def admin_logout(admin_session: Optional[str] = Cookie(None)):
    if admin_session:
        _admin_tokens.discard(admin_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return resp


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(admin_session: Optional[str] = Cookie(None)):
    if not admin_session or admin_session not in _admin_tokens:
        return RedirectResponse("/admin", status_code=302)
    return HTMLResponse(ADMIN_DASHBOARD_HTML)


@router.get("/admin/api/sessions")
async def admin_sessions(request: Request, _: bool = Depends(require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    return {"sessions": await _list_demo_sessions(db._pool)}


@router.get("/admin/api/session/{conv_id}")
async def admin_session_detail(conv_id: int, request: Request, _: bool = Depends(require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    pool = db._pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, phone_number, state, context, created_at, updated_at FROM conversations WHERE id = $1",
            conv_id,
        )
        if not row or not row["phone_number"].startswith(DEMO_SENDER_PREFIX):
            raise HTTPException(status_code=404, detail="sesión no encontrada")
        conv = dict(row)
        conv["alias"] = _alias_from_sender_id(conv["phone_number"])
        conv["created_at"] = conv["created_at"].isoformat() if conv["created_at"] else None
        conv["updated_at"] = conv["updated_at"].isoformat() if conv["updated_at"] else None
    messages = await _get_session_messages(pool, conv_id)
    feedback = await _get_session_feedback(pool, conv["phone_number"])
    return {"conversation": conv, "messages": messages, "feedback": feedback}


@router.get("/admin/api/feedback")
async def admin_all_feedback(request: Request, _: bool = Depends(require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="db no inicializada")
    pool = db._pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, session_alias, turn_number, bot_message, user_message,
                      comment, created_at FROM demo_feedback
               ORDER BY created_at DESC LIMIT 200"""
        )
    return {
        "feedback": [
            {
                "id": r["id"],
                "alias": _alias_from_sender_id(r["session_alias"]),
                "turn_number": r["turn_number"],
                "bot_message": r["bot_message"],
                "user_message": r["user_message"],
                "comment": r["comment"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/admin/api/metrics")
async def admin_metrics(_: bool = Depends(require_admin)):
    """Métricas agregadas en memoria, sin identificadores ni contenido."""
    return {"metrics": dict(_metrics)}


# ──────────────────────────────────────────────────────────────────────────
# HTML / CSS / JS inline (sin Jinja2 para no agregar deps)
# ──────────────────────────────────────────────────────────────────────────

# Paleta: rojo bordó (típico de comercios de repuestos AR) + verde WhatsApp
# para mensajes. Si el cliente quiere otra, cambiar las CSS variables abajo.

LOGIN_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MotorIA — Demo de agente de repuestos</title>
<style>
:root{--ink:#172033;--brand:#5b45e0;--accent:#17a673;--paper:#f6f7fb;--muted:#667085}*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:Inter,system-ui,sans-serif;color:var(--ink);background:radial-gradient(circle at 20% 0,#e8e5ff,transparent 45%),var(--paper)}
.wrap{width:min(1040px,calc(100% - 36px));margin:auto;padding:64px 0}.pill{display:inline-block;background:#e8e5ff;color:var(--brand);font-weight:800;padding:7px 12px;border-radius:999px;letter-spacing:.08em;font-size:12px}
h1{font-size:clamp(38px,7vw,68px);line-height:1.02;max-width:800px;margin:22px 0 18px}p{font-size:18px;line-height:1.65;color:var(--muted);max-width:760px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:30px 0}
button,a.cta{border:0;border-radius:12px;padding:14px 20px;font:inherit;font-weight:750;cursor:pointer;text-decoration:none}.start{background:var(--brand);color:white}.cta{color:var(--brand);background:white;border:1px solid #ddd9ff}.notice{background:#fff7dc;border:1px solid #f0d881;padding:14px 16px;border-radius:12px;font-size:14px;color:#66531d;max-width:760px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:42px}.card{background:white;padding:22px;border-radius:16px;border:1px solid #e8e9ef}.card b{display:block;margin-bottom:8px}.card p{font-size:14px;margin:0}.err{color:#b42318;min-height:20px;margin-top:8px}@media(max-width:720px){.grid{grid-template-columns:1fr}.wrap{padding:36px 0}}
</style></head><body><main class="wrap">
<span class="pill">DESAFÍO INTERACTIVO</span><h1>Encontrá el repuesto correcto.</h1>
<p>Explorá un catálogo técnico de 300 artículos, elegí una misión y usá las pistas de MotorIA para llegar a la pieza correcta. Acá el agente te guía: la decisión final es tuya.</p>
<div class="notice"><strong>Aviso:</strong> el catálogo, los productos, precios y disponibilidad son ficticios. No es una tienda activa ni permite comprar o reservar.</div>
<div class="actions"><button class="start" id="start">Entrar al desafío</button><a class="cta" href="/demo/api/cta">Crear una solución con MetaIA ↗</a></div><div class="err" id="err"></div>
<section class="grid"><div class="card"><b>1. Elegí una misión</b><p>Recibí una necesidad realista de taller sin conocer de antemano el artículo correcto.</p></div><div class="card"><b>2. Investigá el catálogo</b><p>Combiná familia, vehículo, motor y medidas. MotorIA ofrece pistas progresivas.</p></div><div class="card"><b>3. Validá tu elección</b><p>Seleccioná un artículo y comprobá si resolviste correctamente la compatibilidad.</p></div></section>
</main><script>
const btn=document.getElementById('start'),err=document.getElementById('err');btn.onclick=async()=>{btn.disabled=true;err.textContent='';try{const r=await fetch('/demo/api/session',{method:'POST'}),d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'No se pudo iniciar');location.href=d.redirect}catch(e){err.textContent=e.message}finally{btn.disabled=false}};
</script></body></html>"""


CHAT_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MotorIA DEMO — Chat de repuestos</title>
  <style>
    :root {
      --brand: #8b1a1a;
      --brand-dark: #5e0e0e;
      --user-bubble: #dcf8c6;
      --bot-bubble: #ffffff;
      --bg: #ece5dd;
      --header: #075e54;
      --text: #111;
      --muted: #667781;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex; flex-direction: column;
    }
    header {
      background: var(--header); color: #fff;
      padding: 10px 14px;
      display: flex; align-items: center; gap: 12px;
      box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    }
    .avatar {
      width: 38px; height: 38px; border-radius: 50%;
      background: var(--brand); color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 14px;
    }
    .header-info { flex: 1; }
    .header-info .name { font-weight: 600; font-size: 16px; }
    .header-info .status { font-size: 12px; opacity: 0.85; }
    .demo-pill {
      background: var(--brand); color: #fff;
      font-weight: 800; padding: 6px 12px; border-radius: 6px;
      font-size: 13px; letter-spacing: 1px;
      box-shadow: 0 2px 6px rgba(0,0,0,0.3);
    }
    .header-right { display: flex; align-items: center; gap: 12px; }
    .alias { font-size: 13px; opacity: 0.9; }
    .logout {
      background: transparent; color: #fff; border: 1px solid rgba(255,255,255,0.4);
      padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 12px;
    }
    .logout:hover { background: rgba(255,255,255,0.1); }
    .top-btn, .metaia {
      color:#fff; border:1px solid rgba(255,255,255,.45); background:transparent;
      padding:6px 9px; border-radius:6px; cursor:pointer; font-size:12px; text-decoration:none;
    }
    .disclaimer { background:#fff3cd; color:#664d03; padding:8px 14px; font-size:12px; text-align:center; border-bottom:1px solid #ead58c; }
    .examples { background:#f7f9f8; border-top:1px solid #d8e1df; padding:10px 12px 9px; }
    .examples-head { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
    .examples-title { color:#153f3a; font-size:13px; font-weight:800; }
    .examples-hint { color:var(--muted); font-size:11px; }
    .suggestions {
      display:grid; grid-auto-flow:column; grid-auto-columns:minmax(270px, 320px);
      gap:9px; overflow-x:auto; padding:0 1px 4px; scroll-snap-type:x proximity;
    }
    .suggestion {
      scroll-snap-align:start; border:1px solid #c6d5d2; background:#fff; color:#172b28;
      border-radius:12px; padding:10px 12px; cursor:pointer; text-align:left;
      box-shadow:0 1px 2px rgba(7,94,84,.06); font:inherit;
    }
    .suggestion:hover, .suggestion:focus-visible { border-color:#075e54; box-shadow:0 3px 10px rgba(7,94,84,.12); outline:none; }
    .case-kicker { display:block; color:#8b1a1a; font-size:10px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; }
    .case-title { display:block; margin-top:3px; color:#153f3a; font-size:13px; line-height:1.25; }
    .case-detail { display:-webkit-box; margin-top:4px; color:#667781; font-size:11px; line-height:1.3; overflow:hidden; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
    .case-action { display:block; margin-top:7px; color:#075e54; font-size:11px; font-weight:800; }
    main {
      flex: 1; overflow-y: auto; padding: 14px;
      background-image:
        linear-gradient(rgba(236,229,221,0.92), rgba(236,229,221,0.92)),
        url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80' viewBox='0 0 80 80'%3E%3Cpath fill='%23a39681' fill-opacity='0.18' d='M0 0h40v40H0zM40 40h40v40H40z'/%3E%3C/svg%3E");
    }
    .msg { display: flex; margin-bottom: 6px; }
    .msg.user { justify-content: flex-end; }
    .bubble {
      max-width: 78%;
      padding: 8px 12px 6px; border-radius: 8px; font-size: 15px; line-height: 1.4;
      box-shadow: 0 1px 1px rgba(0,0,0,0.08);
      position: relative; word-wrap: break-word;
      white-space: pre-wrap;
    }
    .msg.user .bubble { background: var(--user-bubble); border-top-right-radius: 0; }
    .msg.bot .bubble  { background: var(--bot-bubble); border-top-left-radius: 0; }
    .bubble a { color: #027eb5; text-decoration: underline; }
    .ts { font-size: 10px; color: var(--muted); margin-top: 4px; text-align: right; }
    .flag-btn {
      position: absolute; top: -10px; right: -10px;
      width: 26px; height: 26px; border-radius: 50%;
      background: #fff; border: 1px solid #ccc; cursor: pointer;
      display: none; align-items: center; justify-content: center;
      font-size: 13px; box-shadow: 0 2px 4px rgba(0,0,0,0.15);
    }
    .msg.bot:hover .flag-btn, .msg.bot .flag-btn:focus { display: flex; }
    .flag-btn.flagged { display: flex !important; background: #fff3cd; border-color: #f0ad4e; }
    @media (hover: none) { .msg.bot .flag-btn { display: flex; opacity: 0.6; } }
    .typing { padding: 8px 14px; color: var(--muted); font-size: 13px; font-style: italic; }
    footer {
      background: #f0f0f0; padding: 10px;
      display: flex; gap: 8px; align-items: flex-end;
      border-top: 1px solid #ddd;
    }
    textarea {
      flex: 1; resize: none; border: 1px solid #ccc; border-radius: 18px;
      padding: 10px 14px; font-size: 15px; font-family: inherit;
      max-height: 120px; outline: none;
    }
    textarea:focus { border-color: var(--header); }
    .send {
      background: var(--header); color: #fff; border: 0;
      width: 44px; height: 44px; border-radius: 50%; cursor: pointer;
      font-size: 18px; display: flex; align-items: center; justify-content: center;
    }
    .send:disabled { opacity: 0.5; cursor: not-allowed; }
    /* Modal de feedback */
    .modal-bg {
      position: fixed; inset: 0; background: rgba(0,0,0,0.5);
      display: none; align-items: center; justify-content: center; padding: 20px; z-index: 100;
    }
    .modal-bg.show { display: flex; }
    .modal {
      background: #fff; border-radius: 12px; padding: 22px; max-width: 420px; width: 100%;
    }
    .modal h3 { margin: 0 0 6px; color: var(--brand); }
    .modal p { margin: 0 0 14px; color: var(--muted); font-size: 14px; }
    .modal textarea {
      width: 100%; min-height: 80px; border: 1px solid #ccc; border-radius: 8px;
      padding: 10px; font-size: 14px; font-family: inherit;
    }
    .modal-actions { display: flex; gap: 10px; margin-top: 14px; justify-content: flex-end; }
    .modal-actions button {
      border: 0; padding: 10px 16px; border-radius: 8px; cursor: pointer; font-size: 14px;
    }
    .btn-cancel { background: #eee; color: #333; }
    .btn-send-fb { background: var(--brand); color: #fff; }
    /* Mobile */
    @media (max-width: 640px) {
      .alias { display: none; }
      .demo-pill { font-size: 11px; padding: 4px 8px; }
      .examples-hint { display:none; }
      .suggestions { grid-auto-columns:minmax(250px, 84vw); }
    }
  </style>
</head>
<body>
  <header>
    <div class="avatar">MI</div>
    <div class="header-info">
      <div class="name">MotorIA</div>
      <div class="status">Agente demostrativo de repuestos</div>
    </div>
    <div class="header-right">
      <div class="demo-pill">DEMO</div>
      <span class="alias" id="alias">...</span>
      <button class="top-btn" id="new-chat">Nueva conversación</button>
      <a class="metaia" href="/demo/api/cta">MetaIA ↗</a>
    </div>
  </header>
  <div class="disclaimer"><strong>Datos ficticios:</strong> productos, precios y stock son solo demostrativos. No se procesan compras ni reservas.</div>
  <main id="chat">
    <div class="msg bot">
      <div class="bubble">
        ¡Hola! Soy <b>MotorIA</b>. Contame qué repuesto buscás y todo lo que sepas del vehículo o motor.
        <div class="ts" id="ts0"></div>
      </div>
    </div>
  </main>
  <div class="typing" id="typing" style="display:none;">MotorIA está analizando la consulta...</div>
  <section class="examples" aria-labelledby="examples-title">
    <div class="examples-head">
      <span class="examples-title" id="examples-title">¿Querés ver cómo razona MotorIA?</span>
      <span class="examples-hint">Elegí un desafío. Podés editar el mensaje antes de enviarlo.</span>
    </div>
    <div class="suggestions" aria-label="Casos para probar">
      <button class="suggestion" data-prompt="Estoy reparando un Ford Mondeo 1.8 Zetec 16V. Para avanzar necesito que busques en el catálogo una junta de descarbonización Mondeo Zetec. Después contame qué incluye la opción encontrada y si trae retenes.">
        <span class="case-kicker">Caso 1 · Compatibilidad</span>
        <strong class="case-title">Una junta completa para un Mondeo</strong>
        <span class="case-detail">Buscá una opción para el Zetec 16V y pedí que incluya retenes.</span>
        <span class="case-action">Usar este ejemplo →</span>
      </button>
      <button class="suggestion" data-prompt="Estoy reparando el motor de un Chevrolet Corsa 1.8 y necesito mantener la medida estándar. Buscá en el catálogo aros Chevrolet Corsa 1.8 STD y contame qué opción encontrás.">
        <span class="case-kicker">Caso 2 · Motor y medida</span>
        <strong class="case-title">Elegir aros en medida estándar</strong>
        <span class="case-detail">Combiná vehículo, cilindrada, tipo de pieza y medida STD.</span>
        <span class="case-action">Usar este ejemplo →</span>
      </button>
      <button class="suggestion" data-prompt="Necesito hacer la distribución de un Chevrolet Corsa 1.6 8 válvulas y prefiero un kit que ya incluya la bomba de agua. ¿Hay alguna alternativa compatible en el catálogo?">
        <span class="case-kicker">Caso 3 · Kit completo</span>
        <strong class="case-title">Distribución y bomba en una sola búsqueda</strong>
        <span class="case-detail">Pedí una solución completa para un Corsa 1.6 8 válvulas.</span>
        <span class="case-action">Usar este ejemplo →</span>
      </button>
      <button class="suggestion" data-prompt="No tengo el código del motor: solo sé que el vehículo es una Renault Kangoo diésel 1.9. ¿Qué datos debería revisar para que puedas ayudarme a identificar el repuesto correcto?">
        <span class="case-kicker">Caso 4 · Datos incompletos</span>
        <strong class="case-title">No sé qué motor tiene el vehículo</strong>
        <span class="case-detail">Probá cómo el agente guía la conversación antes de recomendar.</span>
        <span class="case-action">Usar este ejemplo →</span>
      </button>
      <button class="suggestion" data-prompt="Tengo en el taller un retén viejo de 30 x 47 x 7 mm. Buscá exactamente un retén de árbol de levas Volvo 30x47x7 y explicame para qué modelos podría servir.">
        <span class="case-kicker">Caso 5 · Búsqueda por medida</span>
        <strong class="case-title">Identificar un retén por dimensiones</strong>
        <span class="case-detail">Encontrá una pieza aun cuando no conocés el vehículo de origen.</span>
        <span class="case-action">Usar este ejemplo →</span>
      </button>
    </div>
  </section>
  <footer>
    <textarea id="input" rows="1" maxlength="1000" placeholder="Escribí tu consulta (máx. 1.000 caracteres)..." autofocus></textarea>
    <button class="send" id="send" title="Enviar">➤</button>
  </footer>

  <div class="modal-bg" id="modal-bg">
    <div class="modal">
      <h3>🐛 Reportar este turno</h3>
      <p>¿Qué esperabas que respondiera el bot? (opcional)</p>
      <textarea id="fb-comment" placeholder="Esperaba que me diera el link del producto, pero pidió otro dato..."></textarea>
      <div class="modal-actions">
        <button class="btn-cancel" id="fb-cancel">Cancelar</button>
        <button class="btn-send-fb" id="fb-send">Enviar reporte</button>
      </div>
    </div>
  </div>

  <script>
    const chat = document.getElementById('chat');
    const input = document.getElementById('input');
    const send = document.getElementById('send');
    const typing = document.getElementById('typing');
    const aliasEl = document.getElementById('alias');
    const newChat = document.getElementById('new-chat');
    const modalBg = document.getElementById('modal-bg');
    const fbComment = document.getElementById('fb-comment');
    const fbCancel = document.getElementById('fb-cancel');
    const fbSend = document.getElementById('fb-send');

    let turnCounter = 0;       // contador global de turnos
    let lastUserText = '';     // último mensaje del visitante (para feedback)
    let pendingFlag = null;    // {turn, botText} mientras está abierto el modal

    function fmtTime(d) {
      d = d || new Date();
      const h = String(d.getHours()).padStart(2,'0');
      const m = String(d.getMinutes()).padStart(2,'0');
      return `${h}:${m}`;
    }
    document.getElementById('ts0').textContent = fmtTime();

    function escapeHtml(s) {
      return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function linkify(s) {
      return s.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    }
    function bold(s) {
      return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    }

    function addBubble(role, text, turnNumber) {
      const div = document.createElement('div');
      div.className = 'msg ' + role;
      const safe = bold(linkify(escapeHtml(text)));
      const ts = fmtTime();
      let flagBtn = '';
      if (role === 'bot') {
        flagBtn = `<button class="flag-btn" title="Reportar este turno" data-turn="${turnNumber}">🐛</button>`;
      }
      div.innerHTML = `<div class="bubble">${safe}<div class="ts">${ts}</div>${flagBtn}</div>`;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
      if (role === 'bot') {
        const btn = div.querySelector('.flag-btn');
        if (btn) {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            pendingFlag = { turn: parseInt(btn.dataset.turn, 10), botText: text, btnEl: btn };
            fbComment.value = '';
            modalBg.classList.add('show');
            fbComment.focus();
          });
        }
      }
    }

    async function loadMe() {
      try {
        const r = await fetch('/demo/api/me');
        if (r.ok) { const d = await r.json(); aliasEl.textContent = d.alias; }
        else if (r.status === 401) location.href = '/demo';
      } catch (e) {}
    }
    loadMe();

    async function sendMessage() {
      const text = input.value.trim();
      if (!text) return;
      send.disabled = true;
      addBubble('user', text, turnCounter);
      lastUserText = text;
      input.value = '';
      input.style.height = 'auto';
      typing.style.display = 'block';
      try {
        const r = await fetch('/demo/api/chat', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({text}),
        });
        typing.style.display = 'none';
        if (r.status === 401) { location.href = '/demo'; return; }
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          addBubble('bot', `(Error: ${d.detail || r.status})`, ++turnCounter);
          return;
        }
        const data = await r.json();
        // Igual que WhatsApp real: si el bot está apagado o fuera de horario
        // sin respuesta automática, simplemente no se muestra nada — silencio.
        // Si hay parts (off_message o respuesta del agente), se muestran tal cual.
        for (const part of (data.parts || [])) {
          turnCounter++;
          addBubble('bot', part, turnCounter);
        }
      } catch (e) {
        typing.style.display = 'none';
        addBubble('bot', '(Error de red. Probá de nuevo.)', ++turnCounter);
      } finally {
        send.disabled = false;
        input.focus();
      }
    }

    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });

    document.querySelectorAll('.suggestion').forEach(el => el.addEventListener('click', () => {
      input.value = el.dataset.prompt || '';
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
      input.focus();
    }));

    newChat.addEventListener('click', async () => {
      const r = await fetch('/demo/api/new-conversation', {method:'POST'}).catch(() => null);
      if (r && r.ok) location.reload();
    });

    // Feedback modal
    fbCancel.addEventListener('click', () => { modalBg.classList.remove('show'); pendingFlag = null; });
    modalBg.addEventListener('click', (e) => { if (e.target === modalBg) { modalBg.classList.remove('show'); pendingFlag = null; }});
    fbSend.addEventListener('click', async () => {
      if (!pendingFlag) return;
      const payload = {
        turn_number: pendingFlag.turn,
        bot_message: pendingFlag.botText || '',
        user_message: lastUserText || '',
        comment: fbComment.value.trim(),
      };
      try {
        const r = await fetch('/demo/api/feedback', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(payload),
        });
        if (r.ok && pendingFlag.btnEl) {
          pendingFlag.btnEl.classList.add('flagged');
          pendingFlag.btnEl.title = 'Reporte enviado';
        }
      } catch (e) {}
      modalBg.classList.remove('show'); pendingFlag = null;
    });
  </script>
</body>
</html>
"""


EXPLORER_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MotorIA — Desafío de catálogo</title>
  <style>
    :root{--ink:#172033;--muted:#667085;--paper:#f4f6f8;--panel:#fff;--line:#e4e7ec;--green:#075e54;--green2:#0b7668;--red:#8b1a1a;--violet:#5b45e0;--amber:#b45309}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--paper)}button,input,select{font:inherit}
    header{height:64px;background:var(--green);color:#fff;display:flex;align-items:center;gap:12px;padding:0 20px;position:sticky;top:0;z-index:20;box-shadow:0 2px 8px #0002}
    .logo{width:40px;height:40px;border-radius:12px;background:var(--red);display:grid;place-items:center;font-weight:900}.brand{flex:1}.brand b{display:block}.brand small{opacity:.8}.demo{background:#b42318;padding:6px 10px;border-radius:7px;font-weight:900;letter-spacing:.08em;font-size:12px}.toplink{color:#fff;text-decoration:none;border:1px solid #ffffff66;border-radius:8px;padding:8px 11px;font-size:12px}
    .notice{background:#fff5d6;color:#664d03;border-bottom:1px solid #eed78e;text-align:center;padding:7px 16px;font-size:12px}
    .layout{display:grid;grid-template-columns:290px minmax(0,1fr) 310px;min-height:calc(100vh - 97px)}
    .missions,.mentor{background:var(--panel);padding:18px;border-right:1px solid var(--line);overflow:auto}.mentor{border-right:0;border-left:1px solid var(--line)}
    .section-label{font-size:11px;color:var(--violet);font-weight:900;letter-spacing:.08em;text-transform:uppercase}.missions h1,.mentor h2{font-size:21px;margin:6px 0}.intro{color:var(--muted);font-size:13px;line-height:1.45;margin:0 0 14px}
    .mission-list{display:grid;gap:9px}.mission{border:1px solid var(--line);background:#fff;border-radius:12px;padding:12px;text-align:left;cursor:pointer;transition:.15s}.mission:hover{border-color:#a99ff2;transform:translateY(-1px)}.mission.active{border:2px solid var(--violet);background:#f7f5ff}.mission-top{display:flex;justify-content:space-between;gap:8px}.mission-title{font-size:14px;font-weight:800}.difficulty{font-size:10px;color:var(--amber);background:#fff4e8;border-radius:999px;padding:3px 6px;height:max-content}.mission p{color:var(--muted);font-size:11px;line-height:1.35;margin:6px 0 0}.skills{display:flex;gap:4px;flex-wrap:wrap;margin-top:8px}.skill{font-size:9px;background:#eef4f3;color:var(--green);border-radius:999px;padding:3px 6px}
    .catalog{padding:20px;min-width:0}.catalog-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:14px}.catalog h2{font-size:26px;margin:2px 0}.catalog-sub{color:var(--muted);font-size:13px}.count{font-size:12px;color:var(--green);font-weight:800;white-space:nowrap}
    .toolbar{display:grid;grid-template-columns:minmax(180px,1fr) 190px 170px auto;gap:8px;background:#fff;padding:10px;border:1px solid var(--line);border-radius:13px;margin-bottom:14px}.toolbar input,.toolbar select{width:100%;border:1px solid #d0d5dd;border-radius:9px;padding:10px;background:#fff;color:var(--ink)}.toolbar button{border:0;background:#edf2f1;color:var(--green);border-radius:9px;padding:0 13px;cursor:pointer;font-weight:700}
    .product-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(225px,1fr));gap:11px}.product{background:#fff;border:1px solid var(--line);border-radius:14px;padding:13px;display:flex;flex-direction:column;min-height:220px;transition:.15s}.product:hover{box-shadow:0 7px 20px #10182812;border-color:#b8c7c5}.product.selected{border:2px solid var(--violet);box-shadow:0 0 0 3px #5b45e018}.product-head{display:flex;justify-content:space-between;gap:8px}.category{font-size:9px;color:var(--red);font-weight:900;text-transform:uppercase;letter-spacing:.05em}.sku{font-size:9px;color:#98a2b3}.product h3{font-size:14px;line-height:1.32;margin:7px 0 10px}.specs{display:grid;gap:5px;margin-bottom:12px}.spec{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.spec b{color:#344054}.product-foot{display:flex;align-items:end;justify-content:space-between;gap:8px;margin-top:auto}.price{font-weight:900;color:var(--green)}.stock{font-size:9px;color:#027a48}.stock.out{color:#b42318}.choose{border:0;border-radius:8px;background:#edf2f1;color:var(--green);padding:7px 9px;font-size:10px;font-weight:800;cursor:pointer}.selected .choose{background:var(--violet);color:#fff}
    .empty{grid-column:1/-1;background:#fff;border:1px dashed #cbd5e1;border-radius:14px;padding:36px;text-align:center;color:var(--muted)}.pager{display:flex;justify-content:center;align-items:center;gap:10px;margin-top:16px}.pager button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:8px 12px;cursor:pointer}.pager button:disabled{opacity:.4}.pager span{font-size:12px;color:var(--muted)}
    .mentor-card{border:1px solid var(--line);border-radius:14px;padding:14px;margin-top:12px}.mentor-card.active{border-color:#a99ff2;background:#faf9ff}.mentor-card h3{font-size:15px;margin:0 0 7px}.brief{font-size:12px;line-height:1.5;color:#475467}.mentor-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:12px}.mentor-actions button,.validate{border:0;border-radius:9px;padding:9px;font-weight:800;cursor:pointer;font-size:11px}.hint-btn{background:#fff0d8;color:#92400e}.restart-btn{background:#eef2f6;color:#344054}
    .mentor-log{display:grid;gap:7px;margin-top:12px}.mentor-msg{font-size:11px;line-height:1.42;background:#edf7f5;color:#174c45;border-radius:10px 10px 10px 2px;padding:9px}.mentor-msg.error{background:#fff1f0;color:#9f1c13}.mentor-msg.success{background:#ecfdf3;color:#027a48;border:1px solid #abefc6}
    .selection-empty{text-align:center;color:var(--muted);font-size:12px;padding:18px 5px}.selected-product{display:none}.selected-product.show{display:block}.selected-product .label{font-size:10px;color:var(--violet);font-weight:900}.selected-product h3{font-size:14px;line-height:1.35;margin:5px 0}.validate{width:100%;background:var(--violet);color:#fff;margin-top:10px;padding:11px}.validate:disabled,.hint-btn:disabled{opacity:.45;cursor:not-allowed}
    .loading{opacity:.55;pointer-events:none}
    @media(max-width:1120px){.layout{grid-template-columns:260px minmax(0,1fr)}.mentor{grid-column:1/-1;border-left:0;border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr;gap:14px}.mentor>h2,.mentor>.section-label,.mentor>.intro{display:none}.mentor-card{margin:0}}
    @media(max-width:760px){header{padding:0 12px}.brand small,.toplink{display:none}.layout{display:block}.missions{border-right:0;border-bottom:1px solid var(--line);padding:14px}.mission-list{display:flex;overflow-x:auto}.mission{min-width:245px}.catalog{padding:14px}.catalog-head{align-items:flex-start}.toolbar{grid-template-columns:1fr 1fr}.toolbar .search{grid-column:1/-1}.product-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mentor{display:block;padding:14px}.mentor-card{margin-top:10px}}
    @media(max-width:480px){.product-grid{grid-template-columns:1fr}.toolbar{grid-template-columns:1fr}.toolbar .search{grid-column:auto}.catalog h2{font-size:22px}.notice{font-size:10px}}
  </style>
</head>
<body>
  <header><div class="logo">MI</div><div class="brand"><b>MotorIA</b><small>Desafío de catálogo</small></div><span class="demo">DEMO</span><a class="toplink" href="/demo/api/cta">Crear con MetaIA ↗</a></header>
  <div class="notice"><strong>Catálogo ficticio:</strong> 300 artículos, precios y stock creados exclusivamente para esta demostración.</div>
  <div class="layout">
    <aside class="missions">
      <span class="section-label">Paso 1</span><h1>Elegí una misión</h1>
      <p class="intro">Cada objetivo simula una consulta real de taller. La respuesta correcta está dentro del catálogo.</p>
      <div class="mission-list" id="mission-list"></div>
    </aside>
    <main class="catalog" id="catalog">
      <div class="catalog-head"><div><span class="section-label">Paso 2</span><h2>Investigá los artículos</h2><div class="catalog-sub">Buscá, filtrá y compará compatibilidades antes de elegir.</div></div><div class="count" id="count">Cargando catálogo…</div></div>
      <form class="toolbar" id="filters"><input class="search" id="query" placeholder="Buscar pieza, modelo, motor o medida…"><select id="category"><option value="">Todas las familias</option></select><select id="brand"><option value="">Todas las marcas</option></select><button type="button" id="clear">Limpiar</button></form>
      <div class="product-grid" id="product-grid"></div>
      <div class="pager"><button id="prev">← Anterior</button><span id="page-info"></span><button id="next">Siguiente →</button></div>
    </main>
    <aside class="mentor">
      <span class="section-label">Paso 3</span><h2>Resolvé con MotorIA</h2><p class="intro">Te orienta con pistas progresivas, pero no revela la pieza.</p>
      <section class="mentor-card" id="active-card"><div class="selection-empty" id="mission-empty">Elegí una misión para comenzar.</div><div id="mission-active" hidden><h3 id="active-title"></h3><div class="brief" id="active-brief"></div><div class="mentor-actions"><button class="hint-btn" id="hint">Pedir una pista</button><button class="restart-btn" id="restart">Reiniciar misión</button></div><div class="mentor-log" id="mentor-log"></div></div></section>
      <section class="mentor-card"><div class="selection-empty" id="selection-empty">Todavía no elegiste ningún artículo.</div><div class="selected-product" id="selected-product"><span class="label">TU ELECCIÓN</span><h3 id="selected-name"></h3><div class="brief" id="selected-meta"></div><button class="validate" id="validate">Validar mi elección</button></div></section>
    </aside>
  </div>
  <script>
    const state={challenges:[],active:null,selected:null,page:1,pages:1,facetsLoaded:false};
    const $=id=>document.getElementById(id);
    const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const money=value=>value?new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:0}).format(Number(value)):'Sin precio';
    const first=(attrs,key)=>((attrs||{})[key]||[]).slice(0,2).join(', ')||'—';
    async function api(url,options={}){const r=await fetch(url,options);if(r.status===401){location.href='/demo';throw new Error('Sesión vencida')}const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.detail||'No se pudo completar');return data}
    function body(data){return{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}}
    async function loadChallenges(){const data=await api('/demo/api/challenges');state.challenges=data.items;renderChallenges()}
    function renderChallenges(){$('mission-list').innerHTML=state.challenges.map(c=>`<button class="mission ${state.active?.id===c.id?'active':''}" data-id="${esc(c.id)}"><div class="mission-top"><span class="mission-title">${esc(c.title)}</span><span class="difficulty">${esc(c.difficulty)}</span></div><p>${esc(c.brief)}</p><div class="skills">${c.skills.map(s=>`<span class="skill">${esc(s)}</span>`).join('')}</div></button>`).join('');document.querySelectorAll('.mission').forEach(el=>el.onclick=()=>startMission(el.dataset.id))}
    async function startMission(id){const data=await api('/demo/api/challenge/start',body({challenge_id:id}));state.active=data.challenge;state.selected=null;renderChallenges();$('mission-empty').hidden=true;$('mission-active').hidden=false;$('active-card').classList.add('active');$('active-title').textContent=data.challenge.title;$('active-brief').textContent=data.challenge.brief;$('mentor-log').innerHTML=`<div class="mentor-msg">${esc(data.mentor_message)}</div>`;$('hint').disabled=false;clearSelection();window.scrollTo({top:0,behavior:'smooth'})}
    function filters(){return new URLSearchParams({query:$('query').value.trim(),category:$('category').value,brand:$('brand').value,page:String(state.page),per_page:'24'})}
    async function loadCatalog(){const grid=$('product-grid');grid.classList.add('loading');try{const data=await api('/demo/api/catalog?'+filters());state.pages=data.pages;if(!state.facetsLoaded){fillSelect('category',data.facets.categories,'Todas las familias');fillSelect('brand',data.facets.brands,'Todas las marcas');state.facetsLoaded=true}renderProducts(data.items);$('count').textContent=`${data.total} artículo${data.total===1?'':'s'} encontrados`;$('page-info').textContent=`Página ${data.page} de ${data.pages}`;$('prev').disabled=data.page<=1;$('next').disabled=data.page>=data.pages}catch(e){grid.innerHTML=`<div class="empty">${esc(e.message)}</div>`}finally{grid.classList.remove('loading')}}
    function fillSelect(id,values,label){const select=$(id);select.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('')}
    function renderProducts(items){$('product-grid').innerHTML=items.length?items.map(p=>{const a=p.attributes||{};const selected=state.selected?.id===p.id;return`<article class="product ${selected?'selected':''}" data-id="${esc(p.id)}"><div class="product-head"><span class="category">${esc((p.categories||[])[0]||'Repuesto')}</span><span class="sku">${esc(p.id)}</span></div><h3>${esc(p.name)}</h3><div class="specs"><div class="spec"><b>Marca:</b> ${esc(first(a,'Marca Vehiculo'))}</div><div class="spec"><b>Modelo:</b> ${esc(first(a,'Modelo'))}</div><div class="spec"><b>Motor:</b> ${esc(first(a,'motor'))}</div><div class="spec"><b>Medida:</b> ${esc(first(a,'Medida'))}</div></div><div class="product-foot"><div><div class="price">${money(p.price)}</div><div class="stock ${p.stock_status==='outofstock'?'out':''}">${p.stock_status==='outofstock'?'Stock ficticio: agotado':'Stock ficticio: disponible'}</div></div><button class="choose" data-id="${esc(p.id)}">${selected?'Elegido ✓':'Elegir'}</button></div></article>`}).join(''):'<div class="empty">No hay artículos con esos filtros. Probá quitando una condición.</div>';document.querySelectorAll('.choose').forEach(btn=>btn.onclick=()=>choose(btn.dataset.id,items.find(p=>p.id===btn.dataset.id)))}
    function choose(id,product){state.selected=product;$('selection-empty').hidden=true;$('selected-product').classList.add('show');$('selected-name').textContent=product.name;$('selected-meta').textContent=`${product.id} · ${(product.categories||[])[0]||'Repuesto'}`;$('validate').textContent='Validar mi elección';$('validate').disabled=!state.active;loadCatalog()}
    function clearSelection(){state.selected=null;$('selection-empty').hidden=false;$('selected-product').classList.remove('show');$('validate').textContent='Validar mi elección';loadCatalog()}
    function addMessage(text,type=''){const div=document.createElement('div');div.className='mentor-msg '+type;div.textContent=text;$('mentor-log').appendChild(div);div.scrollIntoView({behavior:'smooth',block:'nearest'})}
    $('hint').onclick=async()=>{if(!state.active)return;try{const d=await api('/demo/api/challenge/hint',body({challenge_id:state.active.id}));addMessage(`Pista ${d.number}: ${d.hint}`);$('hint').disabled=d.remaining===0}catch(e){addMessage(e.message,'error')}};
    $('restart').onclick=()=>state.active&&startMission(state.active.id);
    $('validate').onclick=async()=>{if(!state.active){addMessage('Primero elegí una misión.','error');return}if(!state.selected)return;try{const d=await api('/demo/api/challenge/check',body({challenge_id:state.active.id,product_id:state.selected.id}));addMessage(d.message,d.correct?'success':'error');if(d.correct){$('validate').textContent='Misión resuelta ✓';$('validate').disabled=true}}catch(e){addMessage(e.message,'error')}};
    let timer;$('query').oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{state.page=1;loadCatalog()},280)};$('category').onchange=$('brand').onchange=()=>{state.page=1;loadCatalog()};$('filters').onsubmit=e=>{e.preventDefault();state.page=1;loadCatalog()};$('clear').onclick=()=>{$('query').value='';$('category').value='';$('brand').value='';state.page=1;loadCatalog()};$('prev').onclick=()=>{state.page--;loadCatalog();$('catalog').scrollIntoView()};$('next').onclick=()=>{state.page++;loadCatalog();$('catalog').scrollIntoView()};
    Promise.all([loadChallenges(),loadCatalog()]).catch(e=>console.error(e));
  </script>
</body></html>"""

# La experiencia portfolio usa el explorador de catálogo y conserva el chat
# anterior únicamente como referencia histórica dentro de este artefacto.
CHAT_HTML = EXPLORER_HTML


ADMIN_LOGIN_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin — MotorIA DEMO</title>
  <style>
    :root { --brand:#8b1a1a; --brand-dark:#5e0e0e; --bg:#1f2937; --card:#fff; --muted:#667781; }
    * { box-sizing: border-box; }
    body {
      margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
      color:#fff; padding:20px;
    }
    .card {
      background: var(--card); color:#111; border-radius:14px; padding:32px 28px;
      max-width:380px; width:100%; box-shadow:0 12px 40px rgba(0,0,0,0.4);
    }
    h1 { margin:0 0 6px; font-size:22px; color: var(--brand); }
    p.sub { margin:0 0 22px; color: var(--muted); font-size:14px; }
    label { display:block; font-size:13px; color: var(--muted); margin-bottom:6px; }
    input[type=password] {
      width:100%; padding:12px 14px; border:1px solid #ccc; border-radius:8px;
      font-size:16px; outline:none;
    }
    input[type=password]:focus { border-color: var(--brand); }
    button {
      width:100%; margin-top:16px; padding:12px; border:0; border-radius:8px;
      background: var(--brand); color:#fff; font-weight:600; font-size:16px; cursor:pointer;
    }
    button:hover { background: var(--brand-dark); }
    .err { color:#c0392b; margin-top:10px; font-size:13px; min-height:18px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>MotorIA · Admin</h1>
    <p class="sub">Dashboard de monitoreo de sesiones de demo.</p>
    <label for="pw">Contraseña admin</label>
    <input id="pw" type="password" autofocus>
    <button id="enter">Entrar</button>
    <div class="err" id="err"></div>
  </div>
  <script>
    const pw = document.getElementById('pw');
    const btn = document.getElementById('enter');
    const err = document.getElementById('err');
    async function submit() {
      err.textContent=''; btn.disabled=true; btn.textContent='Entrando...';
      try {
        const r = await fetch('/admin/api/login', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({password: pw.value})
        });
        if (r.ok) { const d = await r.json(); location.href = d.redirect || '/admin/dashboard'; }
        else if (r.status === 401) err.textContent = 'Contraseña incorrecta.';
        else { const d = await r.json().catch(() => ({})); err.textContent = d.detail || 'Error.'; }
      } catch(e){ err.textContent = 'Error de red.'; }
      finally { btn.disabled=false; btn.textContent='Entrar'; }
    }
    btn.addEventListener('click', submit);
    pw.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  </script>
</body>
</html>
"""


ADMIN_DASHBOARD_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin Dashboard — MotorIA DEMO</title>
  <style>
    :root {
      --brand:#8b1a1a; --brand-dark:#5e0e0e;
      --bg:#0f172a; --panel:#1e293b; --panel2:#334155;
      --text:#e2e8f0; --muted:#94a3b8;
      --user-bubble:#dcf8c6; --bot-bubble:#fff;
      --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
    }
    * { box-sizing: border-box; }
    html, body { height:100%; margin:0; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text);
      display: flex; flex-direction: column;
    }
    header {
      background: var(--panel); padding: 12px 18px;
      display: flex; align-items: center; gap: 14px;
      border-bottom: 1px solid var(--panel2);
    }
    header h1 { margin: 0; font-size: 18px; color: #fff; }
    header .pill {
      background: var(--brand); color: #fff; padding: 4px 10px; border-radius: 6px;
      font-size: 11px; font-weight: 700; letter-spacing: 1px;
    }
    .right { margin-left: auto; display: flex; gap: 10px; align-items: center; }
    .right button {
      background: transparent; color: var(--text); border: 1px solid var(--panel2);
      padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 13px;
    }
    .right button:hover { background: var(--panel2); }
    .layout { flex: 1; display: flex; min-height: 0; }
    .sidebar {
      width: 360px; background: var(--panel); border-right: 1px solid var(--panel2);
      overflow-y: auto;
    }
    .sidebar .filter {
      padding: 10px 12px; border-bottom: 1px solid var(--panel2); font-size: 12px;
      color: var(--muted); display: flex; gap: 8px; align-items: center;
    }
    .sidebar .filter label { display: flex; gap: 4px; align-items: center; cursor: pointer; }
    .session-item {
      padding: 12px 14px; border-bottom: 1px solid var(--panel2); cursor: pointer;
      display: flex; flex-direction: column; gap: 4px;
    }
    .session-item:hover { background: var(--panel2); }
    .session-item.active { background: var(--panel2); border-left: 3px solid var(--brand); }
    .session-item.has-flag { border-left: 3px solid var(--bad); }
    .si-row1 { display: flex; justify-content: space-between; align-items: center; }
    .si-alias { font-weight: 600; color: #fff; font-size: 14px; }
    .si-time { font-size: 11px; color: var(--muted); }
    .si-row2 { display: flex; gap: 10px; font-size: 11px; color: var(--muted); }
    .si-flag { color: var(--bad); font-weight: 700; }
    .main {
      flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0;
    }
    .main .empty {
      flex: 1; display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 14px;
    }
    .main .header-detail {
      background: var(--panel); padding: 10px 16px;
      border-bottom: 1px solid var(--panel2);
      display: flex; gap: 14px; align-items: center;
    }
    .main .header-detail .alias-big { font-weight: 700; font-size: 16px; }
    .main .header-detail .meta { font-size: 12px; color: var(--muted); }
    .main .body {
      flex: 1; display: flex; min-height: 0;
    }
    .transcript {
      flex: 1; overflow-y: auto; padding: 14px; background: #ece5dd;
    }
    .msg { display:flex; margin-bottom: 6px; position: relative; }
    .msg.user { justify-content: flex-end; }
    .bubble {
      max-width: 78%; padding: 8px 12px 6px; border-radius: 8px;
      font-size: 14px; line-height: 1.4; color: #111;
      box-shadow: 0 1px 1px rgba(0,0,0,0.08); word-wrap: break-word; white-space: pre-wrap;
    }
    .msg.user .bubble { background: var(--user-bubble); border-top-right-radius: 0; }
    .msg.bot .bubble { background: var(--bot-bubble); border-top-left-radius: 0; }
    .bubble a { color: #027eb5; text-decoration: underline; }
    .ts { font-size: 10px; color: #667781; margin-top: 4px; text-align: right; }
    .turn-num { position: absolute; left: 4px; top: 4px; font-size: 9px; color: var(--muted); }
    .msg.flagged .bubble {
      box-shadow: 0 0 0 2px var(--bad), 0 1px 1px rgba(0,0,0,0.08);
    }
    .flag-comment {
      background: #fff3cd; border-left: 3px solid #f0ad4e;
      padding: 6px 10px; margin: 4px 0; font-size: 12px; color: #5c4400;
    }
    .debug {
      width: 320px; background: var(--panel); border-left: 1px solid var(--panel2);
      overflow-y: auto; padding: 14px; font-size: 12px;
    }
    .debug h3 { margin: 0 0 8px; font-size: 13px; color: var(--brand); text-transform: uppercase; letter-spacing: 1px; }
    .debug pre {
      background: #0f172a; color: #cbd5e1; padding: 8px; border-radius: 6px;
      font-size: 11px; overflow-x: auto; white-space: pre-wrap; word-break: break-all;
    }
    .debug .kv { display: flex; gap: 6px; margin-bottom: 4px; }
    .debug .kv .k { color: var(--muted); min-width: 80px; }
    .debug .kv .v { color: #fff; word-break: break-all; }
    /* Mobile */
    @media (max-width: 900px) {
      .layout { flex-direction: column; }
      .sidebar { width: 100%; max-height: 250px; }
      .main .body { flex-direction: column; }
      .debug { width: 100%; max-height: 260px; border-left: 0; border-top: 1px solid var(--panel2); }
    }
  </style>
</head>
<body>
  <header>
    <h1>MotorIA · Admin</h1>
    <span class="pill">DEMO</span>
    <div class="right">
      <span id="updated" style="color:var(--muted); font-size:12px;"></span>
      <button id="refresh">↻ Refrescar</button>
      <button id="logout">Salir</button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="filter">
        <label><input type="checkbox" id="only-flagged"> Solo con flags</label>
      </div>
      <div id="sessions"></div>
    </aside>
    <section class="main">
      <div class="empty" id="empty">Seleccioná una sesión a la izquierda para ver el transcript.</div>
      <div id="detail" style="display:none; flex:1; flex-direction: column; min-height:0;">
        <div class="header-detail">
          <div>
            <div class="alias-big" id="d-alias"></div>
            <div class="meta" id="d-meta"></div>
          </div>
        </div>
        <div class="body">
          <div class="transcript" id="d-transcript"></div>
          <aside class="debug">
            <h3>Estado del bot</h3>
            <div class="kv"><div class="k">state</div><div class="v" id="d-state">-</div></div>
            <div class="kv"><div class="k">phone</div><div class="v" id="d-phone">-</div></div>
            <h3 style="margin-top:14px;">Context</h3>
            <pre id="d-context">{}</pre>
            <h3 style="margin-top:14px;">Feedback</h3>
            <div id="d-feedback"></div>
          </aside>
        </div>
      </div>
    </section>
  </div>

  <script>
    const sessionsEl = document.getElementById('sessions');
    const onlyFlaggedEl = document.getElementById('only-flagged');
    const emptyEl = document.getElementById('empty');
    const detailEl = document.getElementById('detail');
    const dAlias = document.getElementById('d-alias');
    const dMeta = document.getElementById('d-meta');
    const dState = document.getElementById('d-state');
    const dPhone = document.getElementById('d-phone');
    const dContext = document.getElementById('d-context');
    const dTranscript = document.getElementById('d-transcript');
    const dFeedback = document.getElementById('d-feedback');
    const updatedEl = document.getElementById('updated');

    let activeId = null;
    let sessions = [];
    let pollTimer = null;

    function escapeHtml(s) {
      return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function linkify(s) { return s.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>'); }
    function bold(s) { return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>'); }
    function fmtTime(iso) {
      if (!iso) return '-';
      const d = new Date(iso);
      const pad = n => String(n).padStart(2,'0');
      return `${pad(d.getDate())}/${pad(d.getMonth()+1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    function relTime(iso) {
      if (!iso) return '';
      const diff = (Date.now() - new Date(iso).getTime()) / 1000;
      if (diff < 60) return `${Math.floor(diff)}s`;
      if (diff < 3600) return `${Math.floor(diff/60)}m`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h`;
      return `${Math.floor(diff/86400)}d`;
    }

    async function loadSessions() {
      try {
        const r = await fetch('/admin/api/sessions');
        if (r.status === 401) { location.href = '/admin'; return; }
        const data = await r.json();
        sessions = data.sessions || [];
        renderSessions();
        updatedEl.textContent = 'Actualizado ' + new Date().toLocaleTimeString();
      } catch (e) {}
    }

    function renderSessions() {
      let list = sessions;
      if (onlyFlaggedEl.checked) list = list.filter(s => s.flag_count > 0);
      if (list.length === 0) {
        sessionsEl.innerHTML = '<div style="padding:20px; color:var(--muted); font-size:13px;">No hay sesiones de demo todavía.</div>';
        return;
      }
      sessionsEl.innerHTML = list.map(s => {
        const flagCls = s.flag_count > 0 ? 'has-flag' : '';
        const activeCls = s.id === activeId ? 'active' : '';
        const flagHtml = s.flag_count > 0 ? `<span class="si-flag">🐛 ${s.flag_count}</span>` : '';
        return `
          <div class="session-item ${flagCls} ${activeCls}" data-id="${s.id}">
            <div class="si-row1">
              <span class="si-alias">${escapeHtml(s.alias)}</span>
              <span class="si-time">${relTime(s.updated_at)}</span>
            </div>
            <div class="si-row2">
              <span>${s.msg_count} msgs</span>
              <span>${escapeHtml(s.state || 'IDLE')}</span>
              ${flagHtml}
            </div>
          </div>`;
      }).join('');
      sessionsEl.querySelectorAll('.session-item').forEach(el => {
        el.addEventListener('click', () => selectSession(parseInt(el.dataset.id, 10)));
      });
    }

    async function selectSession(id) {
      activeId = id;
      renderSessions();
      emptyEl.style.display = 'none';
      detailEl.style.display = 'flex';
      try {
        const r = await fetch('/admin/api/session/' + id);
        if (r.status === 401) { location.href = '/admin'; return; }
        if (!r.ok) { dTranscript.textContent = 'Error cargando sesión.'; return; }
        const data = await r.json();
        renderDetail(data);
      } catch (e) {
        dTranscript.textContent = 'Error de red.';
      }
    }

    function renderDetail(data) {
      const { conversation, messages, feedback } = data;
      dAlias.textContent = conversation.alias;
      dMeta.textContent = `Última actividad: ${fmtTime(conversation.updated_at)} — ${messages.length} mensajes`;
      dState.textContent = conversation.state || 'IDLE';
      dPhone.textContent = conversation.phone_number;
      dContext.textContent = JSON.stringify(conversation.context || {}, null, 2);

      // Indexar feedback por turno (turnos del bot)
      const fbByTurn = {};
      for (const fb of feedback) {
        if (!fbByTurn[fb.turn_number]) fbByTurn[fb.turn_number] = [];
        fbByTurn[fb.turn_number].push(fb);
      }

      // Renderizar transcript
      let botTurn = 0;
      const html = messages.map(m => {
        const cls = m.role === 'user' ? 'user' : 'bot';
        let flaggedCls = '';
        let fbHtml = '';
        if (m.role === 'assistant') {
          botTurn++;
          if (fbByTurn[botTurn]) {
            flaggedCls = 'flagged';
            fbHtml = fbByTurn[botTurn].map(fb =>
              `<div class="flag-comment">🐛 <b>${escapeHtml(fb.alias)}</b>: ${escapeHtml(fb.comment || '(sin comentario)')}</div>`
            ).join('');
          }
        }
        const safe = bold(linkify(escapeHtml(m.content)));
        const turnLabel = m.role === 'assistant' ? `<span class="turn-num">#${botTurn}</span>` : '';
        return `
          <div class="msg ${cls} ${flaggedCls}">
            ${turnLabel}
            <div class="bubble">${safe}<div class="ts">${fmtTime(m.created_at)}</div></div>
          </div>${fbHtml}`;
      }).join('');
      dTranscript.innerHTML = html || '<div style="color:#667781; padding:20px;">Sesión sin mensajes todavía.</div>';
      dTranscript.scrollTop = dTranscript.scrollHeight;

      // Panel feedback
      if (feedback.length === 0) {
        dFeedback.innerHTML = '<div style="color:var(--muted); font-size:12px;">Sin reportes.</div>';
      } else {
        dFeedback.innerHTML = feedback.map(fb =>
          `<div style="background:#fff3cd; color:#5c4400; padding:6px 8px; margin-bottom:6px; border-radius:4px; font-size:11px;">
             <b>Turno #${fb.turn_number}</b> · ${fmtTime(fb.created_at)}<br>
             ${escapeHtml(fb.comment || '(sin comentario)')}
           </div>`
        ).join('');
      }
    }

    document.getElementById('refresh').addEventListener('click', () => {
      loadSessions();
      if (activeId) selectSession(activeId);
    });
    document.getElementById('logout').addEventListener('click', async () => {
      await fetch('/admin/api/logout', {method:'POST'}).catch(() => {});
      location.href = '/admin';
    });
    onlyFlaggedEl.addEventListener('change', renderSessions);

    function startPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(() => {
        loadSessions();
        if (activeId) selectSession(activeId);
      }, 3000);
    }
    loadSessions();
    startPolling();
  </script>
</body>
</html>
"""
