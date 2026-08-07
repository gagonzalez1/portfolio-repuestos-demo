"""Construcción dinámica del índice motor_expand desde WooCommerce.

El agente usa un mapeo (marca, cilindrada, combustible) → lista de códigos de motor
completos ("1.4 8v Energy E7J", etc.) para expandir queries parciales como
"Renault 1.4 nafta" a los motores reales del catálogo.

Antes este mapeo estaba hardcodeado (7 entradas). Acá se construye consumiendo el
catálogo vía WC REST API una vez al arrancar la app y refrescando cada X horas.

Formato del índice:

    {
        ("renault", "1.4"): {
            "nafta": ["1.4 8v Energy E7J", "1.4 8v Fire", ...],
            "diesel": [],
        },
        ...
    }

Si un producto no tiene atributo Combustible, va a la key "_unknown".
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.woocommerce import WooCommerceClient, _BRAND_ALIASES

logger = logging.getLogger(__name__)

# Regex para extraer cilindrada del string del motor. Matchea decimal ("1.4",
# "1.9", "2.0") o entero de 3-4 digitos ("800", "850") para motores de baja
# cilindrada. Sin enteros, productos como "800 6v F8CV" no se indexaban
# correctamente por cilindrada y "800" del cliente nunca matcheaba.
_CILINDRADA_RE = re.compile(r"(\d+\.\d+|\d{3,4})")


# Códigos que indican que el motor es diesel. Usado para sanitizar la data de WC:
# un producto mal taggeado como "nafta" pero con código HDI/TDI/F9Q/etc. es un error
# humano del catálogo — lo forzamos al bucket diesel.
# NOTA: mantener sincronizado con agent.py::_DIESEL_CODES. En Fase 4 se consolidan.
_DIESEL_CODES: frozenset[str] = frozenset({
    "F8Q", "F9Q", "K9K", "G9U",
    "HDI", "TDI", "TDCI", "DCI", "SDI", "JTD", "CDI",
    "DW8", "DW10", "DV4", "DV6",
    "TUD", "TUD3", "XUD", "XUD7", "XUD9",
    "D7D", "4EE1",
})


def _has_diesel_code(motor_name: str) -> bool:
    """True si el nombre del motor contiene un código diesel conocido."""
    upper = motor_name.upper()
    return any(code in upper for code in _DIESEL_CODES)


# Tipo del índice final. Clave = (marca_norm, cilindrada). Valor = dict combustible → lista.
MotorExpandIndex = dict[tuple[str, str], dict[str, list[str]]]

# Índice marca_norm → lista ordenada de modelos presentes en el catálogo.
# Usado por la tool list_available_models cuando el cliente pregunta qué modelos
# cubre la tienda de una marca (ej: "qué modelos de Peugeot tienen?").
BrandModelsIndex = dict[str, list[str]]

# Índice inverso: modelo_norm (lowercase) → lista ordenada de marcas_norm que
# tienen ese modelo en el catálogo. Usado para inferir la marca cuando el cliente
# menciona solo un modelo ("Fox" → volkswagen). Solo se completa la marca
# automáticamente si el modelo es unívoco (len == 1); para modelos ambiguos
# se devuelve None y el flujo normal pregunta la marca al cliente.
ModelBrandIndex = dict[str, list[str]]


def _normalize_marca(raw: str) -> str:
    """Normaliza una marca a su forma canónica usando _BRAND_ALIASES de woocommerce.py."""
    if not raw:
        return ""
    low = raw.strip().lower()
    return _BRAND_ALIASES.get(low, low)


def _normalize_combustible(raw: str) -> str:
    """Normaliza combustible: lowercase, trim. Valores esperados: nafta, diesel, gnc."""
    return (raw or "").strip().lower() or "_unknown"


def _extract_cilindrada(motor: str) -> str | None:
    """Extrae la cilindrada ('1.4', '1.9', '2.0', ...) del string del motor."""
    if not motor:
        return None
    m = _CILINDRADA_RE.search(motor)
    return m.group(1) if m else None


async def build_motor_expand_from_wc(wc_client: WooCommerceClient) -> MotorExpandIndex:
    """Construye el índice motor_expand iterando todo el catálogo de WC.

    Para cada producto extrae los atributos `Marca Vehiculo`, `motor` y `Combustible`
    y cruza todas las combinaciones (un producto puede tener varias marcas/motores si
    es compatible con varios autos).

    Returns:
        Dict con clave (marca_norm, cilindrada) y valor dict combustible → lista ordenada
        y deduplicada de strings completos del motor.
    """
    # Set intermedio para deduplicar antes de convertir a listas ordenadas.
    raw_index: dict[tuple[str, str], dict[str, set[str]]] = {}

    product_count = 0
    skipped_no_motor = 0
    skipped_no_marca = 0

    async for product in wc_client.iter_all_products(per_page=100):
        product_count += 1
        attrs = product.get("attributes", {}) or {}

        marcas = attrs.get("Marca Vehiculo") or []
        motores = attrs.get("motor") or []
        combustibles = attrs.get("Combustible") or [""]  # "" si no está declarado

        if not marcas:
            skipped_no_marca += 1
            continue
        if not motores:
            skipped_no_motor += 1
            continue

        for marca in marcas:
            marca_norm = _normalize_marca(marca)
            if not marca_norm:
                continue
            for motor_full in motores:
                cil = _extract_cilindrada(motor_full)
                if not cil:
                    continue
                key = (marca_norm, cil)
                per_fuel = raw_index.setdefault(key, {})
                for comb in combustibles:
                    comb_norm = _normalize_combustible(comb)
                    per_fuel.setdefault(comb_norm, set()).add(motor_full.strip())

    logger.warning(
        f"[motor_catalog] productos procesados: {product_count}, "
        f"sin motor: {skipped_no_motor}, sin marca: {skipped_no_marca}, "
        f"combinaciones (marca, cilindrada): {len(raw_index)}"
    )

    # Sanitizado: si un código matchea un marcador diesel conocido, solo debe vivir
    # en el bucket "diesel". Los productos que el catálogo cargó con Combustible=Nafta
    # pero cuyo motor es claramente diesel (ej: HDI, F9Q, TDI) son errores de tagging.
    sanitized_moves = 0
    for key, fuels in raw_index.items():
        diesel_bucket = fuels.setdefault("diesel", set())
        for fuel_name in list(fuels.keys()):
            if fuel_name == "diesel":
                continue
            to_remove = {c for c in fuels[fuel_name] if _has_diesel_code(c)}
            if to_remove:
                diesel_bucket.update(to_remove)
                fuels[fuel_name] -= to_remove
                sanitized_moves += len(to_remove)
        # Si el bucket diesel quedó vacío (no había codes diesel), sacarlo
        if not fuels["diesel"]:
            del fuels["diesel"]

    if sanitized_moves:
        logger.warning(f"[motor_catalog] sanitizado: {sanitized_moves} codes movidos a bucket diesel")

    # Congelar sets a listas ordenadas por longitud (más específico = más corto primero)
    index: MotorExpandIndex = {}
    for key, fuels in raw_index.items():
        index[key] = {
            fuel: sorted(codes, key=lambda s: (len(s), s))
            for fuel, codes in fuels.items()
            if codes  # no incluir buckets vacíos
        }

    return index


def save_motor_expand_snapshot(index: MotorExpandIndex, path: Path | str) -> None:
    """Serializa el índice a JSON. Las tuplas key se serializan como 'marca||cilindrada'."""
    serialized: dict[str, Any] = {
        f"{marca}||{cil}": fuels for (marca, cil), fuels in index.items()
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[motor_catalog] snapshot guardado en {path} ({len(index)} entradas)")


def load_motor_expand_snapshot(path: Path | str) -> MotorExpandIndex:
    """Carga un snapshot previamente guardado. Retorna dict vacío si no existe."""
    path = Path(path)
    if not path.exists():
        logger.warning(f"[motor_catalog] snapshot no encontrado en {path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[motor_catalog] snapshot corrupto en {path}: {e}")
        return {}

    index: MotorExpandIndex = {}
    for k, fuels in data.items():
        if "||" not in k:
            continue
        marca, cil = k.split("||", 1)
        index[(marca, cil)] = {fuel: list(codes) for fuel, codes in fuels.items()}
    logger.info(f"[motor_catalog] snapshot cargado desde {path} ({len(index)} entradas)")
    return index


def lookup_motors(
    index: MotorExpandIndex,
    marca: str,
    cilindrada: str,
    combustible: str = "",
    max_results: int = 5,
) -> list[str]:
    """Consulta el índice y devuelve los códigos de motor matcheando (marca, cilindrada[, combustible]).

    Args:
        index: Índice construido por `build_motor_expand_from_wc`.
        marca: Marca cruda del usuario. Se normaliza internamente.
        cilindrada: '1.4', '1.6', etc.
        combustible: 'nafta' | 'diesel' | 'gnc' | ''.
            Si viene, filtra. Si no, devuelve todos los combustibles combinados.
        max_results: Límite superior del resultado.

    Returns:
        Lista ordenada (más específico primero) de códigos de motor completos.
        Vacío si no hay match.
    """
    if not marca or not cilindrada:
        return []
    key = (_normalize_marca(marca), cilindrada)
    fuels = index.get(key)
    if not fuels:
        return []

    if combustible:
        codes = list(fuels.get(_normalize_combustible(combustible), []))
        # Productos sin Combustible declarado siguen siendo candidatos válidos:
        # los sumamos al final para no perder el match.
        codes += [c for c in fuels.get("_unknown", []) if c not in codes]
    else:
        # Todos los combustibles combinados, deduplicado conservando orden.
        seen: set[str] = set()
        codes = []
        for fuel_codes in fuels.values():
            for c in fuel_codes:
                if c not in seen:
                    seen.add(c)
                    codes.append(c)

    return codes[:max_results]


async def build_brand_models_from_wc(wc_client: WooCommerceClient) -> BrandModelsIndex:
    """Construye el índice marca → modelos iterando el catálogo de WC.

    Mirror de `build_motor_expand_from_wc` pero para la tool `list_available_models`.
    Recorre el mismo catálogo; si a futuro el volumen crece, conviene unificar ambos
    builders en un solo pass (TODO).

    Returns:
        Dict con marca_norm → lista ordenada (case-insensitive) de modelos únicos.
    """
    raw: dict[str, set[str]] = {}
    product_count = 0

    async for product in wc_client.iter_all_products(per_page=100):
        product_count += 1
        attrs = product.get("attributes", {}) or {}

        marcas = attrs.get("Marca Vehiculo") or []
        modelos = attrs.get("Modelo") or []
        if not marcas or not modelos:
            continue

        for marca in marcas:
            marca_norm = _normalize_marca(marca)
            if not marca_norm:
                continue
            bucket = raw.setdefault(marca_norm, set())
            for modelo in modelos:
                modelo_clean = (modelo or "").strip()
                if modelo_clean:
                    bucket.add(modelo_clean)

    index: BrandModelsIndex = {
        marca: sorted(models, key=str.casefold)
        for marca, models in raw.items()
    }
    logger.warning(
        f"[motor_catalog] brand_models: {product_count} productos, "
        f"{len(index)} marcas con modelos"
    )
    return index


def save_brand_models_snapshot(index: BrandModelsIndex, path: Path | str) -> None:
    """Serializa el índice marca→modelos a JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[motor_catalog] brand_models snapshot guardado en {path} ({len(index)} marcas)")


def load_brand_models_snapshot(path: Path | str) -> BrandModelsIndex:
    """Carga snapshot de brand_models. Retorna dict vacío si no existe o está corrupto."""
    path = Path(path)
    if not path.exists():
        logger.warning(f"[motor_catalog] brand_models snapshot no encontrado en {path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[motor_catalog] brand_models snapshot corrupto en {path}: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    index: BrandModelsIndex = {marca: list(models) for marca, models in data.items() if isinstance(models, list)}
    logger.info(f"[motor_catalog] brand_models snapshot cargado desde {path} ({len(index)} marcas)")
    return index


def lookup_models(index: BrandModelsIndex, marca: str) -> list[str]:
    """Retorna la lista de modelos conocidos para una marca (case-insensitive).

    Args:
        index: Construido por `build_brand_models_from_wc`.
        marca: Marca cruda del usuario. Se normaliza con _BRAND_ALIASES.

    Returns:
        Lista ordenada de modelos únicos. Vacío si la marca no tiene entradas.
    """
    if not marca:
        return []
    return list(index.get(_normalize_marca(marca), []))


def build_model_brand_index(brand_models: BrandModelsIndex) -> ModelBrandIndex:
    """Invierte el índice marca → [modelos] a modelo_lower → [marcas_norm].

    Un modelo puede aparecer en varias marcas (ej: un hipotético "500" cargado
    tanto en Fiat como en Ford). El lookup usa esto para decidir si la marca
    se puede inferir unívocamente.

    Args:
        brand_models: Dict construido por `build_brand_models_from_wc`.

    Returns:
        Dict modelo_lower → lista ordenada de marcas normalizadas que lo contienen.
    """
    raw: dict[str, set[str]] = {}
    for marca_norm, modelos in brand_models.items():
        for modelo in modelos:
            key = (modelo or "").strip().lower()
            if not key:
                continue
            raw.setdefault(key, set()).add(marca_norm)

    index: ModelBrandIndex = {
        modelo: sorted(marcas) for modelo, marcas in raw.items()
    }
    logger.info(
        f"[motor_catalog] model_brand: {len(index)} modelos indexados "
        f"(invertidos desde {len(brand_models)} marcas)"
    )
    return index


def lookup_marca_by_modelo(index: ModelBrandIndex, modelo: str) -> str | None:
    """Retorna la marca única para un modelo, o None si es ambiguo / desconocido.

    Principio "no inventar, desambiguar": solo devuelve marca cuando el modelo
    aparece en UNA sola marca del catálogo. Si es ambiguo (Fiat 500 vs Ford 500)
    o inexistente, devuelve None y el flujo normal del agente pregunta al cliente.

    Args:
        index: Construido por `build_model_brand_index`.
        modelo: Modelo crudo del usuario (case-insensitive).

    Returns:
        Marca normalizada (ej: "volkswagen") si el modelo es unívoco. None en
        cualquier otro caso (ambiguo, desconocido, input vacío).
    """
    if not modelo:
        return None
    marcas = index.get(modelo.strip().lower(), [])
    if len(marcas) == 1:
        return marcas[0]
    return None
