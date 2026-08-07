import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query, Response
from fastapi.responses import RedirectResponse
import httpx

from app.bootstrap import build_agent_stack
from app.config import get_settings
from app.whatsapp import WhatsAppClient
from app.agent import Agent
from app.database import Database
from app.web_demo import router as demo_router, init_demo_schema
from app.admin_params import (
    router as params_router,
    init_agent_config_schema,
    get_config_from_db,
)
from app.bot_status import (
    STATUS_OFF_KILL,
    STATUS_OFF_HORARIO,
    STATUS_ON,
    evaluate_status,
    get_cached_config_sync,
    set_cache,
)

logger = logging.getLogger(__name__)


class _LiteLLMNoiseFilter(logging.Filter):
    """Suprime un mensaje espurio del logger interno de LiteLLM 1.63.2.

    Cuando LiteLLM intenta armar su `standard logging object` choca con un bug
    de typing en `TranscriptionCreateParams.__annotations__` y emite un ERROR
    + traceback enorme. No afecta el resultado de la llamada al LLM (la
    respuesta vuelve OK), solo ensucia los logs. Este filtro descarta ese
    mensaje específico sin silenciar otros errores reales del logger LiteLLM.
    Retirar este filtro cuando se pueda actualizar litellm a una versión
    >=1.65 que tenga el fix (respetando la regla de supply chain de CLAUDE.md).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Error creating standard logging object" not in msg


db: Database | None = None
agent: Agent | None = None
wa_client: WhatsAppClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, agent, wa_client
    settings = get_settings()

    logging.basicConfig(level=getattr(logging, settings.log_level))
    # Filtra el ruido del logger interno de LiteLLM (ver _LiteLLMNoiseFilter).
    logging.getLogger("LiteLLM").addFilter(_LiteLLMNoiseFilter())
    logger.info("Iniciando demo portfolio de repuestos")

    # El wiring (db, wc, agent, motor_expand, refresh loop) vive en bootstrap.
    async with build_agent_stack(settings) as (_db, _wc, _agent):
        db = _db
        agent = _agent

        # Exponer db y agent en app.state para que el router web_demo pueda
        # consumirlos sin replicar el wiring. NO se modifica nada del bot.
        app.state.db = _db
        app.state.agent = _agent
        app.state.catalog_client = _wc

        # Crear tabla auxiliar `demo_feedback` (idempotente). El bot la ignora.
        await init_demo_schema(_db)

        # Inicializar tabla `agent_config` + cache. Si no existe, inserta la fila
        # default de portfolio disponible 24/7; el panel mantiene el kill switch.
        await init_agent_config_schema(_db)

        # WhatsApp usa un HTTP client independiente porque su vida útil está
        # atada a la app, no al stack del agente (podría cambiar a futuro).
        wa_http = None
        if settings.whatsapp_enabled:
            if not settings.meta_access_token or not settings.meta_phone_number_id:
                raise RuntimeError(
                    "WHATSAPP_ENABLED=true requiere META_ACCESS_TOKEN y META_PHONE_NUMBER_ID"
                )
            wa_http = httpx.AsyncClient(timeout=30.0)
            wa_client = WhatsAppClient(
                access_token=settings.meta_access_token,
                phone_number_id=settings.meta_phone_number_id,
                api_base=settings.meta_api_base,
                http_client=wa_http,
            )
        else:
            wa_client = None
            logger.info("Canal WhatsApp deshabilitado")

        logger.info("Todos los componentes inicializados")

        try:
            yield
        finally:
            if wa_http is not None:
                await wa_http.aclose()
            logger.info("Shutdown completo")


app = FastAPI(title="MotorIA - Demo de repuestos", lifespan=lifespan)

# Demo web (frontend puro, no toca lógica del bot). Rutas: /demo, /admin.
app.include_router(demo_router)

# Panel admin de parámetros del bot. Rutas: /admin/params*. Login con
# ADMIN_PARAMETROS (env var separada de ADMIN_PASSWORD).
app.include_router(params_router)


@app.get("/", include_in_schema=False)
async def portfolio_root():
    return RedirectResponse("/demo", status_code=302)


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Verificación de webhook de Meta."""
    settings = get_settings()
    if not settings.whatsapp_enabled:
        return Response(content="Not Found", status_code=404)
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        logger.info("Webhook verificado correctamente")
        return Response(content=hub_challenge, media_type="text/plain")
    logger.warning("Verificación de webhook fallida")
    return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Recibe mensajes de WhatsApp via Meta Cloud API."""
    if not get_settings().whatsapp_enabled:
        return Response(content="Not Found", status_code=404)
    body = await request.json()

    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "no_messages"}

        message = messages[0]
        sender = message["from"]
        message_type = message["type"]

        logger.info(f"Mensaje de {sender}: tipo={message_type}")

        # ── Guard de estado del bot ──────────────────────────────────────
        # PRIMERO chequear si el bot está apagado o fuera de horario, ANTES
        # de cualquier acción visible al cliente (mark_as_read, send_text).
        # Si Nico apagó el bot, no queremos que mande "solo puedo leer texto"
        # cuando llega un audio/sticker — debe ser silencio total.
        # Política:
        #   - off_kill: silencio absoluto. Guardamos el mensaje del cliente
        #     (si es texto) para que Nico lo vea cuando vuelva, pero no
        #     respondemos NI ejecutamos el agente (no gastamos tokens) NI
        #     marcamos como leído.
        #   - off_horario + respond_when_off=true: respondemos con el
        #     off_message configurado. Igual guardamos mensaje + respuesta.
        #   - off_horario + respond_when_off=false: silencio (igual guardamos).
        #   - on: flow normal.
        cached = get_cached_config_sync()
        cache_hit = cached is not None
        if cached is None:
            # Cache expirado o miss en este worker. Releer desde DB y cachear.
            cached = await get_config_from_db(db)
            set_cache(cached)
        status, off_msg = evaluate_status(cached)
        logger.info(
            f"[bot_status] cache_hit={cache_hit} kill_switch={cached.get('kill_switch') if cached else None} "
            f"respond_when_off={cached.get('respond_when_off') if cached else None} "
            f"status={status}"
        )

        # Extract text content (None si no es texto — para guardar lo que se pueda)
        if message_type == "text":
            user_text = message["text"]["body"]
        else:
            user_text = None

        if status != STATUS_ON:
            # Guardamos siempre el mensaje del cliente — así Nico lo ve a la mañana.
            conversation = await db.get_or_create_conversation(sender)
            saved_text = user_text if user_text is not None else f"[{message_type}]"
            await db.save_message(conversation["id"], "user", saved_text)
            if off_msg:
                # Marcamos como leído solo si vamos a responder.
                await wa_client.mark_as_read(message["id"])
                await wa_client.send_text(sender, off_msg)
                await db.save_message(conversation["id"], "assistant", off_msg)
                logger.info(f"[bot_status={status}] enviado off_message a {sender}")
            else:
                logger.info(f"[bot_status={status}] silencio a {sender}")
            return {"status": status}

        # Bot ON: marcar como leído + manejar el tipo de mensaje
        await wa_client.mark_as_read(message["id"])

        if user_text is None:
            if message_type == "audio":
                await wa_client.send_text(
                    sender,
                    "🔇 Por el momento solo puedo leer mensajes de texto. "
                    "¿Podés escribirme qué repuesto necesitás? "
                    "Pronto vamos a activar la función de audio.",
                )
                return {"status": "audio_disabled"}
            await wa_client.send_text(
                sender,
                "Por el momento solo puedo leer mensajes de texto. "
                "¿Podés escribirme qué repuesto necesitás?",
            )
            return {"status": "unsupported_type"}

        # Get conversation context from DB
        conversation = await db.get_or_create_conversation(sender)

        # Process with agent
        response_text = await agent.process_message(
            user_text=user_text,
            sender=sender,
            conversation=conversation,
        )

        # Send response (split only if LLM used ---SPLIT--- delimiter)
        if "---SPLIT---" in response_text:
            parts = [p.strip() for p in response_text.split("---SPLIT---") if p.strip()]
            for i, part in enumerate(parts):
                await wa_client.send_text(sender, part)
                if i < len(parts) - 1:
                    await asyncio.sleep(1)
        else:
            await wa_client.send_text(sender, response_text)

        # Save full response to DB
        await db.save_message(conversation["id"], "user", user_text)
        await db.save_message(conversation["id"], "assistant", response_text)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error procesando webhook: {e}", exc_info=True)
        return {"status": "error"}



@app.get("/health")
async def health():
    # Deliberadamente no consulta al LLM, Meta ni al catálogo. Sirve como
    # liveness probe incluso si un proveedor externo está degradado.
    return {"status": "ok", "service": "portfolio-repuestos-demo"}
