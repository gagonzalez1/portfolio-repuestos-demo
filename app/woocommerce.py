import logging
import re
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Normaliza aliases de marca al valor canónico del catálogo (lowercase).
# El filtro Python compara substring case-insensitive, así que el valor canónico
# debe ser exactamente como aparece en pa_marca-vehiculo (ej: "volkswagen" matchea "Volkswagen").
_BRAND_ALIASES: dict[str, str] = {
    # Marcas top del catálogo (forma canónica = la que aparece en pa_marca-vehiculo).
    "vw":            "volkswagen",
    "volkswagen":    "volkswagen",
    "chevy":         "chevrolet",
    "chevrolet":     "chevrolet",
    "peugeot":       "peugeot",
    "citroen":       "citroen",
    "citroën":       "citroen",
    "renault":       "renault",
    "ford":          "ford",
    "fiat":          "fiat",
    "toyota":        "toyota",
    "honda":         "honda",
    "nissan":        "nissan",
    "hyundai":       "hyundai",
    "kia":           "kia",
    # Mercedes: el catálogo tiene typo "Mercedez Benz" → mapeamos al typo
    # para que el filtro substring matchee. Si Nico corrige el typo, ajustar.
    "mercedes":      "mercedez",
    "mercedes benz": "mercedez",
    "mercedez":      "mercedez",
    # Marcas medianas del catálogo (8-50 SKUs en stock cada una). Antes no
    # estaban — el cliente no se podía buscar por estas marcas porque
    # _BRAND_ALIASES no las cubría y el normalize devolvía la forma cruda.
    # El catálogo dice "Daewo" (sin doble o); mapeamos "daewoo" al typo.
    "daewo":         "daewo",
    "daewoo":        "daewo",
    "seat":          "seat",
    "suzuki":        "suzuki",
    "audi":          "audi",
    "iveco":         "iveco",
    "maxion":        "maxion",
    "mitsubishi":    "mitsubishi",
    "alfa romeo":    "alfa romeo",
    "alfa":          "alfa romeo",
    "isuzu":         "isuzu",
    "power stroke":  "power stroke",
    "land rover":    "land rover",
    "perkins":       "perkins",
    "mazda":         "mazda",
    "volvo":         "volvo",
    "rover":         "rover",
    "dodge":         "dodge",
    "chrysler":      "chrysler",
    "jeep":          "jeep",
    "bmw":           "bmw",
    # Indenor era el fabricante francés del motor diesel del Peugeot 504/505.
    # En AR algunos clientes se refieren al auto como "Indenor" en lugar de
    # "Peugeot". El catálogo tiene 20 SKUs cargados como "Indenor" y otros
    # tantos como "Peugeot" con motor XD2/XD3 — los productos físicos son
    # los mismos. Mapeamos a "peugeot" porque cubre más SKUs (Peugeot tiene
    # 277 productos vs 20 de Indenor) y porque los clientes que dicen
    # "Indenor" también aceptan productos del Peugeot 504/505. Trade-off:
    # los 20 SKUs cargados solo como Indenor pueden quedar sin matchear si
    # el filtro pa_marca-vehiculo='peugeot' es estricto.
    "indenor":       "peugeot",
}


def extract_measure(text: str) -> dict | None:
    """Normaliza una medida desde string. Retorna dict con raw/family/value o None."""
    if not text:
        return None
    text = text.strip()

    # STD
    if re.fullmatch(r"STD", text, re.IGNORECASE):
        return {"raw": "STD", "family": "std", "value": "0.00"}

    # Rectificado: BA0.30 / BI0.50 / BA030
    m = re.fullmatch(r"(BA|BI)\s*(\d+[.,]?\d*)", text, re.IGNORECASE)
    if m:
        side = m.group(1).upper()
        raw_val = m.group(2).replace(",", ".")
        # Normalizar "030" → "0.30"
        if "." not in raw_val and len(raw_val) >= 2:
            raw_val = raw_val[0] + "." + raw_val[1:]
        return {"raw": f"{side}{raw_val}", "family": "rectificado", "side": side, "value": raw_val}

    # Junta M: 1M / 2M / 3M / 4M / 5M
    m = re.fullmatch(r"(\d)M", text, re.IGNORECASE)
    if m:
        return {"raw": m.group(0).upper(), "family": "junta_m", "value": m.group(1)}

    # Esp. 1.68mm — el prefijo "Esp." es OPCIONAL. Aceptamos "Esp. 1.50mm",
    # "esp 1.50mm", "1.50mm" y variaciones. El cliente real rara vez escribe
    # el "Esp." literal — suele decir "1.50mm" o "espesor 1.5mm". Sin esta
    # flexibilidad, _product_matches_measure rechaza el producto correcto
    # del catálogo (que sí está tagged con "Esp. 1.50mm" en attr.Medida).
    # Caso testigo: AUTO-0009 (Peugeot 206 1.9 DW8 — junta TC-609-20 6M
    # con attr.Medida='Esp. 1.50mm'); cliente decía "esp 1.50mm" y
    # extract_measure devolvía None.
    m = re.fullmatch(r"(?:esp(?:esor)?\.?\s*)?(\d+[.,]\d+)\s*mm", text, re.IGNORECASE)
    if m:
        value = m.group(1).replace(",", ".")
        return {"raw": text, "family": "esp_mm", "value": value}

    # Decimal puro: 0.25 / 0.50 / 1.75
    m = re.fullmatch(r"(\d+[.,]\d+)", text)
    if m:
        value = text.replace(",", ".")
        return {"raw": text, "family": "decimal", "value": value}

    return None


def extract_sku_base(sku: str) -> str:
    """Limpia sufijos de medida del SKU.

    Ejemplos:
        '10-237/4-(0.25)'  → '10-237/4'
        'TC-609-20 5M'     → 'TC-609-20'   (separador con espacio)
        'ABC-123-BA030'    → 'ABC-123'

    El separador entre la base y el sufijo puede ser '-' o whitespace: distintos
    proveedores usan distintas convenciones. Sin esto, los SKU con sufijo pegado
    por espacio (ej. 'TC-609-20 5M') no matchean cuando el usuario filtra por
    medida (caso real: junta de tapa de cilindros Peugeot 1.9 DW8).
    """
    return re.sub(
        r"[-\s]\(?(?:STD|\d+[.,]\d+|\d+M|(?:BA|BI)\d+(?:[.,]\d+)?)\)?$",
        "",
        sku,
        flags=re.IGNORECASE,
    )


def _get_product_measure(product: dict) -> dict | None:
    """Lee la medida de un producto con estrategia híbrida.

    Orden:
    a) atributo "medida" (case-insensitive)
    b) parsear nombre del producto
    c) parsear SKU
    """
    # a/b) atributo cuyo nombre sea "medida" (case-insensitive)
    for key, values in product.get("attributes", {}).items():
        if key.lower() == "medida" and values:
            m = extract_measure(values[0])
            if m:
                return m

    # c) nombre del producto
    m = extract_measure(product.get("name", ""))
    if m:
        return m

    # d) SKU
    m = extract_measure(product.get("sku", ""))
    if m:
        return m

    return None


def _get_sku_suffix_measure(product: dict) -> dict | None:
    """Extrae medida del sufijo del SKU (preferido para junta_m)."""
    sku = product.get("sku", "")
    base = extract_sku_base(sku)
    if len(sku) <= len(base):
        return None
    suffix = sku[len(base):].lstrip("-(").rstrip(")")
    return extract_measure(suffix)


# ─────────────────────────────────────────────────────────────────────────────
# Filtro defensivo de tipo de repuesto contra ambigüedades de substring
# ─────────────────────────────────────────────────────────────────────────────


# Tokens que crean asimetría de substring entre nombres de productos físicamente
# distintos. Si el query del cliente NO contiene el token, productos cuyo name
# empieza con ese token deben descartarse antes de detectar variantes.
# Caso de origen (abr-2026): cliente pide "tapa cilindros" → fulltext de WC
# devuelve "Junta Tapa De Cilindros..." porque "tapa cilindros" es substring
# del nombre. Sin este filtro, _detect_measure_variants dispara medida (1M..5M)
# sobre productos que son juntas, no tapas — y el LLM ejecuta el flujo de
# clarificación sin verificar que el name del producto coincida con lo pedido.
_EXCLUSIVE_REPUESTO_PREFIXES = ("junta", "juego")


def _classify_repuesto_head(text: str) -> str | None:
    """Clasifica el cabezal léxico del texto en uno de los exclusive prefixes.

    Devuelve el primer exclusive prefix encontrado por POSICIÓN en el texto,
    matcheando como palabra completa con plural opcional (junta/juntas, juego/juegos).
    Esto modela la convención del español donde el head noun viene primero
    en la frase: "juego de juntas" → head 'juego', "junta tapa" → head 'junta'.

    Retorna None si el texto no contiene ningún prefix exclusivo (ej: "tapa cilindros",
    "bulones tapa", "cigüeñal").
    """
    if not text:
        return None
    earliest_prefix: str | None = None
    earliest_pos: int = -1
    for prefix in _EXCLUSIVE_REPUESTO_PREFIXES:
        m = re.search(rf"\b{re.escape(prefix)}s?\b", text, re.IGNORECASE)
        if m and (earliest_pos < 0 or m.start() < earliest_pos):
            earliest_prefix = prefix
            earliest_pos = m.start()
    return earliest_prefix


def _strict_repuesto_match(product: dict, repuesto: str) -> bool:
    """¿El nombre del producto matchea el tipo de repuesto que pidió el cliente?

    Filtro defensivo contra el patrón donde un nombre de producto contiene a otro
    como substring (ej: "Junta Tapa De Cilindros" contiene a "Tapa De Cilindros").

    Estrategia: clasificar el query y el name por su HEAD NOUN (primer exclusive
    prefix por posición). Si el name tiene un head exclusivo (junta/juego), el query
    debe pedir el mismo. Si el name no tiene head exclusivo, el query tampoco debe
    pedir uno. Esto resuelve correctamente el caso de queries como "juego de juntas"
    donde 'junta' aparece como substring de 'juntas' pero el head verdadero es
    'juego' (caso testigo: SKU JR-778-MGR del 2-may-2026).

    Reglas:
      - name="Junta Tapa..." (head='junta'): query DEBE tener head='junta'.
      - name="Juego De Juntas..." (head='juego'): query DEBE tener head='juego'.
      - name="Tapa De Cilindros..." (head=None): query NO debe tener head exclusivo.

    No filtra cuando `repuesto` viene vacío (preserva backward compat).
    """
    if not repuesto:
        return True
    name = product.get("name") or ""
    name_head = _classify_repuesto_head(name)
    query_head = _classify_repuesto_head(repuesto)
    if name_head is not None:
        # Name de tipo exclusivo: solo pasa si el query pide ese mismo tipo.
        return query_head == name_head
    # Name no exclusivo: solo pasa si el query tampoco pide tipo exclusivo
    # (evita devolver una "tapa" cuando el cliente pidió un "juego" o "junta").
    return query_head is None


def _detect_measure_variants(products: list) -> dict | None:
    """Detecta si los productos son variantes del mismo repuesto diferenciadas por medida.

    Agrupa por SKU base; si el grupo dominante tiene 2+ medidas distintas, retorna
    un dict con needs_clarification='medida' y las opciones ordenadas.
    Retorna None si no hay variantes claras.
    """
    if not products:
        return None

    # Agrupar por SKU base
    groups: dict[str, list] = {}
    for p in products:
        base = extract_sku_base(p.get("sku", ""))
        groups.setdefault(base, []).append(p)

    # Grupo dominante (más productos)
    dominant_base, dominant_products = max(groups.items(), key=lambda x: len(x[1]))

    if len(dominant_products) < 2:
        return None

    # Obtener medida de cada producto del grupo: SKU suffix primero (captura junta_m),
    # luego fallback a atributo/nombre. Para juntas de tapa de cilindros, también
    # capturamos la lectura paralela del atributo pa_medida (Esp. X.XXmm) para
    # poder ofrecerle al cliente la doble nomenclatura en la clarificación.
    measures_seen: dict[str, dict] = {}  # raw → measure dict (principal)
    paired_esp: dict[str, str] = {}      # raw_principal → "Esp. X.XXmm" si existe
    for p in dominant_products:
        sku_m = _get_sku_suffix_measure(p)
        attr_m = _get_product_measure(p)
        primary = sku_m or attr_m
        if primary and primary["raw"] not in measures_seen:
            measures_seen[primary["raw"]] = primary
            # Si la medida principal viene del sufijo del SKU y es junta_m,
            # guardamos la lectura paralela del atributo (espesor real) para
            # que el cliente que piensa en mm también la reconozca.
            if (
                sku_m is not None
                and sku_m.get("family") == "junta_m"
                and attr_m is not None
                and attr_m.get("family") == "esp_mm"
            ):
                paired_esp[primary["raw"]] = attr_m["raw"]

    if len(measures_seen) < 2:
        return None

    # Ordenar: STD primero, luego ascendente por value numérico
    def _sort_key(m: dict) -> tuple:
        if m["family"] == "std":
            return (0, 0.0)
        try:
            return (1, float(m["value"]))
        except (ValueError, KeyError):
            return (2, 0.0)

    sorted_measures = sorted(measures_seen.values(), key=_sort_key)

    # Etiqueta visible al cliente: si tenemos el espesor paralelo, anexarlo
    # en formato "1M (Esp. 1.20mm)". Cubre la asimetría documentada en la
    # auditoría del catálogo (mapeo NM↔Esp no es algorítmico).
    available_measures: list[str] = []
    for m in sorted_measures:
        raw = m["raw"]
        esp = paired_esp.get(raw)
        if esp:
            available_measures.append(f"{raw} ({esp})")
        else:
            available_measures.append(raw)

    return {
        "needs_clarification": "medida",
        "available_measures": available_measures,
        "product_base": dominant_products[0].get("name", ""),
        "sku_base": dominant_base,
    }


def _product_matches_measure(product: dict, target_raw: str) -> bool:
    """Verifica si un producto tiene la medida buscada (target_raw es lo que escribió el usuario).

    Lee AMBAS lecturas de medida del producto (sufijo del SKU y atributo pa_medida)
    y matchea si el target del cliente coincide con cualquiera de las dos. Esto
    cubre la doble nomenclatura de las juntas de tapa de cilindros: el SKU lleva
    "1M/2M/3M" y pa_medida lleva "Esp. X.XXmm" en paralelo para el MISMO producto.

    Sin este OR, un cliente que escribe "1.20mm" (family=esp_mm) rebota contra el
    sufijo "1M" (family=junta_m) y el producto correcto queda filtrado, aunque
    pa_medida sí trae "Esp. 1.20mm". Y viceversa para el cliente que escribe "1M".

    El mapeo NM ↔ Esp X.XXmm es por familia de SKU (43 bases en el catálogo) y
    no es algorítmico — 7 bases tienen orden invertido (ej: TC-694-20 1M=1.20mm
    pero 2M=1.10mm). Por eso no hardcodeamos tabla: cada producto trae su propio
    mapeo en su payload y lo consultamos en runtime.
    """
    target = extract_measure(target_raw)
    if target is None:
        return False

    # Recolectar todas las lecturas disponibles del producto.
    candidates = []
    sku_measure = _get_sku_suffix_measure(product)
    if sku_measure is not None:
        candidates.append(sku_measure)
    attr_measure = _get_product_measure(product)
    if attr_measure is not None and (sku_measure is None or attr_measure["raw"] != sku_measure["raw"]):
        candidates.append(attr_measure)

    if not candidates:
        return False

    for cand in candidates:
        if target["family"] != cand["family"]:
            continue
        if target["family"] == "rectificado":
            if target.get("side") == cand.get("side") and target["value"] == cand["value"]:
                return True
            continue
        if target["value"] == cand["value"]:
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Clarificación por VÁLVULAS (8v / 16v)
# Mismo patrón que _detect_measure_variants: agrupa por SKU base y, si el grupo
# dominante muestra ambas variantes, retorna dict needs_clarification='valvulas'.
# ─────────────────────────────────────────────────────────────────────────────


def extract_valvulas(text: str) -> str | None:
    """Normaliza una mención de válvulas a '8v' o '16v'. Retorna None si no encuentra.

    Reconoce: '8V', '16 V', '8 valvulas', '16 válvulas', 'motor 8V', etc.
    """
    if not text:
        return None
    text_lower = text.lower()
    # 16 antes que 8 — substring '8' está dentro de '16'
    if re.search(r"\b16\s*v(?:[áa]lvulas?)?\b", text_lower):
        return "16v"
    if re.search(r"\b8\s*v(?:[áa]lvulas?)?\b", text_lower):
        return "8v"
    return None


def _get_product_valvulas(product: dict) -> str | None:
    """Determina si un producto es 8v o 16v leyendo pa_motor primero, luego nombre."""
    motor_values = product.get("attributes", {}).get("motor", [])
    for v in motor_values:
        result = extract_valvulas(v)
        if result:
            return result
    return extract_valvulas(product.get("name", ""))


def _detect_valvulas_variants(products: list) -> dict | None:
    """Detecta si los productos son variantes 8v/16v sobre el mismo SKU base.

    Retorna dict con needs_clarification='valvulas' y available_valvulas=['8v','16v']
    cuando el grupo dominante de SKU base contiene ambas variantes.
    """
    if not products:
        return None

    groups: dict[str, list] = {}
    for p in products:
        base = extract_sku_base(p.get("sku", ""))
        groups.setdefault(base, []).append(p)

    dominant_base, dominant_products = max(groups.items(), key=lambda x: len(x[1]))
    if len(dominant_products) < 2:
        return None

    valvulas_seen: set[str] = set()
    for p in dominant_products:
        v = _get_product_valvulas(p)
        if v:
            valvulas_seen.add(v)

    if len(valvulas_seen) < 2:
        return None

    # Orden estable: 8v antes que 16v
    sorted_valvulas = sorted(valvulas_seen, key=lambda x: (0 if x == "8v" else 1))

    return {
        "needs_clarification": "valvulas",
        "available_valvulas": sorted_valvulas,
        "product_base": dominant_products[0].get("name", ""),
        "sku_base": dominant_base,
    }


def _product_matches_valvulas(product: dict, target_raw: str) -> bool:
    """¿El producto matchea las válvulas pedidas? target_raw es lo que escribió el usuario."""
    target = extract_valvulas(target_raw)
    if not target:
        # Toleramos cuando llega ya normalizado ('8v' literal del agente)
        if target_raw and target_raw.strip().lower() in ("8v", "16v"):
            target = target_raw.strip().lower()
        else:
            return False
    product_norm = _get_product_valvulas(product)
    if product_norm is None:
        # Sin info de válvulas en el producto → no descartar (evita falsos negativos)
        return True
    return product_norm == target


# ─────────────────────────────────────────────────────────────────────────────
# Clarificación por RETENES (con / sin)
# ─────────────────────────────────────────────────────────────────────────────


def extract_retenes(text: str) -> str | None:
    """Detecta 'con' o 'sin' retenes en un string. Retorna 'con' / 'sin' / None.

    Reconoce: 'con retenes', 'sin retenes', 'c/retenes', 's/retenes',
    'con retén', 'sin retén'.
    """
    if not text:
        return None
    text_lower = text.lower()
    # "ret[eé]n" cubre tanto 'reten' (de retenes) como 'retén' (singular acentuado).
    if re.search(r"\bsin\s+ret[eé]n[eé]?s?\b", text_lower) or re.search(r"\bs\s*/\s*ret[eé]n[eé]?s?\b", text_lower):
        return "sin"
    if re.search(r"\bcon\s+ret[eé]n[eé]?s?\b", text_lower) or re.search(r"\bc\s*/\s*ret[eé]n[eé]?s?\b", text_lower):
        return "con"
    return None


def _get_product_retenes(product: dict) -> str | None:
    """Detecta si un producto trae 'con retenes' o 'sin retenes' por atributo variante o nombre."""
    variante_values = product.get("attributes", {}).get("variante", [])
    for v in variante_values:
        r = extract_retenes(v)
        if r:
            return r
    return extract_retenes(product.get("name", ""))


def _detect_retenes_variants(products: list) -> dict | None:
    """Detecta si los productos son variantes con/sin retenes sobre el mismo SKU base."""
    if not products:
        return None

    groups: dict[str, list] = {}
    for p in products:
        base = extract_sku_base(p.get("sku", ""))
        groups.setdefault(base, []).append(p)

    dominant_base, dominant_products = max(groups.items(), key=lambda x: len(x[1]))
    if len(dominant_products) < 2:
        return None

    retenes_seen: set[str] = set()
    for p in dominant_products:
        r = _get_product_retenes(p)
        if r:
            retenes_seen.add(r)

    if len(retenes_seen) < 2:
        return None

    # Orden: con antes que sin
    sorted_retenes = sorted(retenes_seen, key=lambda x: (0 if x == "con" else 1))

    return {
        "needs_clarification": "retenes",
        "available_retenes": sorted_retenes,
        "product_base": dominant_products[0].get("name", ""),
        "sku_base": dominant_base,
    }


def _product_matches_retenes(product: dict, target_raw: str) -> bool:
    """¿El producto matchea la elección de retenes (con/sin)?"""
    target = extract_retenes(target_raw)
    if not target:
        if target_raw and target_raw.strip().lower() in ("con", "sin"):
            target = target_raw.strip().lower()
        else:
            return False
    product_norm = _get_product_retenes(product)
    if product_norm is None:
        # Producto sin info de retenes → no descartar
        return True
    return product_norm == target


# ─────────────────────────────────────────────────────────────────────────────
# Clarificación por CALENTADOR (vertical / horizontal)
# Mismo patrón que retenes/valvulas/medida. Aplica principalmente a tapas de
# cilindro Diesel donde la posición del calentador (la bujía incandescente)
# define la variante física del producto.
# ─────────────────────────────────────────────────────────────────────────────


def extract_calentador(text: str) -> str | None:
    """Detecta 'vertical' u 'horizontal' en un string. Retorna 'vertical' / 'horizontal' / None.

    Reconoce 'vertical', 'horizontal', 'calentador vertical', 'calent. horizontal',
    case-insensitive y con word-boundary para evitar falsos matches en otras palabras.
    """
    if not text:
        return None
    text_lower = text.lower()
    if re.search(r"\bvertical(?:es)?\b", text_lower):
        return "vertical"
    if re.search(r"\bhorizontal(?:es)?\b", text_lower):
        return "horizontal"
    return None


def _get_product_calentador(product: dict) -> str | None:
    """Determina si un producto es calentador vertical/horizontal.

    Estrategia: atributo `variante` primero, luego nombre del producto.
    """
    variante_values = product.get("attributes", {}).get("variante", [])
    for v in variante_values:
        c = extract_calentador(v)
        if c:
            return c
    return extract_calentador(product.get("name", ""))


def _detect_calentador_variants(products: list) -> dict | None:
    """Detecta si en el set de productos hay tanto vertical como horizontal.

    A diferencia de medidas (que comparten SKU base con sufijo), las tapas Diesel
    con distinto calentador tienen SKUs completamente distintos (ej: ZL00130 vertical
    vs ZL00131 horizontal). Por eso no agrupamos por SKU base; basta con que el set
    de resultados muestre ambos valores para disparar la clarificación.
    """
    if not products:
        return None

    calentador_seen: set[str] = set()
    first_with_calentador: dict | None = None
    for p in products:
        c = _get_product_calentador(p)
        if c:
            calentador_seen.add(c)
            if first_with_calentador is None:
                first_with_calentador = p

    if len(calentador_seen) < 2:
        return None

    # Orden estable: vertical antes que horizontal
    sorted_calentador = sorted(calentador_seen, key=lambda x: (0 if x == "vertical" else 1))

    return {
        "needs_clarification": "calentador",
        "available_calentador": sorted_calentador,
        "product_base": (first_with_calentador or {}).get("name", ""),
    }


def _product_matches_calentador(product: dict, target_raw: str) -> bool:
    """¿El producto matchea la elección de calentador (vertical/horizontal)?"""
    target = extract_calentador(target_raw)
    if not target:
        if target_raw and target_raw.strip().lower() in ("vertical", "horizontal"):
            target = target_raw.strip().lower()
        else:
            return False
    product_norm = _get_product_calentador(product)
    if product_norm is None:
        # Producto sin info de calentador → no descartar (evita falsos negativos)
        return True
    return product_norm == target


# ─────────────────────────────────────────────────────────────────────────────
# Clarificación por CÓDIGO DE MOTOR
# Caso testigo: Peugeot 207 1.6 16v Nafta tiene dos motores físicamente distintos
# con misma cilindrada+válvulas+combustible: TU5JP4 (aspirado, JR-511-15) y
# THP EP6DT (turbo, JR-571-15). El cliente que dice "Peugeot 207 1.6 16v nafta"
# no especifica cuál, y los productos son SKUs físicamente distintos (no son
# variantes de medida). Sin este detector, el bot devuelve uno solo según el
# ranking del fulltext, perdiendo silenciosamente la otra mitad del catálogo.
#
# A diferencia de medida/válvulas (que agrupan por SKU base), acá los productos
# tienen SKUs distintos — la dimensión que los separa es el código del motor
# leído desde `pa_motor`. Más parecido a calentador en estructura, pero la
# detección parsea (cilindrada, valvulas, código) en lugar de un keyword.
# ─────────────────────────────────────────────────────────────────────────────


def _parse_motor_string(s: str) -> list[tuple[str, str, str]]:
    """Parsea un string como '1.6 16v TU5JP4 1.6 16v THP EP6DT' a tuplas
    (cilindrada, valvulas, código).

    Muchos productos del catálogo listan varios motores compatibles en el mismo
    string (separador es solo whitespace). El parser usa la repetición del
    patrón "<cil> <val>" como delimitador.

    Si el string no tiene válvulas reconocibles (raro), retorna [].
    """
    if not s:
        return []
    matches = list(re.finditer(r"(\d+\.\d+)\s+(8v|16v)", s, re.IGNORECASE))
    if not matches:
        return []
    out: list[tuple[str, str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(s)
        code = s[start:end].strip(" /,-")
        # Normalizar whitespace interno (THP  EP6DT → THP EP6DT)
        code = re.sub(r"\s+", " ", code).strip()
        if code:
            out.append((m.group(1), m.group(2).lower(), code))
    return out


def _get_product_motor_codes(product: dict) -> list[tuple[str, str, str]]:
    """Devuelve TODAS las tuplas (cilindrada, válvulas, código) del producto.

    Un producto puede ser compatible con varios motores; cada string de pa_motor
    contribuye potencialmente varias tuplas.
    """
    out: list[tuple[str, str, str]] = []
    for v in product.get("attributes", {}).get("motor", []):
        out.extend(_parse_motor_string(v))
    return out


def _detect_motor_variants(
    products: list,
    *,
    cilindrada_hint: str | None = None,
    valvulas_hint: str | None = None,
) -> dict | None:
    """Detecta códigos de motor distintos para una misma (cilindrada, válvulas).

    Si el cliente ya filtró por una cilindrada/válvulas específica, los hints
    permiten restringir la detección a esa combinación (evita disparar
    clarificación por motores de otro chunk del catálogo). Si no se pasan, se
    elige la combinación con más códigos distintos.

    Solo dispara si hay >= 2 productos en el set y >= 2 códigos para la misma
    combinación. Esto evita falsos positivos cuando un único producto compatible
    con muchos motores trae todos en el mismo string de pa_motor.

    **Filtro compat-list**: si un producto aporta 2+ códigos distintos a la
    misma (cilindrada, válvulas), es un producto universal (reten, cojinete,
    biela compat con varios motores) — su pa_motor enlista variantes que él
    SÍ soporta todas a la vez. Ese producto NO necesita desambiguación y
    contamina la detección haciéndola disparar sobre falsos positivos. Solo
    contribuyen a la votación productos que aportan UN único código a la
    combinación seleccionada.

    Ejemplos:
      - reten RT-9192POL con motor='1.4 8v TU3JP 1.5 8v TU4 1.6 16v TU5JP4'
        aporta UN código (TU5JP4) a la combinación (1.6, 16v). Contribuye.
      - reten "1.9 8v F8Q 1.9 8v F9Q" aporta DOS códigos (F8Q, F9Q) a la
        misma combinación (1.9, 8v). NO contribuye (universal).
    """
    if not products or len(products) < 2:
        return None

    cil_hint = (cilindrada_hint or "").strip()
    val_hint = (valvulas_hint or "").strip().lower()

    # Por producto: agrupar sus códigos por (cilindrada, válvulas) tras aplicar hints.
    # Solo cuentan productos que aporten UN único código a una combinación dada.
    contribs_by_dim: dict[tuple[str, str], set[str]] = {}
    sample_by_code: dict[tuple[str, str, str], dict] = {}

    for p in products:
        per_product: dict[tuple[str, str], set[str]] = {}
        for cil, val, code in _get_product_motor_codes(p):
            if cil_hint and cil != cil_hint:
                continue
            if val_hint and val != val_hint:
                continue
            per_product.setdefault((cil, val), set()).add(code)
        for key, codes_in_dim in per_product.items():
            if len(codes_in_dim) != 1:
                # El producto enlista varios códigos para esa combinación → universal.
                continue
            (only_code,) = codes_in_dim
            contribs_by_dim.setdefault(key, set()).add(only_code)
            sample_by_code.setdefault((key[0], key[1], only_code), p)

    candidates = [(k, codes) for k, codes in contribs_by_dim.items() if len(codes) >= 2]
    if not candidates:
        return None

    # Elegir la combinación con más códigos; desempate alfabético por (cil, val)
    candidates.sort(key=lambda x: (-len(x[1]), x[0]))
    (cilindrada, valvulas), codes = candidates[0]

    sorted_codes = sorted(codes)
    sample = sample_by_code.get((cilindrada, valvulas, sorted_codes[0]))

    return {
        "needs_clarification": "motor",
        "available_motor_codes": sorted_codes,
        "cilindrada": cilindrada,
        "valvulas": valvulas,
        "product_base": (sample or {}).get("name", ""),
    }


def _product_matches_motor_code(product: dict, target_raw: str) -> bool:
    """¿El producto soporta el código de motor pedido por el cliente?

    Word-boundary match case-insensitive sobre los valores de pa_motor. Usar
    word boundaries (y no substring liso) evita confundir códigos cortos como
    "D" con substrings de códigos más largos como "TD" o "TDI". Por ejemplo:
    - target "D" matchea "1.9 8v D" pero NO "1.9 8v TD"
    - target "F8Q" matchea "1.9 8v F8Q"
    - target "THP EP6DT" matchea "1.6 16v THP EP6DT"
    - target "HDI" matchea "1.6 16v HDI DV6" (cliente pide la familia)

    El target típicamente viene de `available_motor_codes` (que sale del
    catálogo), así que el match es estable contra los strings originales.

    Si el producto no tiene pa_motor cargado, no descartamos (evita falsos
    negativos en catálogos incompletos, mismo criterio que valvulas/retenes).
    """
    if not target_raw:
        return False
    target = target_raw.strip()
    if not target:
        return False
    motor_values = product.get("attributes", {}).get("motor", [])
    if not motor_values:
        return True
    # Word-boundary regex case-insensitive; escapamos caracteres especiales del target
    # por si trae paréntesis u otros símbolos (defensivo, los códigos reales son
    # alfanuméricos + espacios pero no cuesta nada).
    pattern = re.compile(rf"\b{re.escape(target)}\b", re.IGNORECASE)
    return any(pattern.search(v) for v in motor_values)


def _finalize_with_clarification(
    result: list,
    *,
    repuesto: str | None = None,
    valvulas: str | None = None,
    retenes: str | None = None,
    medida: str | None = None,
    calentador: str | None = None,
    motor_code: str | None = None,
) -> list | dict:
    """Aplica filtros explícitos y luego chequea clarificaciones en orden de prioridad.

    Prioridad de detección: valvulas → retenes → calentador → motor → medida.
    La clarificación se devuelve solo si la dimensión correspondiente NO vino ya
    filtrada por el agente. Esto evita re-preguntar después de que el cliente ya
    respondió.

    El filtro de `repuesto` (estricto por tipo) corre PRIMERO porque elimina productos
    físicamente distintos que el fulltext de WC mete por ambigüedad de substring
    ("tapa cilindros" matchea "Junta Tapa De Cilindros..."). Sin esto, la detección
    de variantes puede dispararse sobre productos del tipo equivocado.

    Calentador (vertical/horizontal) va DESPUÉS de retenes y ANTES de motor porque:
    aplica casi exclusivamente a tapas Diesel que no suelen tener subvariantes de
    medida pero sí pueden tener retenes; ponerlo último haría que algunas tapas
    cayeran en clarificación de medida espuria.

    Motor (TU5JP4 vs THP EP6DT vs HDI DV6 con misma cilindrada/válvulas/combustible)
    va DESPUÉS de calentador y ANTES de medida: si el cliente todavía no eligió motor,
    preguntar por medida es prematuro (la medida cambia entre familias de SKU).
    """
    if repuesto:
        result = [p for p in result if _strict_repuesto_match(p, repuesto)]

    if valvulas:
        result = [p for p in result if _product_matches_valvulas(p, valvulas)]
    if retenes:
        result = [p for p in result if _product_matches_retenes(p, retenes)]
    if calentador:
        result = [p for p in result if _product_matches_calentador(p, calentador)]
    if motor_code:
        result = [p for p in result if _product_matches_motor_code(p, motor_code)]
    if medida:
        # Snapshot pre-filtro. Si el filtro estricto descarta TODO, ofrecer
        # las medidas disponibles en lugar de devolver vacio. Caso testigo:
        # RND-0030 Ford Transit 2.4 - cliente pidió medida 1.20mm pero el
        # SKU tenia formato distinto y el filtro estricto descartaba todo.
        # Con este fallback, si no hay match exacto, volvemos al pre-filtro
        # y disparamos needs_clarification=medida con las medidas reales.
        pre_medida = list(result)
        result = [p for p in result if _product_matches_measure(p, medida)]
        if not result and pre_medida:
            clar_fallback = _detect_measure_variants(pre_medida)
            if clar_fallback:
                return clar_fallback
            # Si no hay variantes claras, devolver el pre_medida (mejor algo
            # que nada — el cliente puede confirmar manualmente).
            result = pre_medida

    if not valvulas:
        clar = _detect_valvulas_variants(result)
        if clar:
            return clar
    if not retenes:
        clar = _detect_retenes_variants(result)
        if clar:
            return clar
    if not calentador:
        clar = _detect_calentador_variants(result)
        if clar:
            return clar
    if not motor_code:
        clar = _detect_motor_variants(result)
        if clar:
            return clar
    if not medida:
        clar = _detect_measure_variants(result)
        if clar:
            return clar
    return result


class WooCommerceClient:
    """Cliente para consultar productos via WooCommerce REST API."""

    def __init__(
        self,
        base_url: str,
        consumer_key: str,
        consumer_secret: str,
        http_client: httpx.AsyncClient,
    ):
        self._base_url = base_url
        self._auth = (consumer_key, consumer_secret)
        self._http_client = http_client

    async def search_products(
        self,
        search: str | None = None,
        category: int | None = None,
        sku: str | None = None,
        per_page: int = 5,
        page: int = 1,
    ) -> list[dict]:
        """Busca productos en WooCommerce.

        Args:
            search: Texto libre de búsqueda
            category: ID de categoría para filtrar
            sku: SKU exacto del producto
            per_page: Cantidad de resultados (max 10 para MVP)
            page: Página de resultados
        """
        params = {
            "per_page": min(per_page, 10),
            "page": page,
            "status": "publish",
        }
        if search:
            params["search"] = search
        if category:
            params["category"] = str(category)
        if sku:
            params["sku"] = sku

        url = f"{self._base_url}/products?{urlencode(params)}"

        try:
            resp = await self._http_client.get(url, auth=self._auth)
            resp.raise_for_status()
            products = resp.json()
            return [self._format_product(p) for p in products]
        except Exception as e:
            logger.error(f"Error buscando productos: {e}", exc_info=True)
            return []

    async def iter_all_products(
        self,
        per_page: int = 100,
        status: str = "publish",
    ):
        """Itera todo el catálogo paginando /products hasta agotarlo.

        Async generator: yield-ea productos uno por uno ya formateados
        con `_format_product` (atributos como dict por nombre, no la lista raw
        de WC). Lo consumen `motor_catalog.build_motor_expand_from_wc` y
        `motor_catalog.build_brand_models_from_wc` para construir los índices
        dinámicos en bootstrap y en el refresh periódico.

        Args:
            per_page: Tamaño de página WC. Default 100. Bajar si el hosting
                da timeout (ej: 50).
            status: Status del producto. Default "publish" para no traer
                borradores. Pasar "any" si se necesitan todos.

        Si una página falla (timeout, 5xx), loguea el error y corta la
        iteración — no levanta excepción para no tumbar el bootstrap.
        El builder ya maneja índice parcial.
        """
        page = 1
        total_yielded = 0
        while True:
            params = {
                "per_page": per_page,
                "page": page,
                "status": status,
            }
            url = f"{self._base_url}/products?{urlencode(params)}"
            try:
                resp = await self._http_client.get(url, auth=self._auth)
                resp.raise_for_status()
            except Exception as e:
                logger.error(
                    f"[iter_all_products] page={page} falló: {type(e).__name__}: {e} "
                    f"— corto iteración (yielded {total_yielded} productos antes)"
                )
                return

            raw_products = resp.json()
            n = len(raw_products)
            if n == 0:
                break

            for raw in raw_products:
                yield self._format_product(raw)
                total_yielded += 1

            # Última página detectada por tamaño parcial
            if n < per_page:
                break
            page += 1

    async def get_product(self, product_id: int) -> dict | None:
        """Obtiene un producto específico por ID."""
        url = f"{self._base_url}/products/{product_id}"
        try:
            resp = await self._http_client.get(url, auth=self._auth)
            resp.raise_for_status()
            return self._format_product(resp.json())
        except Exception as e:
            logger.error(f"Error obteniendo producto {product_id}: {e}")
            return None

    async def get_attribute_terms(self, attribute_id: int, per_page: int = 100) -> list[dict]:
        """Obtiene los terms disponibles de un atributo de producto."""
        url = f"{self._base_url}/products/attributes/{attribute_id}/terms?per_page={per_page}"
        try:
            resp = await self._http_client.get(url, auth=self._auth)
            resp.raise_for_status()
            return [{"id": t["id"], "name": t["name"], "slug": t["slug"]} for t in resp.json()]
        except Exception as e:
            logger.error(f"Error obteniendo terms de atributo {attribute_id}: {e}")
            return []

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
        """Busca productos por texto libre y filtra en Python por atributos del vehículo.

        Nota: el filtro attribute/attribute_term de WooCommerce REST API genera JOINs
        que superan el timeout del hosting. Se usa fulltext search + filtrado en Python.

        Args:
            search: Tipo de repuesto (ej: "tapa cilindros"). Se manda como ?search= a WC.
            attribute_filters: Dict con slug de atributo -> valor a matchear (substring, case-insensitive).
                Ej: {"pa_marca-vehiculo": "renault", "pa_modelo": "kangoo", "pa_motor": "1.9"}
            per_page: Cantidad de resultados finales deseados (se piden más a la API para compensar el filtrado).
            page: Página
            repuesto: Tipo de repuesto pedido por el cliente (ej: "tapa cilindros", "junta tapa").
                    Si viene, se aplica filtro defensivo `_strict_repuesto_match` para descartar
                    productos físicamente distintos que el fulltext de WC mete por ambigüedad de
                    substring (ej: "tapa cilindros" matchea "Junta Tapa De Cilindros..."). Sin esto,
                    detección de variantes (medida/válvulas/retenes) puede disparar sobre productos
                    del tipo equivocado.
            medida: Si viene, filtra los resultados por esa medida específica (ej: "STD", "0.25", "3M").
                    Cuando no viene y hay variantes de medida, retorna dict con needs_clarification='medida'.
            valvulas: Si viene ('8v' o '16v'), filtra resultados a esa variante. Cuando NO viene
                    y hay variantes 8v/16v, retorna dict con needs_clarification='valvulas'.
            retenes: Si viene ('con' o 'sin'), filtra resultados a esa variante. Cuando NO viene
                    y hay variantes con/sin retenes, retorna dict con needs_clarification='retenes'.

            motor_code: Si viene (ej: "TU5JP4", "THP EP6DT"), filtra los resultados al
                    código de motor específico. Cuando NO viene y el set tiene 2+ códigos
                    distintos para la misma (cilindrada, válvulas), retorna dict con
                    needs_clarification='motor'. Pensado para desambiguar productos
                    físicamente distintos que comparten cilindrada+válvulas+combustible
                    (caso testigo: Peugeot 207 1.6 16v TU5JP4 vs THP EP6DT).

        Orden de detección de clarificaciones (la primera dimensión ambigua gana):
            valvulas → retenes → calentador → motor → medida.
        """
        # Pedimos más resultados de los necesarios para que el filtrado Python tenga material.
        # Subido a fijo en 100 (10-may-2026) tras detectar con run_search_cases que en
        # consultas de marcas minoritarias (caso SRCH-0003: Suzuki Vitara 2.0 TD RF) el
        # fulltext de WC quedaba dominado por productos de categoría junta/bulones y las
        # tapas correctas (SKU ZL00117) caían fuera del top 50, dejando al filtro Python
        # sin candidatos válidos. 100 es el max default de la API WC en una sola request
        # (sin paginar). Si el hosting compartido empieza a dar timeout, bajar progresivamente.
        fetch_count = 100
        params = {
            "per_page": fetch_count,
            "page": page,
            "status": "publish",
        }
        if search:
            params["search"] = search

        url = f"{self._base_url}/products?{urlencode(params)}"
        try:
            resp = await self._http_client.get(url, auth=self._auth)
            resp.raise_for_status()
            products = [self._format_product(p) for p in resp.json()]
            logger.warning(f"[WC_RAW] search='{search}' → {len(products)} productos antes de filtrar")
            if products:
                # Listamos todos los nombres + atributos clave para diagnosticar por qué una variante
                # esperada (ej: tapa horizontal) no aparece después del filtro Python por marca/modelo/motor.
                for i, prod in enumerate(products):
                    attrs = prod.get("attributes", {})
                    modelo_v = attrs.get("Modelo", [])
                    motor_v = attrs.get("motor", [])
                    logger.warning(
                        f"[WC_RAW] [{i}] sku='{prod.get('sku')}' name='{prod.get('name')}' "
                        f"pa_modelo={modelo_v} pa_motor={motor_v}"
                    )
        except Exception as e:
            logger.error(f"Error en búsqueda por atributos: {e}", exc_info=True)
            return []

        def _apply_explicit_filters(prods: list) -> list:
            """Aplica todos los filtros explícitos del cliente (repuesto + dimensiones).

            Es CRÍTICO correrlo antes de cualquier truncate por per_page: si productos
            rechazables (juntas, juegos, calentador opuesto, etc.) ocupan los slots
            del window, variantes legítimas que están más abajo en el ranking de WC
            quedan fuera y nunca llegan a la respuesta. Caso testigo: ZL00129 (tapa
            calentador horizontal) en posición 11 del set pre-truncate, mientras los
            slots 4-10 estaban ocupados por juntas TC-663-20 + juegos SJ-367 que
            _finalize iba a rechazar de todos modos.
            """
            if repuesto:
                prods = [p for p in prods if _strict_repuesto_match(p, repuesto)]
            if valvulas:
                prods = [p for p in prods if _product_matches_valvulas(p, valvulas)]
            if retenes:
                prods = [p for p in prods if _product_matches_retenes(p, retenes)]
            if calentador:
                prods = [p for p in prods if _product_matches_calentador(p, calentador)]
            if motor_code:
                prods = [p for p in prods if _product_matches_motor_code(p, motor_code)]
            if medida:
                prods = [p for p in prods if _product_matches_measure(p, medida)]
            return prods

        if not attribute_filters:
            result = _apply_explicit_filters(products)[:per_page]
            return _finalize_with_clarification(
                result,
                repuesto=repuesto,
                valvulas=valvulas,
                retenes=retenes,
                calentador=calentador,
                motor_code=motor_code,
                medida=medida,
            )

        # Filtrado en Python: substring case-insensitive sobre los atributos del producto
        attr_name_map = {
            "pa_marca-vehiculo": "Marca Vehiculo",
            "pa_modelo": "Modelo",
            "pa_motor": "motor",
            "pa_combustible": "Combustible",
        }

        def _passes_attr_filters(prod: dict, filters: dict) -> bool:
            for attr_slug, term_value in filters.items():
                attr_name = attr_name_map.get(attr_slug, attr_slug)
                product_values = prod.get("attributes", {}).get(attr_name, [])
                normalized = (
                    _BRAND_ALIASES.get(term_value.lower(), term_value.lower())
                    if attr_slug == "pa_marca-vehiculo"
                    else term_value.lower()
                )
                if product_values and not any(normalized in v.lower() for v in product_values):
                    return False
            return True

        # ─── Pre-pa_modelo calentador detection (catalog-gap rescue) ───
        # Calentador (vertical/horizontal) es una distinción a nivel marca+motor,
        # NO a nivel modelo: un mismo motor en una misma marca tiene siempre las
        # mismas variantes de calentador. Si el catálogo tiene tagging asimétrico
        # (ej: vertical con pa_modelo=Kangoo cargado pero horizontal sin Kangoo),
        # el filtro pa_modelo descartaría silenciosamente la otra variante y la
        # detección nunca dispararía. Esta pasada corre los detectores antes del
        # filtro pa_modelo para rescatar el caso (mismo patrón CHT/Fox del 8-abr).
        if not calentador and "pa_modelo" in attribute_filters:
            wide_filters = {k: v for k, v in attribute_filters.items() if k != "pa_modelo"}
            wide_filtered = [
                p for p in products
                if _passes_attr_filters(p, wide_filters)
                and (not repuesto or _strict_repuesto_match(p, repuesto))
            ]
            wide_clar = _detect_calentador_variants(wide_filtered)
            if wide_clar:
                logger.warning(
                    f"[WIDE_CLAR] calentador detectado pre-pa_modelo "
                    f"(brand+motor={list(wide_filters.keys())}): {wide_clar.get('available_calentador')}"
                )
                return wide_clar

        filtered = [p for p in products if _passes_attr_filters(p, attribute_filters)]

        # Filtros explícitos ANTES del truncate per_page (ver _apply_explicit_filters
        # arriba para explicación detallada del bug que esto resuelve).
        filtered = _apply_explicit_filters(filtered)

        result = filtered[:per_page]

        return _finalize_with_clarification(
            result,
            repuesto=repuesto,
            valvulas=valvulas,
            retenes=retenes,
            calentador=calentador,
            motor_code=motor_code,
            medida=medida,
        )

    async def get_categories(self, per_page: int = 50) -> list[dict]:
        """Lista las categorías de productos."""
        url = f"{self._base_url}/products/categories?per_page={per_page}"
        try:
            resp = await self._http_client.get(url, auth=self._auth)
            resp.raise_for_status()
            return [
                {"id": c["id"], "name": c["name"], "count": c["count"]}
                for c in resp.json()
            ]
        except Exception as e:
            logger.error(f"Error obteniendo categorías: {e}")
            return []

    def _format_product(self, raw: dict) -> dict:
        """Formatea un producto de WooCommerce para el agente."""
        return {
            "id": raw.get("id"),
            "name": raw.get("name", ""),
            "sku": raw.get("sku", ""),
            "price": raw.get("price", ""),
            "regular_price": raw.get("regular_price", ""),
            "sale_price": raw.get("sale_price", ""),
            "stock_status": raw.get("stock_status", ""),
            "description_short": self._strip_html(
                raw.get("short_description", "")
            ),
            "categories": [
                c["name"] for c in raw.get("categories", [])
            ],
            "attributes": {
                attr["name"]: attr["options"]
                for attr in raw.get("attributes", [])
            },
            "permalink": raw.get("permalink", ""),
        }

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remueve tags HTML básicos de descripciones."""
        return re.sub(r"<[^>]+>", "", text).strip()
