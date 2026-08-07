"""Regresión del fix bidireccional de matching de medida.

Antes del fix, `_product_matches_measure` leía UNA medida del producto
(sufijo del SKU o pa_medida, lo que apareciera primero) y descartaba la otra.
Eso rompía la doble nomenclatura de juntas de tapa de cilindros: el cliente
que escribía "1.20mm" no matcheaba el SKU "TC-694-20 1M" aunque ese mismo
producto trae "Esp. 1.20mm" en pa_medida.

El fix lee AMBAS lecturas y matchea si target coincide con cualquiera.
También enriquece la clarificación con la doble etiqueta:
"1M (Esp. 1.20mm)".

El mapeo NM↔Esp es por familia de SKU (43 bases en el catálogo) y no es
algorítmico: 7 bases lo tienen invertido. Caso testigo: TC-694-20 donde
1M=1.20mm pero 2M=1.10mm.

Estos tests usan productos mock construidos para reflejar lo que devuelve
la API de WooCommerce. No dependen del catálogo real ni de la red.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Permitir `python tests/test_measure_matching.py` además de `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.woocommerce import (  # noqa: E402
    _detect_measure_variants,
    _product_matches_measure,
)


# ── Fixtures: productos TC-694-20 con orden invertido 1M=1.20mm / 2M=1.10mm ──

def _make_tc694(nm: str, esp_mm: str, marca: str = "Iveco") -> dict:
    """Construye un producto-mock con sufijo NM y pa_medida=Esp X.XXmm.

    Refleja la estructura del payload de WooCommerce REST API tal como
    la consume woocommerce.py (atributos como dict {nombre: [valores]}).
    """
    return {
        "id": 90000 + int(nm.rstrip("M")),
        "sku": f"TC-694-20 {nm}",
        "name": f"Junta Tapa De Cilindros {marca} 2.3 16v JTD F1AE - {nm}",
        "stock_status": "instock",
        "attributes": {
            "Marca": ["Illinois"],
            "Marca Vehiculo": [marca],
            "Modelo": ["Daily"] if marca == "Iveco" else ["Ducato"],
            "motor": ["2.3 16v JTD F1AE"],
            "Medida": [f"Esp. {esp_mm}mm"],
            "Combustible": ["Diesel"],
        },
    }


TC694_1M = _make_tc694("1M", "1.20")   # 1M = 1.20mm
TC694_2M = _make_tc694("2M", "1.10")   # 2M = 1.10mm  (más fino que 1M, orden invertido)
TC694_3M = _make_tc694("3M", "1.30")   # 3M = 1.30mm


# ── Tests del matching bidireccional ──

def test_cliente_dice_NM_matchea_su_propio_sufijo():
    """Caso 1: cliente dice '1M' → match contra sufijo del SKU."""
    assert _product_matches_measure(TC694_1M, "1M") is True
    assert _product_matches_measure(TC694_2M, "2M") is True
    assert _product_matches_measure(TC694_3M, "3M") is True


def test_cliente_dice_NM_no_matchea_otro_NM():
    """Cliente que pide 1M no se queda con el 2M."""
    assert _product_matches_measure(TC694_1M, "2M") is False
    assert _product_matches_measure(TC694_2M, "1M") is False


def test_cliente_dice_Esp_mm_matchea_atributo():
    """Caso 2: cliente dice 'Esp. 1.20mm' → match contra pa_medida del 1M.

    Antes del fix: target.family=esp_mm vs product.family=junta_m → reject.
    Después: candidates incluye AMBAS lecturas, esp_mm vs esp_mm matchea.
    """
    assert _product_matches_measure(TC694_1M, "Esp. 1.20mm") is True
    assert _product_matches_measure(TC694_2M, "Esp. 1.10mm") is True
    assert _product_matches_measure(TC694_3M, "Esp. 1.30mm") is True


def test_cliente_dice_mm_sin_prefijo_matchea():
    """Caso 3: cliente dice '1.20mm' (sin 'Esp.') → mismo comportamiento."""
    assert _product_matches_measure(TC694_1M, "1.20mm") is True
    assert _product_matches_measure(TC694_2M, "1.10mm") is True


def test_cliente_dice_mm_no_matchea_otro_espesor():
    """1.20mm pedido NO matchea con el 2M (que es 1.10mm)."""
    assert _product_matches_measure(TC694_1M, "1.10mm") is False
    assert _product_matches_measure(TC694_2M, "1.20mm") is False


def test_orden_invertido_no_se_confunde():
    """Caso 4 (testigo TC-694-20): 2M es físicamente más fino que 1M.

    Si el cliente dice 'Esp. 1.10mm', debe llegar al 2M, NO al 1M.
    Si dice '1M', debe llegar al 1M (1.20mm), NO al 2M.
    """
    # Cliente pide el espesor real más fino → debe matchear SOLO el 2M
    assert _product_matches_measure(TC694_1M, "Esp. 1.10mm") is False
    assert _product_matches_measure(TC694_2M, "Esp. 1.10mm") is True
    assert _product_matches_measure(TC694_3M, "Esp. 1.10mm") is False
    # Cliente pide "1M" → debe matchear SOLO el 1M (sufijo)
    assert _product_matches_measure(TC694_1M, "1M") is True
    assert _product_matches_measure(TC694_2M, "1M") is False


# ── Tests de la doble etiqueta en clarificación ──

def test_clarificacion_juntas_M_anexa_espesor():
    """_detect_measure_variants debe ofrecer ambas nomenclaturas para juntas_m."""
    result = _detect_measure_variants([TC694_1M, TC694_2M, TC694_3M])
    assert result is not None
    assert result["needs_clarification"] == "medida"
    opciones = result["available_measures"]
    # Cada opción debe contener tanto el NM como el Esp. X.XXmm equivalente.
    joined = " | ".join(opciones)
    assert "1M" in joined and "1.20mm" in joined
    assert "2M" in joined and "1.10mm" in joined
    assert "3M" in joined and "1.30mm" in joined
    # Formato esperado: "NM (Esp. X.XXmm)" — al menos una opción debe tenerlo
    assert any("(Esp." in op for op in opciones)


def test_clarificacion_preserva_orden_por_numero_M():
    """Las opciones se ordenan por value numérico (1M, 2M, 3M), no por espesor.

    Esto es deseable: el cliente piensa "voy del 1M para arriba", no
    "voy del más fino al más grueso". El espesor real va entre paréntesis.
    """
    result = _detect_measure_variants([TC694_3M, TC694_1M, TC694_2M])
    opciones = result["available_measures"]
    assert len(opciones) == 3
    assert opciones[0].startswith("1M")
    assert opciones[1].startswith("2M")
    assert opciones[2].startswith("3M")


# ── Test de no-regresión: cojinetes STD/0.25 NO deben recibir doble etiqueta ──

def _make_cojinete(sku_sfx: str, marca: str = "Renault") -> dict:
    return {
        "id": 80000,
        "sku": f"A168/2-{sku_sfx}",
        "name": f"Cojinete Axial {marca} 1.6 16v K4M",
        "stock_status": "instock",
        "attributes": {
            "Marca Vehiculo": [marca],
            "motor": ["1.6 16v K4M"],
            "Medida": [sku_sfx if sku_sfx == "STD" else f"{sku_sfx}mm"],
        },
    }


def test_clarificacion_cojinetes_sin_doble_etiqueta():
    """Para cojinetes STD/0.25/0.50, la nomenclatura ya es la misma en ambos
    lados; no hay que anexar nada.
    """
    prods = [_make_cojinete("STD"), _make_cojinete("0.25"), _make_cojinete("0.50")]
    result = _detect_measure_variants(prods)
    assert result is not None
    # No debe contener "(Esp." porque no hay mapeo paralelo NM↔mm aquí.
    for op in result["available_measures"]:
        assert "(Esp." not in op


# ── Runner standalone ──

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
