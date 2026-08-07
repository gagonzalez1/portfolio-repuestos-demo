"""Pruebas del constructor local y anónimo del catálogo demostrativo."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_demo_catalog.py"
SPEC = importlib.util.spec_from_file_location("build_demo_catalog", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _real_build():
    return builder.build_catalog(ROOT / "tests" / "catalog_dump.json", 300)


REAL_PAYLOAD, REAL_AUDIT = _real_build()


def test_real_catalog_has_target_and_expected_family_distribution():
    assert REAL_PAYLOAD["product_count"] == 300
    assert len(REAL_PAYLOAD["products"]) == 300
    assert REAL_AUDIT["coverage"]["families"] == builder.FAMILY_TARGETS
    assert REAL_AUDIT["privacy_checks"]["status"] == "passed"


def test_products_use_strict_allowlist_and_new_identifiers():
    products = REAL_PAYLOAD["products"]
    assert {key for product in products for key in product} == builder.PRODUCT_KEYS
    assert [product["id"] for product in products] == [
        f"DEMO-{index:04d}" for index in range(1, 301)
    ]
    assert len({product["id"] for product in products}) == 300
    serialized = json.dumps(REAL_PAYLOAD, ensure_ascii=False).casefold()
    for forbidden_key in (
        '"sku"',
        '"slug"',
        '"permalink"',
        '"price"',
        '"regular_price"',
        '"sale_price"',
        '"marca"',  # fabricante/proveedor; Marca Vehiculo sí se transforma
    ):
        assert forbidden_key not in serialized


def test_prices_and_stock_are_fictitious_and_in_normalized_domain():
    source = json.loads((ROOT / "tests" / "catalog_dump.json").read_text(encoding="utf-8"))
    source_prices = {row["price"] for row in source}
    demo_prices = [product["demo_price"] for product in REAL_PAYLOAD["products"]]
    assert all(isinstance(price, int) and 8_000 <= price <= 199_900 for price in demo_prices)
    assert all(price % 100 == 0 for price in demo_prices)
    # El formato y dominio hacen explícito que no se copiaron los strings del comercio.
    assert not any(isinstance(price, str) for price in demo_prices)
    assert {product["stock_status"] for product in REAL_PAYLOAD["products"]} == {
        "in_stock",
        "out_of_stock",
    }
    assert REAL_AUDIT["privacy_checks"]["original_prices_copied"] is False
    assert source_prices  # confirma que el fixture crudo realmente contenía precios


def test_fictional_price_and_stock_do_not_depend_on_source_commercial_values():
    source = {
        "name": "Bomba De Aceite Renault 1.9 F8Q",
        "categories": ["Bomba de aceite"],
        "attributes": {"Marca Vehiculo": ["Renault"], "motor": ["1.9 F8Q"]},
        "price": "1.00",
        "stock_status": "instock",
    }
    changed = {**source, "price": "99999999.00", "stock_status": "outofstock"}
    first = builder._transform_product(source, "bombas", "DEMO-0042", "test-v1")
    second = builder._transform_product(changed, "bombas", "DEMO-0042", "test-v1")
    assert first == second


def test_build_is_byte_reproducible_and_matches_versioned_artifacts():
    rebuilt_payload, rebuilt_audit = _real_build()
    committed_payload = json.loads(
        (ROOT / "app" / "data" / "demo_catalog.json").read_text(encoding="utf-8")
    )
    committed_audit = json.loads(
        (ROOT / "app" / "data" / "demo_catalog_audit.json").read_text(encoding="utf-8")
    )
    assert rebuilt_payload == REAL_PAYLOAD == committed_payload
    assert rebuilt_audit == REAL_AUDIT == committed_audit


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("name", "Escribinos a ventas@example.com"),
        ("name", "Ver https://tienda.example.com/producto"),
        ("name", "WhatsApp: 11 1234-5678"),
        ("name", "Oferta de Repuestos Daniel"),
    ],
)
def test_validation_fails_on_forbidden_contact_or_business_patterns(field, unsafe_value):
    payload = copy.deepcopy(REAL_PAYLOAD)
    payload["products"][0][field] = unsafe_value
    with pytest.raises(builder.CatalogBuildError, match="Patrones prohibidos"):
        builder.validate_catalog(payload)


def test_validation_fails_if_a_commercial_identifier_key_is_reintroduced():
    payload = copy.deepcopy(REAL_PAYLOAD)
    payload["products"][0]["sku"] = "ORIGINAL-123"
    with pytest.raises(builder.CatalogBuildError, match="Allowlist"):
        builder.validate_catalog(payload)


def test_audit_documents_local_source_and_zero_sensitive_matches():
    assert REAL_AUDIT["source"]["file"] == "catalog_dump.json"
    assert REAL_AUDIT["source"]["row_count"] == 3297
    assert REAL_AUDIT["source"]["excluded_unsafe_or_malformed_count"] == 3
    assert set(REAL_AUDIT["privacy_checks"]["forbidden_pattern_matches"].values()) == {0}
    assert REAL_AUDIT["privacy_checks"]["network_access_required"] is False
    assert REAL_AUDIT["output"]["removed_source_keys"] == [
        "id",
        "permalink",
        "price",
        "regular_price",
        "sale_price",
        "sku",
        "slug",
        "stock_status",
    ]
    assert REAL_AUDIT["output"]["discarded_source_attributes"] == ["Marca"]
