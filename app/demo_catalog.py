"""Catálogo demostrativo respaldado exclusivamente por PostgreSQL.

El adaptador conserva el contrato que el agente recibía de WooCommerce, pero no
crea clientes HTTP ni conoce URLs o credenciales de la tienda original.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zlib
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

from app.woocommerce import (
    _BRAND_ALIASES,
    _finalize_with_clarification,
    _product_matches_calentador,
    _product_matches_measure,
    _product_matches_motor_code,
    _product_matches_retenes,
    _product_matches_valvulas,
    _strict_repuesto_match,
)


DEMO_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "demo_catalog.json"

DEMO_CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS demo_catalog_metadata (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    catalog_version TEXT NOT NULL,
    catalog_checksum TEXT NOT NULL,
    product_count INTEGER NOT NULL CHECK (product_count >= 0),
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS demo_products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    category TEXT NOT NULL,
    vehicle_brands JSONB NOT NULL DEFAULT '[]'::jsonb,
    vehicle_models JSONB NOT NULL DEFAULT '[]'::jsonb,
    engine_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    displacement TEXT,
    measurements JSONB NOT NULL DEFAULT '[]'::jsonb,
    compatibility JSONB NOT NULL DEFAULT '{}'::jsonb,
    search_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    description_short TEXT NOT NULL DEFAULT '',
    demo_price NUMERIC(12, 2),
    stock_status TEXT NOT NULL CHECK (stock_status IN ('in_stock', 'out_of_stock')),
    catalog_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_demo_products_normalized_name
    ON demo_products (normalized_name);
CREATE INDEX IF NOT EXISTS idx_demo_products_category
    ON demo_products (category);
CREATE INDEX IF NOT EXISTS idx_demo_products_vehicle_brands
    ON demo_products USING GIN (vehicle_brands);
CREATE INDEX IF NOT EXISTS idx_demo_products_vehicle_models
    ON demo_products USING GIN (vehicle_models);
CREATE INDEX IF NOT EXISTS idx_demo_products_engine_codes
    ON demo_products USING GIN (engine_codes);
CREATE INDEX IF NOT EXISTS idx_demo_products_measurements
    ON demo_products USING GIN (measurements);
CREATE INDEX IF NOT EXISTS idx_demo_products_search_terms
    ON demo_products USING GIN (search_terms);
"""

_SELECT_PRODUCTS_SQL = """
SELECT id, name, normalized_name, category, vehicle_brands, vehicle_models,
       engine_codes, displacement, measurements, compatibility, search_terms,
       attributes, description_short, demo_price, stock_status, catalog_version
FROM demo_products
ORDER BY id
"""

_UPSERT_PRODUCT_SQL = """
INSERT INTO demo_products (
    id, name, normalized_name, category, vehicle_brands, vehicle_models,
    engine_codes, displacement, measurements, compatibility, search_terms,
    attributes, description_short, demo_price, stock_status, catalog_version
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    normalized_name = EXCLUDED.normalized_name,
    category = EXCLUDED.category,
    vehicle_brands = EXCLUDED.vehicle_brands,
    vehicle_models = EXCLUDED.vehicle_models,
    engine_codes = EXCLUDED.engine_codes,
    displacement = EXCLUDED.displacement,
    measurements = EXCLUDED.measurements,
    compatibility = EXCLUDED.compatibility,
    search_terms = EXCLUDED.search_terms,
    attributes = EXCLUDED.attributes,
    description_short = EXCLUDED.description_short,
    demo_price = EXCLUDED.demo_price,
    stock_status = EXCLUDED.stock_status,
    catalog_version = EXCLUDED.catalog_version
"""


def normalize_search_text(value: Any) -> str:
    """Normaliza texto para búsqueda reproducible y sin extensiones de PG."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", text).lower().split())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, Decimal)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _first_list(product: dict, *keys: str) -> list[str]:
    for key in keys:
        if key in product:
            return _as_list(product.get(key))
    return []


def _canonical_attributes(product: dict) -> dict[str, list[str]]:
    raw = product.get("attributes") or {}
    attributes: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        attributes = {str(k): _as_list(v) for k, v in raw.items() if _as_list(v)}
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                values = _as_list(item.get("options", item.get("values")))
                if values:
                    attributes[str(item["name"])] = values

    compatibility = product.get("compatibility") or {}
    if not isinstance(compatibility, dict):
        compatibility = {}
    derived = {
        "Marca Vehiculo": _first_list(product, "vehicle_brands", "brands"),
        "Modelo": _first_list(product, "vehicle_models", "models"),
        "motor": _first_list(product, "engine_codes", "engines"),
        "Medida": _first_list(product, "measurements", "measures"),
        "Combustible": _as_list(compatibility.get("fuels", compatibility.get("fuel"))),
        "variante": _as_list(compatibility.get("variants", compatibility.get("variant"))),
        "Año": _as_list(compatibility.get("years", compatibility.get("year"))),
    }
    for key, values in derived.items():
        if values and key not in attributes:
            attributes[key] = values
    fuel = _first_list(product, "fuel", "fuels", "combustible")
    if fuel and "Combustible" not in attributes:
        attributes["Combustible"] = fuel
    return attributes


def _stock_for_db(value: Any) -> str:
    normalized = str(value or "in_stock").lower().replace("-", "_")
    return "out_of_stock" if normalized in {"outofstock", "out_of_stock", "agotado"} else "in_stock"


def _load_seed_document(path: Path) -> tuple[str, str, list[dict]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        products = document
        declared_version = None
    elif isinstance(document, dict):
        products = document.get("products", document.get("catalog", []))
        declared_version = document.get("catalog_version", document.get("version"))
        declared_count = document.get("product_count")
    else:
        raise ValueError("El catálogo demo debe ser una lista o un objeto con 'products'")
    if not isinstance(products, list) or not products:
        raise ValueError("El catálogo demo no contiene productos")
    if isinstance(document, dict) and declared_count is not None and declared_count != len(products):
        raise ValueError(
            f"product_count declara {declared_count}, pero el catálogo contiene {len(products)} productos"
        )

    canonical = json.dumps(products, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    version = str(declared_version or checksum[:16])
    return version, checksum, products


def _seed_record(product: dict, version: str) -> tuple:
    product_id = str(product.get("id") or "").strip()
    name = str(product.get("name") or "").strip()
    if not product_id.startswith("DEMO-"):
        raise ValueError(f"ID demo inválido: {product_id!r}")
    if not name:
        raise ValueError(f"Producto {product_id} sin nombre")

    attributes = _canonical_attributes(product)
    categories = _first_list(product, "categories", "category")
    category = categories[0] if categories else "Otros"
    brands = _first_list(product, "vehicle_brands", "brands") or attributes.get("Marca Vehiculo", [])
    models = _first_list(product, "vehicle_models", "models") or attributes.get("Modelo", [])
    engines = _first_list(product, "engine_codes", "engines") or attributes.get("motor", [])
    measurements = _first_list(product, "measurements", "measures") or attributes.get("Medida", [])
    search_terms = _first_list(product, "search_terms", "keywords")
    compatibility = product.get("compatibility") or {}
    if not isinstance(compatibility, (dict, list)):
        compatibility = {"text": str(compatibility)}
    price = product.get("demo_price", product.get("price"))

    return (
        product_id,
        name,
        str(product.get("normalized_name") or normalize_search_text(name)),
        category,
        brands,
        models,
        engines,
        str(product.get("displacement") or "") or None,
        measurements,
        compatibility,
        search_terms,
        attributes,
        str(product.get("description_short", product.get("short_description", "")) or ""),
        Decimal(str(price)) if price not in (None, "") else None,
        _stock_for_db(product.get("stock_status")),
        version,
    )


async def seed_demo_catalog(pool: Any, catalog_path: Path | str = DEMO_CATALOG_PATH) -> dict:
    """Carga el JSON de manera atómica e idempotente.

    Si el checksum y el conteo coinciden no escribe productos. Ante una versión
    nueva hace upsert y elimina filas que ya no pertenezcan al catálogo actual.
    """
    path = Path(catalog_path)
    version, checksum, products = _load_seed_document(path)
    records = [_seed_record(product, version) for product in products]
    ids = [record[0] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("El catálogo demo contiene IDs duplicados")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(DEMO_CATALOG_SCHEMA_SQL)
            # Evita que dos réplicas hagan el seed simultáneamente.
            await conn.execute("SELECT pg_advisory_xact_lock(1145392463)")
            current = await conn.fetchrow(
                "SELECT catalog_checksum, product_count FROM demo_catalog_metadata WHERE singleton = TRUE"
            )
            if current and current["catalog_checksum"] == checksum and current["product_count"] == len(records):
                return {"status": "unchanged", "catalog_version": version, "checksum": checksum, "count": len(records)}

            await conn.executemany(_UPSERT_PRODUCT_SQL, records)
            await conn.execute("DELETE FROM demo_products WHERE NOT (id = ANY($1::text[]))", ids)
            await conn.execute(
                """INSERT INTO demo_catalog_metadata
                       (singleton, catalog_version, catalog_checksum, product_count, seeded_at)
                   VALUES (TRUE, $1, $2, $3, NOW())
                   ON CONFLICT (singleton) DO UPDATE SET
                       catalog_version = EXCLUDED.catalog_version,
                       catalog_checksum = EXCLUDED.catalog_checksum,
                       product_count = EXCLUDED.product_count,
                       seeded_at = EXCLUDED.seeded_at""",
                version,
                checksum,
                len(records),
            )
    return {"status": "seeded", "catalog_version": version, "checksum": checksum, "count": len(records)}


def _row_to_product(row: Any) -> dict:
    raw = dict(row)
    price = raw.get("demo_price")
    stock = "outofstock" if raw.get("stock_status") == "out_of_stock" else "instock"
    return {
        "id": raw["id"],
        "name": raw.get("name", ""),
        # El ID anónimo funciona como clave de deduplicación sin recuperar el SKU original.
        "sku": raw["id"],
        "price": str(price) if price is not None else "",
        "regular_price": str(price) if price is not None else "",
        "sale_price": "",
        "stock_status": stock,
        "description_short": raw.get("description_short", ""),
        "categories": [raw.get("category", "Otros")],
        "attributes": raw.get("attributes") or {},
        "permalink": "",
        "_search_terms": raw.get("search_terms") or [],
        "_normalized_name": raw.get("normalized_name") or "",
    }


def _category_id(name: str) -> int:
    return zlib.crc32(normalize_search_text(name).encode("utf-8")) & 0x7FFFFFFF


def _search_haystack(product: dict) -> str:
    parts: list[str] = [product.get("_normalized_name", ""), product.get("name", ""), product.get("sku", "")]
    parts.extend(product.get("_search_terms", []))
    parts.extend(product.get("categories", []))
    for values in product.get("attributes", {}).values():
        parts.extend(_as_list(values))
    return normalize_search_text(" ".join(parts))


def _search_score(product: dict, query: str) -> int:
    normalized = normalize_search_text(query)
    if not normalized:
        return 1
    haystack = _search_haystack(product)
    tokens = normalized.split()
    if not all(token in haystack for token in tokens):
        return 0
    name = normalize_search_text(product.get("name"))
    return (100 if normalized in name else 0) + sum(10 for token in tokens if token in name) + len(tokens)


class DemoCatalogClient:
    """Implementación local del contrato de catálogo consumido por ``Agent``."""

    def __init__(self, database_or_pool: Any):
        self._database_or_pool = database_or_pool

    @property
    def _pool(self) -> Any:
        pool = getattr(self._database_or_pool, "_pool", None) or self._database_or_pool
        if pool is None:
            raise RuntimeError("Database.connect() debe ejecutarse antes de usar DemoCatalogClient")
        return pool

    async def _all_products(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_PRODUCTS_SQL)
        return [_row_to_product(row) for row in rows]

    async def search_products(
        self,
        search: str | None = None,
        category: int | None = None,
        sku: str | None = None,
        per_page: int = 5,
        page: int = 1,
    ) -> list[dict]:
        products = await self._all_products()
        if sku:
            wanted = normalize_search_text(sku)
            products = [p for p in products if normalize_search_text(p["sku"]) == wanted]
        if category is not None:
            products = [p for p in products if _category_id(p["categories"][0]) == int(category)]
        if search:
            scored = [(_search_score(product, search), product) for product in products]
            products = [product for score, product in sorted(scored, key=lambda item: (-item[0], item[1]["id"])) if score]
        start = max(page - 1, 0) * max(per_page, 0)
        return products[start : start + min(max(per_page, 0), 100)]

    async def browse_products(
        self,
        *,
        query: str = "",
        category: str = "",
        brand: str = "",
        page: int = 1,
        per_page: int = 24,
    ) -> dict:
        """Devuelve una vista paginada y filtrable para el explorador público."""
        all_products = await self._all_products()
        categories = sorted({name for product in all_products for name in product["categories"]})
        brands = sorted(
            {
                value
                for product in all_products
                for value in product["attributes"].get("Marca Vehiculo", [])
            }
        )

        products = all_products
        if query.strip():
            scored = [(_search_score(product, query), product) for product in products]
            products = [
                product
                for score, product in sorted(scored, key=lambda item: (-item[0], item[1]["id"]))
                if score
            ]
        if category.strip():
            wanted_category = normalize_search_text(category)
            products = [
                product
                for product in products
                if any(normalize_search_text(value) == wanted_category for value in product["categories"])
            ]
        if brand.strip():
            wanted_brand = normalize_search_text(brand)
            products = [
                product
                for product in products
                if any(
                    normalize_search_text(value) == wanted_brand
                    for value in product["attributes"].get("Marca Vehiculo", [])
                )
            ]

        safe_page = max(page, 1)
        safe_per_page = min(max(per_page, 1), 48)
        total = len(products)
        start = (safe_page - 1) * safe_per_page
        items = products[start : start + safe_per_page]
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "per_page": safe_per_page,
            "pages": max(1, (total + safe_per_page - 1) // safe_per_page),
            "facets": {"categories": categories, "brands": brands},
        }

    async def iter_all_products(self, per_page: int = 100, status: str = "publish") -> AsyncIterator[dict]:
        del per_page, status
        for product in await self._all_products():
            yield product

    async def get_product(self, product_id: str | int) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_PRODUCTS_SQL.replace("ORDER BY id", "WHERE id = $1"), str(product_id))
        return _row_to_product(row) if row else None

    async def get_attribute_terms(self, attribute_id: int, per_page: int = 100) -> list[dict]:
        attribute_names = {
            1: "Marca Vehiculo",
            2: "Modelo",
            3: "motor",
            4: "Medida",
            5: "Combustible",
        }
        name = attribute_names.get(int(attribute_id))
        if not name:
            return []
        values = sorted({value for product in await self._all_products() for value in product["attributes"].get(name, [])})
        return [
            {"id": index, "name": value, "slug": normalize_search_text(value).replace(" ", "-")}
            for index, value in enumerate(values[:per_page], 1)
        ]

    async def search_products_by_attributes(
        self,
        search: str | None = None,
        attribute_filters: dict | None = None,
        per_page: int = 5,
        page: int = 1,
        repuesto: str | None = None,
        medida: str | None = None,
        valvulas: str | None = None,
        retenes: str | None = None,
        calentador: str | None = None,
        motor_code: str | None = None,
    ) -> list[dict] | dict:
        products = await self._all_products()
        if search:
            products = [p for p in products if _search_score(p, search)]

        attr_name_map = {
            "pa_marca-vehiculo": "Marca Vehiculo",
            "pa_modelo": "Modelo",
            "pa_motor": "motor",
            "pa_combustible": "Combustible",
        }
        for slug, raw_value in (attribute_filters or {}).items():
            name = attr_name_map.get(slug, slug)
            wanted = normalize_search_text(raw_value)
            if slug == "pa_marca-vehiculo":
                wanted = normalize_search_text(_BRAND_ALIASES.get(str(raw_value).lower(), str(raw_value)))
            products = [
                p for p in products
                if not p["attributes"].get(name)
                or any(wanted in normalize_search_text(value) for value in p["attributes"][name])
            ]

        if repuesto:
            products = [p for p in products if _strict_repuesto_match(p, repuesto)]
        if valvulas:
            products = [p for p in products if _product_matches_valvulas(p, valvulas)]
        if retenes:
            products = [p for p in products if _product_matches_retenes(p, retenes)]
        if calentador:
            products = [p for p in products if _product_matches_calentador(p, calentador)]
        if motor_code:
            products = [p for p in products if _product_matches_motor_code(p, motor_code)]
        if medida:
            products = [p for p in products if _product_matches_measure(p, medida)]

        products.sort(key=lambda p: (-_search_score(p, search or ""), p["id"]))
        start = max(page - 1, 0) * max(per_page, 0)
        result = products[start : start + min(max(per_page, 0), 100)]
        return _finalize_with_clarification(
            result,
            repuesto=repuesto,
            medida=medida,
            valvulas=valvulas,
            retenes=retenes,
            calentador=calentador,
            motor_code=motor_code,
        )

    async def get_categories(self, per_page: int = 50) -> list[dict]:
        counts: dict[str, int] = {}
        for product in await self._all_products():
            for category in product["categories"]:
                counts[category] = counts.get(category, 0) + 1
        return [
            {"id": _category_id(name), "name": name, "count": count}
            for name, count in sorted(counts.items())[:per_page]
        ]
