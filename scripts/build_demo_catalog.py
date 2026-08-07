#!/usr/bin/env python3
"""Construye el catálogo público de demo desde un dump local.

El transformador es deliberadamente estricto: sólo copia una allowlist de
atributos técnicos, crea identificadores/precios/stock ficticios y audita el
artefacto completo antes de escribirlo. No realiza solicitudes de red.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable


BUILDER_VERSION = "1"
DEFAULT_TARGET_COUNT = 300
DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "tests" / "catalog_dump.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "app" / "data" / "demo_catalog.json"
DEFAULT_AUDIT = (
    Path(__file__).resolve().parents[1] / "app" / "data" / "demo_catalog_audit.json"
)

FAMILY_TARGETS = {
    "juntas": 55,
    "aros_y_pistones": 45,
    "cojinetes_y_metales": 40,
    "retenes_y_sellos": 35,
    "bombas": 35,
    "valvulas_guias_y_botadores": 35,
    "bulones_y_fijaciones": 20,
    "otros": 35,
}

ALLOWED_ATTRIBUTE_MAP = {
    "Marca Vehiculo": "vehicle_brands",
    "Modelo": "vehicle_models",
    "motor": "engine_codes",
    "Combustible": "fuels",
    "Medida": "measurements",
    "Diametro Pistón": "piston_diameters",
    "Variante": "variants",
    "Año": "years",
}

PRODUCT_KEYS = {
    "id",
    "name",
    "normalized_name",
    "category",
    "vehicle_brands",
    "vehicle_models",
    "engine_codes",
    "displacement",
    "measurements",
    "compatibility",
    "search_terms",
    "demo_price",
    "stock_status",
    "catalog_version",
}

# Se buscan en valores y claves del artefacto final. Los teléfonos se detectan
# cuando tienen código internacional o un rótulo explícito para no confundir
# números de pieza/años con datos de contacto.
FORBIDDEN_PATTERNS = {
    "url_or_domain": re.compile(
        r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|com\.ar|net|org|io|pro)\b)", re.I
    ),
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "phone": re.compile(
        r"(?:\+\s*54[\s().-]*\d{2,4}[\s().-]*\d{3,4}[\s.-]*\d{4}"
        r"|\b(?:tel(?:e(?:fono|phone))?|whats(?:app)?|cel(?:ular)?)\b\D{0,8}"
        r"\d{2,4}[\s().-]*\d{3,4}[\s.-]*\d{4})",
        re.I,
    ),
    "original_business": re.compile(
        r"\b(?:repuestos\s*daniel|daniel|star\s*flex\s*up|warnes)\b", re.I
    ),
}

FORBIDDEN_KEY_FRAGMENTS = {
    "sku",
    "slug",
    "permalink",
    "regular_price",
    "sale_price",
    "woocommerce",
    "image",
    "supplier",
    "provider",
    "phone",
    "email",
}


class CatalogBuildError(ValueError):
    """El catálogo no puede publicarse porque incumple una regla de seguridad."""


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().lower()
    return re.sub(r"\s+", " ", value)


def _clean_values(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, (str, int, float)):
            continue
        value = re.sub(r"\s+", " ", str(raw)).strip()
        marker = value.casefold()
        if value and marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _family_for(product: dict[str, Any]) -> str:
    categories = " ".join(_clean_values(product.get("categories")))
    haystack = _normalized(f"{categories} {product.get('name', '')}")
    if "junta" in haystack:
        return "juntas"
    if any(term in haystack for term in ("aros", "subconjunto", "conjunto de motor", "piston")):
        return "aros_y_pistones"
    if any(term in haystack for term in ("cojinete", "axial", "metal de")):
        return "cojinetes_y_metales"
    if any(term in haystack for term in ("reten", "sellador", "sello")):
        return "retenes_y_sellos"
    if "bomba" in haystack:
        return "bombas"
    if any(
        term in haystack
        for term in ("valvula", "botador", "balancin", "arbol de levas", "guia de")
    ):
        return "valvulas_guias_y_botadores"
    if any(term in haystack for term in ("bulon", "fijacion", "tornillo")):
        return "bulones_y_fijaciones"
    return "otros"


def _difficulty(product: dict[str, Any]) -> str:
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    if _clean_values(attrs.get("Medida")):
        return "measurement"
    motors = _clean_values(attrs.get("motor"))
    if any(
        re.search(
            r"\b(?=[A-Z0-9-]{2,8}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b",
            motor,
        )
        for motor in motors
    ):
        return "engine_code"
    if len(_clean_values(attrs.get("Modelo"))) >= 4:
        return "broad_compatibility"
    return "basic"


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_key(product: dict[str, Any]) -> str:
    # El ID/SKU original sólo participa en el orden determinístico interno y
    # nunca se copia al resultado.
    return _stable_hash(
        {
            "builder": BUILDER_VERSION,
            "source_id": product.get("id"),
            "source_sku": product.get("sku"),
            "name": product.get("name"),
        }
    )


def _is_publishable_candidate(product: dict[str, Any]) -> bool:
    """Descarta filas corruptas/comerciales antes del muestreo.

    La auditoría informa cuántas quedaron fuera. La validación final sigue
    siendo obligatoria y falla ante cualquier patrón que sobreviva.
    """
    name = product.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 300:
        return False
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    permitted_source = {
        "name": name,
        "categories": _clean_values(product.get("categories")),
        "attributes": {
            key: _clean_values(attrs.get(key)) for key in ALLOWED_ATTRIBUTE_MAP
        },
    }
    serialized = json.dumps(permitted_source, ensure_ascii=False, sort_keys=True)
    return not any(pattern.search(serialized) for pattern in FORBIDDEN_PATTERNS.values())


def _allocate_quotas(available: dict[str, int], target_count: int) -> dict[str, int]:
    if target_count <= 0:
        raise CatalogBuildError("target_count debe ser mayor que cero")
    total_available = sum(available.values())
    if total_available < target_count:
        raise CatalogBuildError(
            f"Sólo hay {total_available} productos clasificables para un objetivo de {target_count}"
        )

    weight_total = sum(FAMILY_TARGETS.values())
    raw = {
        family: target_count * FAMILY_TARGETS[family] / weight_total for family in FAMILY_TARGETS
    }
    quotas = {
        family: min(available.get(family, 0), int(raw[family])) for family in FAMILY_TARGETS
    }
    remaining = target_count - sum(quotas.values())
    priority = sorted(
        FAMILY_TARGETS,
        key=lambda family: (-(raw[family] - int(raw[family])), family),
    )
    while remaining:
        progressed = False
        for family in priority:
            if quotas[family] < available.get(family, 0):
                quotas[family] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            raise CatalogBuildError("No se pudo completar la distribución solicitada")
    return quotas


def _select_stratified(products: list[dict[str, Any]], target_count: int) -> list[tuple[str, dict[str, Any]]]:
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        if isinstance(product, dict) and _is_publishable_candidate(product):
            families[_family_for(product)].append(product)
    quotas = _allocate_quotas({key: len(value) for key, value in families.items()}, target_count)

    selected: list[tuple[str, dict[str, Any]]] = []
    for family in FAMILY_TARGETS:
        strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for product in families.get(family, []):
            attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
            brands = _clean_values(attrs.get("Marca Vehiculo"))
            primary_brand = _normalized(brands[0]) if brands else "unknown"
            strata[(_difficulty(product), primary_brand)].append(product)
        queues = {
            key: deque(sorted(rows, key=_candidate_key))
            for key, rows in sorted(strata.items())
        }
        keys = list(queues)
        family_selected: list[dict[str, Any]] = []
        while len(family_selected) < quotas[family]:
            progressed = False
            for key in keys:
                if queues[key]:
                    family_selected.append(queues[key].popleft())
                    progressed = True
                    if len(family_selected) == quotas[family]:
                        break
            if not progressed:
                raise CatalogBuildError(f"No se pudo completar el estrato {family}")
        selected.extend((family, row) for row in family_selected)
    return selected


def _first_displacement(engines: Iterable[str], name: str) -> str | None:
    for text in [*engines, name]:
        match = re.search(r"\b(\d{1,2}[.,]\d)\b", text)
        if match:
            return match.group(1).replace(",", ".")
    return None


def _transform_product(
    source: dict[str, Any], family: str, demo_id: str, catalog_version: str
) -> dict[str, Any]:
    name = re.sub(r"\s+", " ", source["name"]).strip()
    attrs = source.get("attributes") if isinstance(source.get("attributes"), dict) else {}
    technical = {
        output_key: _clean_values(attrs.get(input_key))
        for input_key, output_key in ALLOWED_ATTRIBUTE_MAP.items()
    }
    categories = _clean_values(source.get("categories"))
    category = categories[0] if categories else family.replace("_", " ").title()
    measurements = [*technical["measurements"], *technical["piston_diameters"]]
    measurements = _clean_values(measurements)

    compatibility = {
        "brands": technical["vehicle_brands"],
        "models": technical["vehicle_models"],
        "engines": technical["engine_codes"],
        "fuels": technical["fuels"],
        "years": technical["years"],
        "variants": technical["variants"],
    }
    search_sources = [
        name,
        category,
        *technical["vehicle_brands"],
        *technical["vehicle_models"],
        *technical["engine_codes"],
        *technical["fuels"],
        *measurements,
        *technical["variants"],
    ]
    search_terms = list(dict.fromkeys(filter(None, (_normalized(value) for value in search_sources))))

    fiction_hash = int(_stable_hash({"id": demo_id, "name": name})[:16], 16)
    demo_price = 8_000 + (fiction_hash % 1_920) * 100
    stock_status = "out_of_stock" if fiction_hash % 7 == 0 else "in_stock"
    result = {
        "id": demo_id,
        "name": name,
        "normalized_name": _normalized(name),
        "category": category,
        "vehicle_brands": technical["vehicle_brands"],
        "vehicle_models": technical["vehicle_models"],
        "engine_codes": technical["engine_codes"],
        "displacement": _first_displacement(technical["engine_codes"], name),
        "measurements": measurements,
        "compatibility": compatibility,
        "search_terms": search_terms,
        "demo_price": demo_price,
        "stock_status": stock_status,
        "catalog_version": catalog_version,
    }
    return result


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def validate_catalog(payload: dict[str, Any]) -> dict[str, int]:
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise CatalogBuildError("El catálogo debe contener una lista no vacía de productos")
    if payload.get("product_count") != len(products):
        raise CatalogBuildError("product_count no coincide con products")
    identifiers: set[str] = set()
    for product in products:
        extra = set(product) - PRODUCT_KEYS
        missing = PRODUCT_KEYS - set(product)
        if extra or missing:
            raise CatalogBuildError(f"Allowlist de producto inválida: extra={extra}, faltan={missing}")
        product_id = product.get("id")
        if not isinstance(product_id, str) or not re.fullmatch(r"DEMO-\d{4}", product_id):
            raise CatalogBuildError(f"ID demostrativo inválido: {product_id!r}")
        if product_id in identifiers:
            raise CatalogBuildError(f"ID duplicado: {product_id}")
        identifiers.add(product_id)
        if product.get("stock_status") not in {"in_stock", "out_of_stock"}:
            raise CatalogBuildError(f"Stock inválido en {product_id}")
        if not isinstance(product.get("demo_price"), int):
            raise CatalogBuildError(f"Precio demo inválido en {product_id}")

    forbidden_keys = sorted(
        key
        for key in _walk_keys(payload)
        if any(fragment in _normalized(key).replace(" ", "_") for fragment in FORBIDDEN_KEY_FRAGMENTS)
    )
    if forbidden_keys:
        raise CatalogBuildError(f"Claves prohibidas detectadas: {forbidden_keys[:5]}")

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    matches = {
        label: len(pattern.findall(serialized)) for label, pattern in FORBIDDEN_PATTERNS.items()
    }
    detected = {label: count for label, count in matches.items() if count}
    if detected:
        raise CatalogBuildError(f"Patrones prohibidos detectados: {detected}")
    return matches


def build_catalog(
    source_path: Path = DEFAULT_SOURCE, target_count: int = DEFAULT_TARGET_COUNT
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_path = Path(source_path)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, list):
        raise CatalogBuildError("El dump de entrada debe ser una lista JSON")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    catalog_version = f"demo-v{BUILDER_VERSION}-{source_sha256[:12]}"

    selected = _select_stratified(source, target_count)
    products = [
        _transform_product(row, family, f"DEMO-{index:04d}", catalog_version)
        for index, (family, row) in enumerate(selected, start=1)
    ]
    payload = {
        "catalog_version": catalog_version,
        "product_count": len(products),
        "products": products,
    }
    pattern_counts = validate_catalog(payload)
    catalog_sha256 = _stable_hash(payload)
    audit = {
        "audit_version": 1,
        "builder_version": BUILDER_VERSION,
        "catalog_version": catalog_version,
        "source": {
            "file": source_path.name,
            "sha256": source_sha256,
            "row_count": len(source),
            "excluded_unsafe_or_malformed_count": sum(
                not isinstance(row, dict) or not _is_publishable_candidate(row) for row in source
            ),
        },
        "output": {
            "sha256": catalog_sha256,
            "product_count": len(products),
            "allowed_product_keys": sorted(PRODUCT_KEYS),
            "removed_source_keys": sorted(
                {key for row in source if isinstance(row, dict) for key in row} - {"name", "categories", "attributes"}
            ),
            "allowed_source_attributes": sorted(ALLOWED_ATTRIBUTE_MAP),
            "discarded_source_attributes": sorted(
                {
                    attribute
                    for row in source
                    if isinstance(row, dict) and isinstance(row.get("attributes"), dict)
                    for attribute in row["attributes"]
                }
                - set(ALLOWED_ATTRIBUTE_MAP)
            ),
        },
        "coverage": {
            "families": dict(Counter(family for family, _ in selected)),
            "source_categories": dict(Counter(product["category"] for product in products)),
            "stock_status": dict(Counter(product["stock_status"] for product in products)),
            "difficulty": dict(Counter(_difficulty(row) for _, row in selected)),
            "vehicle_brand_count": len({brand for product in products for brand in product["vehicle_brands"]}),
            "vehicle_model_count": len({model for product in products for model in product["vehicle_models"]}),
            "engine_count": len({engine for product in products for engine in product["engine_codes"]}),
            "products_with_measurements": sum(bool(product["measurements"]) for product in products),
        },
        "privacy_checks": {
            "status": "passed",
            "forbidden_pattern_matches": pattern_counts,
            "original_identifiers_copied": False,
            "original_prices_copied": False,
            "original_stock_copied": False,
            "network_access_required": False,
        },
    }
    return payload, audit


def write_catalog(
    source_path: Path = DEFAULT_SOURCE,
    output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
    target_count: int = DEFAULT_TARGET_COUNT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, audit = build_catalog(source_path, target_count)
    output_path = Path(output_path)
    audit_path = Path(audit_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--count", type=int, default=DEFAULT_TARGET_COUNT)
    args = parser.parse_args()
    payload, audit = write_catalog(args.source, args.output, args.audit, args.count)
    print(
        f"Catálogo {payload['catalog_version']}: {payload['product_count']} productos; "
        f"auditoría {audit['privacy_checks']['status']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
