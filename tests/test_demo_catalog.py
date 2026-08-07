"""Pruebas unitarias y contractuales del catálogo demo (sin red ni PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from app.demo_catalog import DemoCatalogClient, _seed_record, seed_demo_catalog


PRODUCT_ROWS = [
    {
        "id": "DEMO-0001",
        "name": "Junta Tapa De Cilindros Volkswagen Fox 1.6 8v",
        "normalized_name": "junta tapa de cilindros volkswagen fox 1 6 8v",
        "category": "Juntas",
        "vehicle_brands": ["Volkswagen"],
        "vehicle_models": ["Fox"],
        "engine_codes": ["1.6 8v EA111"],
        "displacement": "1.6",
        "measurements": ["STD"],
        "compatibility": {},
        "search_terms": ["junta fox"],
        "attributes": {
            "Marca Vehiculo": ["Volkswagen"],
            "Modelo": ["Fox"],
            "motor": ["1.6 8v EA111"],
            "Medida": ["STD"],
            "Combustible": ["Nafta"],
        },
        "description_short": "Producto ficticio",
        "demo_price": "15200.00",
        "stock_status": "in_stock",
        "catalog_version": "test-v1",
    },
    {
        "id": "DEMO-0002",
        "name": "Aros Renault 1.9 Diesel F8Q 0.50",
        "normalized_name": "aros renault 1 9 diesel f8q 0 50",
        "category": "Aros y pistones",
        "vehicle_brands": ["Renault"],
        "vehicle_models": ["Kangoo"],
        "engine_codes": ["1.9 8v Diesel F8Q"],
        "displacement": "1.9",
        "measurements": ["0.50"],
        "compatibility": {},
        "search_terms": ["aros f8q"],
        "attributes": {
            "Marca Vehiculo": ["Renault"],
            "Modelo": ["Kangoo"],
            "motor": ["1.9 8v Diesel F8Q"],
            "Medida": ["0.50"],
            "Combustible": ["Diesel"],
        },
        "description_short": "Producto ficticio",
        "demo_price": "21500.00",
        "stock_status": "out_of_stock",
        "catalog_version": "test-v1",
    },
]


class ReadConnection:
    async def fetch(self, query, *args):
        del query, args
        return PRODUCT_ROWS

    async def fetchrow(self, query, *args):
        del query
        return next((row for row in PRODUCT_ROWS if row["id"] == args[0]), None)


class ReadPool:
    @asynccontextmanager
    async def acquire(self):
        yield ReadConnection()


def test_contract_search_product_has_woocommerce_compatible_shape():
    client = DemoCatalogClient(ReadPool())
    products = asyncio.run(client.search_products(search="junta fox"))

    assert len(products) == 1
    assert set(products[0]) >= {
        "id", "name", "sku", "price", "regular_price", "sale_price",
        "stock_status", "description_short", "categories", "attributes", "permalink",
    }
    assert products[0]["id"] == products[0]["sku"] == "DEMO-0001"
    assert products[0]["permalink"] == ""
    assert products[0]["stock_status"] == "instock"


def test_search_by_attributes_and_get_product_are_compatible():
    client = DemoCatalogClient(ReadPool())

    products = asyncio.run(client.search_products_by_attributes(
        search="aros",
        attribute_filters={"pa_marca-vehiculo": "renault", "pa_motor": "F8Q"},
        repuesto="aros",
        medida="0.50",
    ))
    detail = asyncio.run(client.get_product("DEMO-0002"))

    assert isinstance(products, list)
    assert [product["id"] for product in products] == ["DEMO-0002"]
    assert detail is not None
    assert detail["stock_status"] == "outofstock"


def test_iter_categories_and_attribute_terms_contracts():
    client = DemoCatalogClient(ReadPool())

    async def collect():
        return [product async for product in client.iter_all_products()]

    products = asyncio.run(collect())
    categories = asyncio.run(client.get_categories())
    brands = asyncio.run(client.get_attribute_terms(1))

    assert len(products) == 2
    assert all(set(category) == {"id", "name", "count"} for category in categories)
    assert [term["name"] for term in brands] == ["Renault", "Volkswagen"]
    assert all(set(term) == {"id", "name", "slug"} for term in brands)


def test_browse_products_returns_filters_facets_and_pagination():
    client = DemoCatalogClient(ReadPool())

    result = asyncio.run(client.browse_products(
        query="f8q",
        category="Aros y pistones",
        brand="Renault",
        page=1,
        per_page=24,
    ))

    assert result["total"] == 1
    assert [product["id"] for product in result["items"]] == ["DEMO-0002"]
    assert result["pages"] == 1
    assert result["facets"]["categories"] == ["Aros y pistones", "Juntas"]
    assert result["facets"]["brands"] == ["Renault", "Volkswagen"]


def test_seed_record_accepts_builder_allowlist_shape():
    record = _seed_record({
        "id": "DEMO-0042",
        "name": "Bomba De Aceite Renault F8Q",
        "normalized_name": "bomba de aceite renault f8q",
        "category": "Bombas de aceite",
        "vehicle_brands": ["Renault"],
        "vehicle_models": ["Kangoo"],
        "engine_codes": ["1.9 8v Diesel F8Q"],
        "displacement": "1.9",
        "measurements": [],
        "compatibility": {"fuel": ["Diesel"]},
        "search_terms": ["bomba f8q"],
        "demo_price": "35000.00",
        "stock_status": "in_stock",
        "catalog_version": "ignored-per-item",
    }, "catalog-v1")

    assert record[0] == "DEMO-0042"
    assert record[4] == ["Renault"]
    assert record[5] == ["Kangoo"]
    assert record[6] == ["1.9 8v Diesel F8Q"]
    assert record[11]["Marca Vehiculo"] == ["Renault"]
    assert record[11]["Combustible"] == ["Diesel"]
    assert record[-1] == "catalog-v1"


class SeedConnection:
    def __init__(self):
        self.metadata = None
        self.records = []
        self.executemany_calls = 0

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, query, *args):
        if "INSERT INTO demo_catalog_metadata" in query:
            self.metadata = {"catalog_checksum": args[1], "product_count": args[2]}
        return "OK"

    async def fetchrow(self, query, *args):
        del query, args
        return self.metadata

    async def executemany(self, query, records):
        del query
        self.executemany_calls += 1
        self.records = list(records)


class SeedPool:
    def __init__(self):
        self.connection = SeedConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def test_seed_is_checksum_idempotent(tmp_path: Path):
    catalog = tmp_path / "demo_catalog.json"
    catalog.write_text(json.dumps({
        "catalog_version": "catalog-v1",
        "product_count": 1,
        "products": [{
            "id": "DEMO-0001",
            "name": "Junta demo",
            "normalized_name": "junta demo",
            "category": "Juntas",
            "vehicle_brands": [],
            "vehicle_models": [],
            "engine_codes": [],
            "displacement": None,
            "measurements": [],
            "compatibility": {},
            "search_terms": ["junta"],
            "demo_price": "1000.00",
            "stock_status": "in_stock",
            "catalog_version": "catalog-v1",
        }],
    }), encoding="utf-8")
    pool = SeedPool()

    first = asyncio.run(seed_demo_catalog(pool, catalog))
    second = asyncio.run(seed_demo_catalog(pool, catalog))

    assert first["status"] == "seeded"
    assert second["status"] == "unchanged"
    assert first["checksum"] == second["checksum"]
    assert pool.connection.executemany_calls == 1
