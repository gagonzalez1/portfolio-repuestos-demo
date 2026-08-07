#!/usr/bin/env python3
"""Carga explícita e idempotente de ``app/data/demo_catalog.json``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database  # noqa: E402
from app.demo_catalog import DEMO_CATALOG_PATH, seed_demo_catalog  # noqa: E402


async def _run(database_url: str, catalog_path: Path) -> None:
    database = Database(database_url)
    await database.connect()
    try:
        result = await seed_demo_catalog(database.pool, catalog_path)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEMO_CATALOG_PATH)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url o DATABASE_URL es obligatorio")
    asyncio.run(_run(args.database_url, args.catalog))


if __name__ == "__main__":
    main()
