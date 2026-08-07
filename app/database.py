import json
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg

from app.demo_catalog import DEMO_CATALOG_SCHEMA_SQL

logger = logging.getLogger(__name__)


def _resolve_ssl_param(database_url: str):
    """Lee `sslmode` del query string del DATABASE_URL y lo traduce al param ssl
    que espera asyncpg.

    Convención libpq: sslmode=disable / allow / prefer / require / verify-ca /
    verify-full. asyncpg acepta 'disable', 'allow', 'prefer', 'require',
    'verify-ca', 'verify-full' o booleanos. Si no se especifica, default
    'require' (compat con deploy en Railway, que usa SSL).

    Retorna (ssl_value, cleaned_url) — el URL limpio sin el sslmode, porque
    asyncpg interpreta el query string y duplicarlo confunde al driver.
    """
    parts = urlsplit(database_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    sslmode_values = query.pop("sslmode", None)
    sslmode = sslmode_values[0].lower() if sslmode_values else "require"

    # Reconstruir URL sin sslmode
    new_query = "&".join(
        f"{k}={v}" for k, vs in query.items() for v in vs
    )
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    if sslmode in ("disable", "allow"):
        return False, cleaned
    if sslmode == "prefer":
        # asyncpg no tiene "prefer" nativo; aproximamos con False (no fuerza SSL)
        return False, cleaned
    # require / verify-ca / verify-full → asyncpg los acepta como string
    return sslmode, cleaned

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    state VARCHAR(50) DEFAULT 'IDLE',
    context JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role VARCHAR(10) NOT NULL,  -- 'user' o 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_phone ON conversations(phone_number);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


class Database:
    """Maneja el estado de conversaciones en PostgreSQL."""

    def __init__(self, database_url: str):
        ssl_value, cleaned_url = _resolve_ssl_param(database_url)
        self._database_url = cleaned_url
        self._ssl = ssl_value
        self._pool: asyncpg.Pool | None = None

    async def connect(self):
        """Conecta y crea tablas si no existen."""
        async def _init_conn(conn):
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

        self._pool = await asyncpg.create_pool(self._database_url, min_size=2, max_size=10, init=_init_conn, ssl=self._ssl)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            await conn.execute(DEMO_CATALOG_SCHEMA_SQL)
        logger.info("Base de datos conectada y schema creado")

    async def disconnect(self):
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        """Expone el pool conectado a adaptadores de persistencia locales."""
        if self._pool is None:
            raise RuntimeError("Database.connect() debe ejecutarse antes de acceder al pool")
        return self._pool

    async def get_or_create_conversation(self, phone_number: str) -> dict:
        """Obtiene o crea una conversación por número de teléfono."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversations WHERE phone_number = $1",
                phone_number,
            )
            if row:
                return dict(row)

            row = await conn.fetchrow(
                """INSERT INTO conversations (phone_number)
                   VALUES ($1) RETURNING *""",
                phone_number,
            )
            return dict(row)

    async def update_conversation_state(
        self, conversation_id: int, state: str, context: dict | None = None
    ):
        """Actualiza el estado de una conversación."""
        async with self._pool.acquire() as conn:
            if context is not None:
                await conn.execute(
                    """UPDATE conversations
                       SET state = $1, context = $2, updated_at = $3
                       WHERE id = $4""",
                    state,
                    context,
                    datetime.now(timezone.utc),
                    conversation_id,
                )
            else:
                await conn.execute(
                    """UPDATE conversations
                       SET state = $1, updated_at = $2
                       WHERE id = $3""",
                    state,
                    datetime.now(timezone.utc),
                    conversation_id,
                )

    async def save_message(self, conversation_id: int, role: str, content: str):
        """Guarda un mensaje en el historial."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO messages (conversation_id, role, content)
                   VALUES ($1, $2, $3)""",
                conversation_id,
                role,
                content,
            )

    async def get_recent_messages(
        self, conversation_id: int, limit: int = 10
    ) -> list[dict]:
        """Obtiene los últimos N mensajes de una conversación."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT role, content FROM messages
                   WHERE conversation_id = $1
                   ORDER BY created_at DESC LIMIT $2""",
                conversation_id,
                limit,
            )
            # Return in chronological order
            return [dict(r) for r in reversed(rows)]

    async def reset_conversation(self, phone_number: str) -> None:
        """Resetea por completo la conversación de un número:
            - borra todos los mensajes del historial
            - limpia state y context de la fila de conversations

        Pensado para testing manual (ej. `/reset` en scripts/chat.py): después
        de llamar esto, la próxima llamada a process_message arranca limpia
        sin rastros del turno anterior.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM conversations WHERE phone_number = $1",
                    phone_number,
                )
                if not row:
                    return
                conv_id = row["id"]
                await conn.execute(
                    "DELETE FROM messages WHERE conversation_id = $1",
                    conv_id,
                )
                await conn.execute(
                    """UPDATE conversations
                       SET state = 'IDLE', context = '{}', updated_at = $1
                       WHERE id = $2""",
                    datetime.now(timezone.utc),
                    conv_id,
                )
