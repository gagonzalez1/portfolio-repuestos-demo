"""Regresión local representativa sobre el catálogo reducido."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from app.demo_catalog import DEMO_CATALOG_PATH, DemoCatalogClient


ROOT = Path(__file__).resolve().parent


def _rows():
    products = json.loads(DEMO_CATALOG_PATH.read_text(encoding="utf-8"))["products"]
    return [
        {
            **product,
            "attributes": {
                "Marca Vehiculo": product["vehicle_brands"],
                "Modelo": product["vehicle_models"],
                "motor": product["engine_codes"],
                "Medida": product["measurements"],
            },
            "description_short": "Producto demostrativo",
        }
        for product in products
    ]


class _Connection:
    async def fetch(self, _query, *_args):
        return _rows()


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _Connection()


def test_30_representative_queries_find_the_expected_demo_product():
    cases = json.loads((ROOT / "demo_regression_cases.json").read_text(encoding="utf-8"))
    assert 25 <= len(cases) <= 40

    async def run():
        client = DemoCatalogClient(_Pool())
        hits = 0
        for expected_id, query in cases:
            products = await client.search_products(search=query, per_page=10)
            hits += expected_id in {product["id"] for product in products}
        return hits

    hits = asyncio.run(run())
    assert hits / len(cases) >= 0.90


def test_unknown_query_never_invents_a_product():
    products = asyncio.run(
        DemoCatalogClient(_Pool()).search_products(
            search="turbina nave espacial modelo inexistente", per_page=10
        )
    )
    assert products == []
