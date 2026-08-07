"""Wiring aislado de la demostración de portfolio.

Este módulo no crea clientes HTTP ni acepta backends de comercio externos. El
catálogo se reconstruye desde el seed anonimizado y se consulta en PostgreSQL.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from app.agent import Agent
from app.config import Settings
from app.database import Database
from app.demo_catalog import DemoCatalogClient, seed_demo_catalog
from app.motor_catalog import (
    build_model_brand_index,
    load_brand_models_snapshot,
    load_motor_expand_snapshot,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
MOTOR_EXPAND_SNAPSHOT = DATA_DIR / "motor_expand_snapshot.json"
BRAND_MODELS_SNAPSHOT = DATA_DIR / "brand_models_snapshot.json"


@asynccontextmanager
async def build_agent_stack(
    settings: Settings,
    *,
    enable_refresh_loop: bool = False,
    http_timeout: float = 30.0,
) -> AsyncIterator[tuple[Database, DemoCatalogClient, Agent]]:
    """Construye el stack local sin realizar ninguna solicitud de red.

    Los parámetros legados se conservan en la firma para no romper scripts, pero
    un refresh o un backend distinto del demo se rechazan explícitamente.
    """
    del http_timeout
    if settings.catalog_backend != "demo_postgres":
        raise RuntimeError(
            "Este artefacto solo admite CATALOG_BACKEND=demo_postgres; "
            "los backends comerciales están deshabilitados"
        )
    if enable_refresh_loop and settings.motor_expand_refresh_hours > 0:
        raise RuntimeError("El refresh externo de catálogo no existe en modo portfolio")

    db = Database(settings.database_url)
    await db.connect()
    try:
        seed_result = await seed_demo_catalog(db.pool)
        logger.info(
            "Catálogo demo listo: status=%s count=%s checksum=%s",
            seed_result["status"],
            seed_result["count"],
            seed_result["checksum"][:12],
        )
        catalog_client = DemoCatalogClient(db)

        motor_expand_index = load_motor_expand_snapshot(MOTOR_EXPAND_SNAPSHOT)
        brand_models_index = load_brand_models_snapshot(BRAND_MODELS_SNAPSHOT)
        model_brand_index = (
            build_model_brand_index(brand_models_index) if brand_models_index else {}
        )

        agent = Agent(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            wc_client=catalog_client,
            db=db,
            escalation_phone="",
            motor_expand_index=motor_expand_index,
            brand_models_index=brand_models_index,
            model_brand_index=model_brand_index,
        )
        yield db, catalog_client, agent
    finally:
        await db.disconnect()
