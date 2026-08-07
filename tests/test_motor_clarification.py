"""Regresión del feature `needs_clarification = 'motor'`.

Caso testigo (SRCH-A-0026 del set Layer 2): Peugeot 207 1.6 16v Nafta tiene
en catálogo dos motores físicamente distintos con misma cilindrada+válvulas+
combustible:
- TU5JP4 (aspirado naftero clásico, juego JR-511-15)
- THP EP6DT (turbo naftero moderno, juego JR-571-15)

Sin el detector, el bot devuelve solo uno según el ranking del fulltext de WC,
perdiendo silenciosamente la otra mitad del catálogo. El fix detecta los 2+
códigos de motor en el set y dispara needs_clarification='motor' con las
opciones para que el bot pregunte cuál motor tiene el cliente.

Estos tests usan productos mock construidos con los strings reales de pa_motor
que aparecen en `tests/catalog_dump.json`. No dependen de la red.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.woocommerce import (  # noqa: E402
    _detect_motor_variants,
    _parse_motor_string,
    _product_matches_motor_code,
    _get_product_motor_codes,
)


# ── Fixtures: juegos de juntas Peugeot 207 1.6 16v Nafta ─────────────────────

def _make_jr_511() -> dict:
    """JR-511-15 — juego de juntas Peugeot 1.6 16v TU5JP4 (aspirado)."""
    return {
        "id": 50001,
        "sku": "JR-511-15",
        "name": "Juego De Juntas Completo Peugeot 1.6 16v TU5JP4 - Sin Retenes",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Peugeot"],
            "Modelo": ["206", "207", "307", "Partner"],
            "motor": ["1.6 16v TU5JP4"],
            "Combustible": ["Nafta"],
        },
    }


def _make_jr_571() -> dict:
    """JR-571-15 — juego de juntas Peugeot 1.6 16v THP EP6DT (turbo)."""
    return {
        "id": 50002,
        "sku": "JR-571-15",
        "name": "Juego De Juntas Completo Citroen 1.6 16v THP EP6DT - Sin Retenes",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Peugeot"],
            "Modelo": ["207", "2007", "3008", "308", "5008", "508", "RCZ"],
            "motor": ["1.6 16v THP EP6DT"],
            "Combustible": ["Nafta"],
        },
    }


def _make_jr_511r() -> dict:
    """Variante 'con retenes' del JR-511-15 — comparte motor pero distinto SKU."""
    return {
        "id": 50003,
        "sku": "JR-511-15R",
        "name": "Juego De Juntas Completo Peugeot 1.6 16v TU5JP4 - Con Retenes",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Peugeot"],
            "Modelo": ["206", "207", "307", "Partner"],
            "motor": ["1.6 16v TU5JP4"],
            "Combustible": ["Nafta"],
        },
    }


def _make_multi_motor() -> dict:
    """Producto que lista múltiples motores compatibles en el mismo string.

    Patrón real en el catálogo: retenes y juntas universales suelen tener
    varios motores en pa_motor. El parser debe extraerlos todos.
    """
    return {
        "id": 50004,
        "sku": "RT-9192POL",
        "name": "Reten De Distribucion Peugeot Multimotor",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Peugeot"],
            "motor": ["1.4 8v TU3JP 1.5 8v TU4 1.6 16v TU5JP4 1.6 8v TU5JP"],
            "Combustible": ["Nafta"],
        },
    }


def _make_reten_compat_misma_dim() -> dict:
    """Reten universal compatible con dos motores DE LA MISMA (cilindrada, válvulas).

    Patrón real: un reten que sirve para F8Q y F9Q (ambos 1.9 8v) — el producto
    es universal para esa cilindrada+válvulas, no requiere desambiguación.
    """
    return {
        "id": 50005,
        "sku": "RT-COMPAT",
        "name": "Reten Renault F8Q/F9Q Universal",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Renault"],
            "motor": ["1.9 8v F8Q 1.9 8v F9Q"],
            "Combustible": ["Diesel"],
        },
    }


def _make_tapa_f8q_pura() -> dict:
    """Tapa con un único motor (1.9 8v F8Q) — producto puro, NO universal."""
    return {
        "id": 50006,
        "sku": "ZL00129",
        "name": "Tapa De Cilindros Renault 1.9 8v Diesel F8Q Calentador Horizontal",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": ["Renault"],
            "Modelo": ["Kangoo"],
            "motor": ["1.9 8v Diesel F8Q"],
            "Combustible": ["Diesel"],
        },
    }


# ── Tests del parser ─────────────────────────────────────────────────────────

def test_parse_motor_simple():
    out = _parse_motor_string("1.6 16v TU5JP4")
    assert out == [("1.6", "16v", "TU5JP4")]


def test_parse_motor_dos_palabras():
    out = _parse_motor_string("1.6 16v THP EP6DT")
    assert out == [("1.6", "16v", "THP EP6DT")]


def test_parse_motor_multiple():
    out = _parse_motor_string(
        "1.4 8v TU3JP 1.5 8v TU4 1.6 16v TU5JP4 1.6 8v TU5JP"
    )
    assert ("1.4", "8v", "TU3JP") in out
    assert ("1.5", "8v", "TU4") in out
    assert ("1.6", "16v", "TU5JP4") in out
    assert ("1.6", "8v", "TU5JP") in out
    assert len(out) == 4


def test_parse_motor_vacio():
    assert _parse_motor_string("") == []
    assert _parse_motor_string("solo texto sin motor") == []


# ── Tests del detector ───────────────────────────────────────────────────────

def test_detector_dispara_con_dos_motores_misma_dim():
    """Caso testigo Peugeot 207 1.6 16v Nafta: TU5JP4 vs THP EP6DT."""
    products = [_make_jr_511(), _make_jr_571()]
    result = _detect_motor_variants(products)
    assert result is not None
    assert result["needs_clarification"] == "motor"
    assert result["cilindrada"] == "1.6"
    assert result["valvulas"] == "16v"
    codes = result["available_motor_codes"]
    assert "TU5JP4" in codes
    assert "THP EP6DT" in codes


def test_detector_no_dispara_un_solo_motor():
    """Si todos los productos comparten el mismo código, no hay ambigüedad."""
    products = [_make_jr_511(), _make_jr_511r()]
    result = _detect_motor_variants(products)
    assert result is None


def test_detector_un_solo_producto_no_dispara():
    """No tiene sentido pedir clarificación cuando hay un solo producto candidato."""
    assert _detect_motor_variants([_make_jr_511()]) is None


def test_detector_hints_restringen_dimension():
    """Si el cliente ya dijo 1.6 16v, no mezclar con motores de 1.4 8v."""
    products = [_make_jr_511(), _make_jr_571(), _make_multi_motor()]
    result = _detect_motor_variants(products, cilindrada_hint="1.6", valvulas_hint="16v")
    assert result is not None
    codes = result["available_motor_codes"]
    # Solo TU5JP4 y THP EP6DT (los 1.6 16v). TU5JP del multi-motor es 1.6 8v.
    assert "TU5JP4" in codes
    assert "THP EP6DT" in codes
    assert "TU3JP" not in codes  # ese es 1.4 8v


def test_detector_set_vacio():
    assert _detect_motor_variants([]) is None


def test_compat_list_misma_dim_no_dispara():
    """Producto compat-list (aporta 2+ códigos a la misma cilindrada+valvulas)
    es universal y NO contribuye a la detección. Sin este filtro, el reten
    con motor='1.9 8v F8Q 1.9 8v F9Q' dispararía clarificación espuria.
    """
    r = _detect_motor_variants([_make_reten_compat_misma_dim(), _make_reten_compat_misma_dim()])
    assert r is None, f"falso positivo: {r}"


def test_tapa_pura_mas_reten_compat_no_dispara():
    """Caso testigo SRCH-M-0011-P2: ZL00129 (F8Q puro) + reten compat F8Q/F9Q
    NO debe disparar. El reten es universal — no contribuye. La tapa aporta
    solo F8Q. No hay 2+ códigos distintos para la combinación.
    """
    r = _detect_motor_variants([_make_tapa_f8q_pura(), _make_reten_compat_misma_dim()])
    assert r is None, f"falso positivo: {r}"


def test_compat_list_otra_dim_si_contribuye():
    """Reten universal con códigos en DISTINTAS (cilindrada, válvulas)
    contribuye con 1 código a cada una — no es compat-list para una sola dim.
    """
    # RT-9192POL (multi-cilindradas) + JR-571 (THP EP6DT) → ambos aportan a (1.6, 16v)
    r = _detect_motor_variants([_make_multi_motor(), _make_jr_571()])
    assert r is not None
    codes = r["available_motor_codes"]
    assert "TU5JP4" in codes
    assert "THP EP6DT" in codes


# ── Tests del matcher ────────────────────────────────────────────────────────

def test_matcher_codigo_exacto():
    """Cliente eligió TU5JP4 → matchea solo JR-511-15."""
    assert _product_matches_motor_code(_make_jr_511(), "TU5JP4") is True
    assert _product_matches_motor_code(_make_jr_571(), "TU5JP4") is False


def test_matcher_codigo_compuesto():
    """Códigos como 'THP EP6DT' deben matchear el string completo."""
    assert _product_matches_motor_code(_make_jr_571(), "THP EP6DT") is True
    assert _product_matches_motor_code(_make_jr_511(), "THP EP6DT") is False


def test_matcher_case_insensitive():
    assert _product_matches_motor_code(_make_jr_511(), "tu5jp4") is True
    assert _product_matches_motor_code(_make_jr_511(), "Tu5Jp4") is True


def test_matcher_multimotor():
    """Producto con múltiples motores debe matchear cualquier código presente."""
    p = _make_multi_motor()
    assert _product_matches_motor_code(p, "TU5JP4") is True
    assert _product_matches_motor_code(p, "TU3JP") is True
    assert _product_matches_motor_code(p, "K4M") is False  # no presente


def test_matcher_target_vacio():
    assert _product_matches_motor_code(_make_jr_511(), "") is False
    assert _product_matches_motor_code(_make_jr_511(), "   ") is False


def test_matcher_producto_sin_pa_motor_no_descarta():
    """Igual criterio que valvulas/retenes: si el producto no tiene info, no
    descartar para evitar falsos negativos en catálogos incompletos.
    """
    prod_sin_motor = {
        "id": 1, "sku": "X", "name": "X",
        "attributes": {"Marca Vehiculo": ["Peugeot"]},
    }
    assert _product_matches_motor_code(prod_sin_motor, "TU5JP4") is True


# ── Tests sobre el dump real (smoke) ─────────────────────────────────────────

def test_detector_sobre_dump_real_peugeot_1_6_16v():
    """Replica el caso SRCH-A-0026 usando datos crudos del catálogo."""
    import json
    cat_path = Path(__file__).parent / "catalog_dump.json"
    if not cat_path.exists():
        return  # smoke opcional
    data = json.loads(cat_path.read_text())
    # Productos Peugeot 1.6 16v nafta
    products = []
    for p in data:
        attrs = p.get("attributes", {})
        marca = " ".join(attrs.get("Marca Vehiculo", []))
        comb = " ".join(attrs.get("Combustible", []))
        motors = attrs.get("motor", [])
        if (
            "peugeot" in marca.lower()
            and "nafta" in comb.lower()
            and any("1.6" in m and "16v" in m.lower() for m in motors)
        ):
            products.append(p)
    result = _detect_motor_variants(
        products, cilindrada_hint="1.6", valvulas_hint="16v"
    )
    assert result is not None
    codes = result["available_motor_codes"]
    # En el catálogo real hay TU5JP4 (aspirado) y THP EP6DT (turbo).
    assert any("TU5JP4" in c for c in codes)
    assert any("THP" in c for c in codes)


# ── Runner standalone ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError:
            fails += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
        except Exception:
            fails += 1
            print(f"  ERROR {t.__name__}")
            traceback.print_exc()
    print()
    print(f"{len(tests) - fails}/{len(tests)} passing")
    sys.exit(0 if fails == 0 else 1)
