import json
import logging
import re
from datetime import datetime, timezone, timedelta

import litellm

from app.woocommerce import (
    WooCommerceClient,
    _detect_calentador_variants,
    _detect_measure_variants,
    _detect_motor_variants,
    _detect_retenes_variants,
    _detect_valvulas_variants,
    _product_matches_calentador,
    _product_matches_retenes,
    _product_matches_valvulas,
)
from app.database import Database
from app.motor_catalog import lookup_motors, lookup_marca_by_modelo, MotorExpandIndex

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Motor terms hardcodeados desde WooCommerce (attribute id=2, 212 términos).
# Obtenidos vía GET /products/attributes/2/terms. Actualizar si cambia el catálogo.
# ---------------------------------------------------------------------------
_WC_MOTOR_TERMS: tuple[str, ...] = (
    "1.0 12v", "1.0 16v Fire", "1.0 16v Firefly", "1.0 8v", "1.0 8v D7D",
    "1.0 8v F10A", "1.0 8v Fire", "1.0 8v Fire Evo", "1.0 8v Zetec Rocam",
    "1.1 8v", "1.1 turbo",
    "1.2 16v 3NR-FE", "1.2 16v D4F", "1.2 8v D7F",
    "1.3 16v B3", "1.3 16v Fire", "1.3 16V G13B", "1.3 8v", "1.3 8v C3G",
    "1.3 8v D", "1.3 8v Endura", "1.3 8v Fire", "1.3 8v Firefly", "1.3 8v MPI",
    "1.4 16v", "1.4 16v 14K16", "1.4 16v Fire", "1.4 16v FSI",
    "1.4 16V T-Jet", "1.4 16v TFSI", "1.4 16v TSI", "1.4 16v Zetec",
    "1.4 8v", "1.4 8v Energy E7J", "1.4 8v Fire", "1.4 8v Fire Evo",
    "1.4 8v HDI DV4", "1.4 8v Junior", "1.4 8v TDCI DV4", "1.4 8v Tipo",
    "1.4 8v TU3JP", "1.4 8v TUD3",
    "1.5 12v Dragon", "1.5 16v", "1.5 16v 2NR-FE", "1.5 16v Sigma",
    "1.5 8v", "1.5 8v DCI K9K", "1.5 8v Tipo", "1.5 8v TU4",
    "1.6 16v", "1.6 16v 16k16", "1.6 16v 4AFE", "1.6 16v E-Torq",
    "1.6 16v H4M", "1.6 16v HDI DV6", "1.6 16v HR16DE", "1.6 16v K4M",
    "1.6 16v MSI", "1.6 16v Sigma", "1.6 16V TD DV6", "1.6 16v THP EP6DT",
    "1.6 16v Torque", "1.6 16v TU5JP4", "1.6 16v Zetec", "1.6 16v Zetec SE",
    "1.6 8v", "1.6 8v C1L/C2L - CARBURADOR", "1.6 8v C3L - INYECCION",
    "1.6 8v CHT", "1.6 8v D", "1.6 8v HDI DV6", "1.6 8v K7M",
    "1.6 8v Tipo", "1.6 8v TU5JP", "1.6 8v XU5", "1.6 8v Zetec Rocam",
    "1.6v",
    "1.7 16v Y17DTL", "1.7 8v 4EE1", "1.7 8v 4EE1T", "1.7 8v D",
    "1.7 8v F2N", "1.7 8v TD", "1.7 8v TDI X17DTL",
    "1.8 16v", "1.8 16v 4G93", "1.8 16v B4184S", "1.8 16v E-Torq",
    "1.8 16v F4P", "1.8 16v MR18", "1.8 16v Twin Spark", "1.8 16v XU7JP4",
    "1.8 16v Zetec", "1.8 20v TN",
    "1.8 8v", "1.8 8v D", "1.8 8v F3P", "1.8 8v TD",
    "1.8 8v TDCI - Duratorq", "1.8 8v TDCI Duratorq", "1.8 8v XU7", "1.8 8v XUD7",
    "1.9 16v", "1.9 8v D", "1.9 8v DW8", "1.9 8v F8Q", "1.9 8v F9Q",
    "1.9 8v SDI", "1.9 8v TD", "1.9 8v TDI", "1.9 8v TDI INY. BOMBA",
    "1.9 8v XU9", "1.9 8v XUD9", "1.9 8v XUD9T",
    "2.0 16v", "2.0 16v B4204S/T", "2.0 16v Duratec", "2.0 16v EW10J4",
    "2.0 16v F20", "2.0 16v F4R", "2.0 16v N7Q", "2.0 16v TD Multijet",
    "2.0 16v TDI", "2.0 16v Twin Spark", "2.0 16v XU10J4", "2.0 16v Zetec",
    "2.0 8v", "2.0 8v 4G52/G52B", "2.0 8v F3R", "2.0 8v HDI DW10",
    "2.0 8v TD", "2.0 8v TD RF", "2.0 8v XD 4.88", "2.0 8v XU10",
    "2.1 8v J8S",
    "2.2 16v", "2.2 16V HDI Puma", "2.2 16v TDCi Puma",
    "2.2 8v", "2.2 8v D HW", "2.2 8v D R2",
    "2.3 16v JTD F1AE", "2.3 16v TD M9T",
    "2.3 8v", "2.3 8v 4ZD1", "2.3 8v XD2",
    "2.4 10v TD", "2.4 16v", "2.4 16v TDCI - Duratorq", "2.4 16v TDCI Duratorq",
    "2.4 8v", "2.4 8v 22R", "2.4 8v D/TD 2L", "2.4 8v OM616 - MB180",
    "2.5 16v 2KD-FTV", "2.5 16v DCI G9U", "2.5 16v TD 2KD-FTV",
    "2.5 8v 4JA1", "2.5 8v D", "2.5 8v D 4D56", "2.5 8v D D4B",
    "2.5 8v IVECO", "2.5 8v TD 4D56T", "2.5 8v TD D4B",
    "2.5 8v TD VM", "2.5 8v TDI", "2.5 8v XD3",
    "2.7 8v D J2", "2.7 8v TD27", "2.7 8v TD27T",
    "2.8 30v V6", "2.8 8v 4JB1", "2.8 8v 4M40", "2.8 8v D/TD 3L",
    "2.8 8v IVECO", "2.8 8v TDI",
    "2.9 16v CRDI J3",
    "3.0 12v 187", "3.0 12v 188", "3.0 12v Max Econo", "3.0 16v 1KD-FTV",
    "3.0 16v TD - Maxion", "3.0 8v 1KZ-T", "3.0 8v 1KZ-TE",
    "3.0 8v D JT", "3.0 8v D/TD 5L",
    "3.1 10V TD VM", "3.1 8v 4JG2", "3.1 8v 4JG2T",
    "3.2 12v 194", "3.3 8v 4-203",
    "3.6 12 221", "3.6 12v 221", "3.6 12v 221 SP",
    "3.8 12v 230", "4.0 12v 250", "4.0 12v V6 SOHC",
    "5.0 12v 6-305 PF",
    "750", "800 6v F8CV", "800cc 8v F8A", "850",
    "OM 366",
)

# Códigos de motor que indican diesel. Se usa para agregar "diesel" al texto
# de búsqueda y mejorar el ranking en WooCommerce fulltext.
_DIESEL_CODES: frozenset[str] = frozenset({
    "F8Q", "F9Q", "K9K", "G9U",
    "HDI", "TDI", "TDCI", "DCI", "SDI", "JTD", "CDI",
    "DW8", "DW10", "DV4", "DV6",
    "TUD", "TUD3", "XUD", "XUD7", "XUD9",
    "D7D", "4EE1",
})

# Expansión por (marca_normalizada, cilindrada) → códigos completos del catálogo.
# Códigos de motor cubiertos por los snapshots locales anonimizados.
_MOTOR_EXPAND: dict[tuple[str, str], list[str]] = {
    ("renault",    "1.6"): ["1.6 16v K4M", "1.6 8v K7M"],
    ("volkswagen", "1.6"): ["1.6 8v CHT", "1.6 8v AP"],
    # Peugeot/Citroen 1.6: incluyen tanto la variante diesel HDI DV6 como la
    # naftera TU5JP4. El paso 3 filtra por combustible (ver _expand_motor),
    # entonces si el cliente pide nafta no se le entrega HDI y viceversa.
    ("peugeot",    "1.6"): [
        "1.6 8v HDI DV6", "1.6 16v HDI DV6",
        "1.6 16v TU5JP4", "1.6 8v TU3JP",
    ],
    ("citroen",    "1.6"): [
        "1.6 8v HDI DV6", "1.6 16v HDI DV6",
        "1.6 16v TU5JP4",
    ],
    ("chevrolet",  "1.6"): ["1.6 8v"],
    ("fiat",       "1.4"): ["1.4 8v Fire"],
    ("suzuki",     "2.0"): ["2.0 8v TD RF"],
    # Toyota HiLux: motores diesel con barra "/" en pa_motor del catalogo.
    # El sistema dinamico ya los indexa pero ponemos el hardcoded por si el
    # snapshot dinamico falla. Caso testigo: RND-0078 (HiLux 2.8) y RND-0090
    # (HiLux 3.0).
    ("toyota",     "2.8"): ["2.8 8v D/TD 3L"],
    ("toyota",     "3.0"): ["3.0 8v D/TD 5L"],
    # Ford y Renault de los runs: cubrimos motores de stock que el sistema
    # dinamico tambien tiene pero por si acaso (caso testigo RND-0094 Fiesta
    # 1.3 Endura, RND-0006 Laguna 2.0 N7Q).
    ("ford",       "1.3"): ["1.3 8v Endura"],
    ("ford",       "1.8"): ["1.8 16v Zetec"],
    ("renault",    "2.0"): ["2.0 16v N7Q"],
    # Indenor 504/505: ya esta en _BRAND_ALIASES como "indenor"->"peugeot",
    # y la cilindrada 2.3 cae a estos motores. (RND-0011/0045/0095)
    ("peugeot",    "2.3"): ["2.3 8v XD2"],
    ("peugeot",    "2.5"): ["2.5 8v XD3"],
}

# Aliases de marca para normalizar antes del lookup en _MOTOR_EXPAND.
# Mantener sincronizado con app.woocommerce::_BRAND_ALIASES (que tambien
# normaliza la marca para el filtro pa_marca-vehiculo). Ambos comparten
# la misma fuente de verdad: las marcas que aparecen en pa_marca-vehiculo
# del catalogo + los aliases que el cliente puede decir.
_MARCA_ALIASES: dict[str, str] = {
    "vw": "volkswagen",
    "volkswagen": "volkswagen",
    "renault": "renault",
    "peugeot": "peugeot",
    "citroen": "citroen",
    "citroën": "citroen",
    "chevrolet": "chevrolet",
    "chevy": "chevrolet",
    "fiat": "fiat",
    "suzuki": "suzuki",
    "ford": "ford",
    "toyota": "toyota",
    "audi": "audi",
    "nissan": "nissan",
    "perkins": "perkins",
    "maxion": "maxion",
    "mitsubishi": "mitsubishi",
    "honda": "honda",
    "hyundai": "hyundai",
    "kia": "kia",
    "iveco": "iveco",
    # Indenor: fabricante del motor diesel del Peugeot 504. En AR algunos
    # clientes se refieren al auto como "Indenor". El catálogo carga estos
    # SKUs como pa_marca-vehiculo=Peugeot. Sin este alias, el filtro Python
    # con marca='indenor' descarta el SKU correcto. Caso testigo: RND-0011.
    "indenor": "peugeot",
    # Daewo (catalogo tiene typo, sin doble o). Daewoo del cliente mapea al typo.
    "daewo": "daewo",
    "daewoo": "daewo",
}

# Códigos que indican que el motor ya viene completo — no expandir.
# Se usan como substring (motor_limpio contiene alguno de estos).
# NO agregar tokens de 2-3 letras que sean ambiguos (ej: "ap" solo matchea "1.6 ap",
# lo que cortocircuita la expansión brand-aware). Usar siempre la combinación completa.
_KNOWN_MOTOR_CODES: tuple[str, ...] = (
    "k4m", "k7m", "cht", "dv6", "dv4", "k9k", "f8q", "f9q", "dw8",
    "xud9", "fire", "zetec", "tdi", "sdi", "tun3",
    # VW AP: combinaciones completas en lugar del token suelto "ap"
    "1.6 ap", "1.6 8v ap",
)


def _has_diesel_code(motor_name: str) -> bool:
    """Retorna True si el nombre del motor contiene un código diesel conocido."""
    upper = motor_name.upper()
    return any(code in upper for code in _DIESEL_CODES)


# Stopwords y longitud máxima usadas por _simplify_search_text para descartar
# tokens que no aportan al fulltext de WC. Stopwords son preposiciones y
# artículos que aparecen en cualquier title y no restringen. La longitud
# máxima descarta palabras compuestas que el catálogo suele escribir distinto
# (ej: "semihidraulicos" 15 chars vs title "Semi Hidraulicos" con espacio).
_SEARCH_TEXT_STOPWORDS = {
    "de", "del", "la", "el", "en", "para", "con", "y", "o",
    # Token compuesto problemático: 16 de 20 SKUs lo escriben como
    # "Semi Hidraulicos" (separado) y solo 4 como "Semihidraulicos" junto.
    # Si lo dejamos pasar al fulltext con el nuevo cap de 16 chars, los 16
    # SKUs con espacio se caen porque LIKE %semihidraulicos% no matchea
    # "Semi Hidraulicos". Drop explícito. Casos cubiertos: aros PC88418,
    # PMA7716, PMA7814, PC88408, PC88413, etc.
    "semihidraulicos", "semihidraulico",
}
# Subido de 10 → 16 (8-may-2026): el cap de 10 dropeaba "descarbonización"
# (16 chars) y dejaba el search_text en "junta X.X", lo que hacía que el SKU
# target compitiera contra cientos de juntas genéricas y se cayera del top 50
# de WC fulltext. Casos testigo del run 20260508_220522 (FAIL_BOT JD-*):
# RND-0014, 0017, 0037, 0085, 0093.
_SEARCH_TEXT_MAX_TOKEN_LEN = 16
# Subido de 2 → 3 (8-may-2026): para repuestos como "junta tapa de válvulas"
# o "junta tapa de cilindros", los dos primeros tokens son siempre "junta tapa"
# y la palabra discriminante (válvulas vs cilindros) era la tercera y se
# perdía. Resultado: el bot mandaba search_text='junta tapa X.X' para ambos
# rubros, y los SKUs JVS competían contra TC en el mismo fulltext. Casos
# testigo: RND-0007, 0009, 0027, 0067 (JVS) y RND-0011, 0065 (TC).
_SEARCH_TEXT_MAX_STRONG_TOKENS = 3


def _simplify_search_text(repuesto: str, motor_full: str = "", marca: str = "") -> str:
    """Genera el search_text mínimo para WC fulltext: hasta 3 sustantivos + cilindrada.

    Motivación: WC `?search=` exige que cada token aparezca como substring en
    title (o description). Dos requisitos opuestos:

    (a) El search_text NO debe incluir el motor expandido entero porque sus
        códigos ('2.8 8v IVECO', '3.0 8v 1KZ-T', 'TDCI - Duratorq') a veces
        están escritos distinto en el title del catálogo y rompen el match
        aunque el SKU exista.

    (b) El search_text TAMPOCO puede ser tan laxo como solo "primer-sustantivo +
        cilindrada" porque queries genéricas como "junta 1.6" matchean cientos
        de productos en WC y el target SKU queda fuera del límite de fetch
        (per_page * 5 = 50). El target nunca es visto por el filtro Python.

    Estrategia: tomar hasta `_SEARCH_TEXT_MAX_STRONG_TOKENS` tokens fuertes del
    repuesto (no-stopword, longitud <= `_SEARCH_TEXT_MAX_TOKEN_LEN`) + cilindrada.
    Eso restringe el universo del fulltext sin meter tokens del motor que pueden
    no estar en el title. El matching fino (motor exacto, modelo, marca,
    combustible) queda en el filtro Python sobre los atributos canónicos del
    catálogo.

    Por qué descartar tokens largos: palabras como "semihidraulicos" (15) suelen
    estar separadas en el catálogo y romperían el match. El cap actual (16) deja
    pasar "descarbonización" (16) que sí está escrita junta en los títulos JD-*.

    Por qué hasta 3 tokens (no 2 ni 4): con 2 tokens, repuestos como
    "junta tapa de válvulas" perdían "válvulas" (3er token) y los JVS-*
    competían en el fulltext con los TC-* generando 0 hits para el target.
    Con 4+ tokens el riesgo de que algún token no esté en el title vuelve a
    crecer.

    Casos testigo:
        Sample 18-fail (validados pre-aplicación):
            Bucket 1 (motor expandido en search_text) — recuperados pasando del
            motor completo a "primer sustantivo + cilindrada": RND-0011, 0013,
            0044, 0045, 0090, 0094, 0095 + alias indenor.
            Bucket B (search_text demasiado laxo, target fuera de fetch_count=50)
            — recuperados al subir a 2 tokens: RND-0030 ("junta tapa 2.4"),
            RND-0039 ("junta tapa 1.9"), RND-0063 ("junta tapa 1.6"),
            RND-0096 ("junta tapa 1.4").
            Aros (palabra compuesta semihidraulicos): RND-0054, 0089, 0094 — el
            límite original max_token_len=10 dropeaba "semihidraulicos". Con el
            cap 16 actual sigue siendo dropeada (15 chars también > 14 si se
            verificó, pero entra en 16); validar regresión post-cambio.

        Run 20260508_220522 (12 FAIL_BOT que motivan este cambio):
            JD-* (descarbonización) — RND-0014, 0017, 0037, 0085, 0093: el cap
            de longitud 10 dropeaba "descarbonización", search_text quedaba
            "junta X.X". Fix: cap 16 deja entrar el sustantivo discriminante.
            JVS-* (junta tapa válvulas) — RND-0007, 0009, 0027, 0067: el cap
            de tokens 2 dropeaba "válvulas" (3er token fuerte), search_text
            quedaba "junta tapa X.X" e indistinguible de TC. Fix: cap 3.
            TC-* (junta tapa cilindros) — RND-0011, 0065: mismo problema, el
            tercer token "cilindros" discriminaba pero se perdía. Fix: cap 3.
    """
    if not repuesto and not motor_full and not marca:
        return ""
    parts: list[str] = []
    if repuesto:
        tokens = repuesto.strip().split()
        strong = [
            t for t in tokens
            if t.lower() not in _SEARCH_TEXT_STOPWORDS
            and len(t) <= _SEARCH_TEXT_MAX_TOKEN_LEN
        ]
        parts.extend(strong[:_SEARCH_TEXT_MAX_STRONG_TOKENS])
        # Fallback: si descartamos todos por stopword/length, usar el primer
        # token tal cual para no quedarnos sin search_text.
        if not parts and tokens:
            parts.append(tokens[0])
    if marca:
        # Normalizamos a la forma canónica del catálogo (lowercase de _MARCA_ALIASES)
        # para maximizar el matching del fulltext de WC: el cliente puede decir "VW"
        # pero el title del producto dice "Volkswagen". Sin esta normalización el
        # alias quedaba afuera del search_text y los productos con marca explícita
        # en title no entraban al top de resultados. Agregar marca al search reduce
        # drásticamente el universo del fulltext (de miles a decenas), lo que permite
        # que SKUs viejos (creados 2023, sort por date_created DESC) entren igual.
        # Trade-off: si la marca del cliente no aparece literal en title del producto
        # correcto, lo perdemos del fulltext. Mitigación: el fallback de
        # search_products_advanced descarta pa_modelo y re-itera, y si tampoco encuentra,
        # avisa no_catalogado — el cliente puede reintentar con texto libre.
        marca_norm = _MARCA_ALIASES.get(marca.strip().lower(), marca.strip().lower())
        if marca_norm:
            parts.append(marca_norm)
    if motor_full:
        m = re.match(r"(\d+\.\d+)", motor_full.strip())
        if m:
            parts.append(m.group(1))
        elif not parts:
            # Sin cilindrada extraíble y sin repuesto: caer al motor literal
            parts.append(motor_full.strip())
    return " ".join(parts).strip()


def _singularize_search_text(text: str) -> str:
    """Mejora el recall del fulltext de WooCommerce despluralizando tokens.

    WC usa LIKE %token% para cada palabra del ?search=. Si el cliente pide
    en plural ('cojinetes de bancada') y el catálogo guarda en singular
    ('Cojinete De Bancada'), %cojinetes% no matchea %Cojinete% porque
    'cojinetes' no es substring de 'Cojinete'. Stripeando la 's' final,
    'cojinete' matchea ambos casos por substring (también 'Cojinetes' contiene
    'cojinete'), así que la regla mejora recall sin riesgo de regresión en
    productos con plural en catálogo (ej: 'Bulones' contiene 'bulone').

    Reglas por token:
      - len > 3 (preserva 'los', 'es', 'la')
      - termina en 's'
      - NO es acrónimo en mayúsculas (preserva STD, TDI, F9Q)
      - NO contiene dígitos (preserva 1.6mm, 8v, 110mm)

    Caso testigo: MULTI-0007 — 'cojinetes de bancada 1.9 8v F9Q' devolvía 0
    productos en WC porque ningún título contiene 'cojinetes'. Con el fix
    queda 'cojinete de bancada 1.9 8v F9Q' y matchea SKU 20-248/4-STD.
    """
    if not text:
        return text
    out: list[str] = []
    for token in text.split():
        if (
            len(token) > 3
            and token[-1].lower() == "s"
            and not token.isupper()
            and not any(c.isdigit() for c in token)
        ):
            out.append(token[:-1])
        else:
            out.append(token)
    return " ".join(out)


def _expand_motor(
    motor_query: str,
    marca: str = "",
    max_results: int = 2,
    combustible: str = "",
    motor_expand_index: MotorExpandIndex | None = None,
) -> list[str]:
    """Expande una cilindrada/motor parcial a códigos completos del catálogo.

    Orden de preferencia:
    1. Si el motor ya contiene un código conocido, se devuelve sin expansión.
    2. Si hay marca + cilindrada + índice dinámico, lookup (respeta combustible).
    3. Fallback al _MOTOR_EXPAND hardcodeado (cobertura mínima histórica).
    4. Fallback final: substring en _WC_MOTOR_TERMS (comportamiento legacy).
    """
    if not motor_query:
        return []

    motor_limpio = motor_query.strip().lower()

    # Paso 1: motor ya contiene un código específico → enriquecer con term del WC
    # si es posible. El cliente puede pedir "1.9 F9Q" pero pa_motor del catálogo
    # guarda "1.9 8v F9Q". El filtro Python hace substring match: "1.9 f9q" NO es
    # substring de "1.9 8v f9q" (el "8v" en el medio rompe el match). Por eso
    # buscamos el term completo del catálogo que contenga TODOS los tokens del
    # query, y lo retornamos enriquecido. Si no hay match unívoco, devolvemos
    # el query original (comportamiento legacy).
    if any(code in motor_limpio for code in _KNOWN_MOTOR_CODES):
        q_tokens = motor_limpio.split()
        if len(q_tokens) >= 2:
            matches = [
                m for m in _WC_MOTOR_TERMS
                if all(t in m.lower() for t in q_tokens)
            ]
            if matches:
                # Diesel/específicos primero; dentro, más corto = más específico
                matches.sort(key=lambda m: (not _has_diesel_code(m), len(m)))
                enriched = matches[0]
                if enriched.lower() != motor_limpio:
                    logger.warning(
                        f"[EXPAND_MOTOR] match especifico WC: '{motor_query}' -> '{enriched}'"
                    )
                    return [enriched]
        return [motor_query]

    # Extraer cilindrada base ("1.6 dci" → "1.6", "1.9 td" → "1.9")
    cil_match = re.match(r"(\d+\.\d+)", motor_limpio)
    cil = cil_match.group(1) if cil_match else None

    # Paso 2: lookup en índice dinámico (WC) — prioridad si está disponible
    if cil and marca and motor_expand_index:
        dynamic_hits = lookup_motors(
            motor_expand_index,
            marca=marca,
            cilindrada=cil,
            combustible=combustible,
            max_results=max_results,
        )
        if dynamic_hits:
            logger.warning(
                f"[EXPAND_MOTOR] dynamic (WC) expansion: marca={marca}, cil={cil}, "
                f"combustible={combustible!r} → {dynamic_hits}"
            )
            return dynamic_hits

    # Paso 3: fallback al _MOTOR_EXPAND hardcodeado (brand-aware)
    if cil and marca:
        marca_norm = _MARCA_ALIASES.get(marca.strip().lower(), marca.strip().lower())
        key = (marca_norm, cil)
        if key in _MOTOR_EXPAND:
            specific = _MOTOR_EXPAND[key]
            # Filtrar por combustible: si el cliente pidió nafta y la entrada
            # del dict tiene motores con códigos diesel (HDI, TDI, etc.), no
            # devolverlos. Caso testigo RND-0089: ("peugeot","1.6") apuntaba
            # a HDI DV6 y descartaba al SKU naftero TU5JP4 cuando el cliente
            # pedía "Peugeot 206 1.6 nafta". El indice dinamico ya respeta
            # combustible — esta logica deja al fallback hardcoded igual de
            # respetuoso.
            comb_norm = (combustible or "").strip().lower()
            filtered = specific
            if comb_norm == "nafta":
                filtered = [m for m in specific if not _has_diesel_code(m)]
            elif comb_norm == "diesel":
                # Preferir diesel pero no excluir si la entrada solo tiene nafta
                only_diesel = [m for m in specific if _has_diesel_code(m)]
                if only_diesel:
                    filtered = only_diesel
            if not filtered:
                # Si el filtro de combustible elimina todo, no devolvemos
                # nada del fallback hardcoded — caemos al Paso 4 (substring).
                logger.warning(
                    f"[EXPAND_MOTOR] hardcoded {key} filtrado por "
                    f"combustible={comb_norm!r} → vacio, fallback paso 4"
                )
            else:
                # Calcular expansión pelada: si todos los códigos comparten el mismo token de válvulas
                # (ej: todos "8v"), usar cil+token como primera expansión (matchea Fox, Polo, etc.).
                # Si hay mezcla 8v/16v, usar solo la cilindrada para no excluir ningún sub-tipo.
                valve_tokens = {e.split()[1].lower() for e in filtered if len(e.split()) >= 2}
                bare = f"{cil} {valve_tokens.pop()}" if len(valve_tokens) == 1 else cil
                candidates = [bare] + [e for e in filtered if e.lower() != bare.lower()]
                logger.warning(
                    f"[EXPAND_MOTOR] brand-aware (hardcoded) expansion: "
                    f"{key} comb={comb_norm!r} → {candidates[:max_results]}"
                )
                return candidates[:max_results]

    # Paso 4: fallback legacy — búsqueda por substring/token en _WC_MOTOR_TERMS
    q = motor_limpio
    pattern = r'\b' + re.escape(q) + r'\b'
    matches = [m for m in _WC_MOTOR_TERMS if re.search(pattern, m, re.IGNORECASE)]
    if not matches:
        # Fallback: substring (para queries parciales como "1.9 xud")
        matches = [m for m in _WC_MOTOR_TERMS if q in m.lower()]
    if not matches:
        # Fallback token: todos los tokens presentes en el term (ej: "1.4 Fire" → "1.4 8v Fire")
        tokens = q.split()
        if len(tokens) > 1:
            matches = [m for m in _WC_MOTOR_TERMS if all(t in m.lower() for t in tokens)]
    # Diesel/específicos primero; dentro de cada grupo, más corto = más específico
    matches.sort(key=lambda m: (not _has_diesel_code(m), len(m)))
    return matches[:max_results]


def _build_compat_string(group: list[dict]) -> str:
    """Construye string de compatibilidad agrupando modelos por marca.

    Ejemplo: "compatible con Peugeot 206/207, Citroen Berlingo/C3, Fiat Qubo"
    """
    marca_to_modelos: dict[str, list[str]] = {}
    for p in group:
        attrs = p.get("attributes", {})
        for marca in attrs.get("Marca Vehiculo", []):
            if marca not in marca_to_modelos:
                marca_to_modelos[marca] = []
            for modelo in attrs.get("Modelo", []):
                if modelo not in marca_to_modelos[marca]:
                    marca_to_modelos[marca].append(modelo)
    parts = []
    for marca, modelos in marca_to_modelos.items():
        parts.append(f"{marca} {'/'.join(modelos)}" if modelos else marca)
    return "compatible con " + ", ".join(parts) if parts else ""


def _dedup_by_sku(products: list[dict]) -> list[dict]:
    """Deduplica productos con SKU idéntico.

    El catálogo publica el mismo repuesto físico varias veces, una por marca de auto
    compatible. Se mantiene el primer representante por SKU y se consolidan las marcas
    y modelos compatibles de todos los duplicados en el campo 'compatible_with'.
    Productos sin SKU se mantienen sin cambios.
    """
    if not products:
        return products

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    no_sku: list[dict] = []

    for p in products:
        sku = p.get("sku", "").strip()
        if not sku:
            no_sku.append(p)
        elif sku not in groups:
            groups[sku] = [p]
            order.append(sku)
        else:
            groups[sku].append(p)

    result = []
    for sku in order:
        group = groups[sku]
        rep = dict(group[0])
        if len(group) > 1:
            compat = _build_compat_string(group)
            if compat:
                rep["compatible_with"] = compat
        result.append(rep)

    result.extend(no_sku)
    return result


def _classify_inventory_state(products: list[dict]) -> str:
    """Clasifica el estado de inventario de una lista de productos.

    Retorna:
      "no_catalogado" — la búsqueda no encontró ningún producto (no está en catálogo).
      "agotado"       — hay productos en catálogo pero todos con stock_status=outofstock.
      "disponible"    — al menos un producto tiene stock_status=instock.
    """
    if not products:
        return "no_catalogado"
    if any(p.get("stock_status") == "instock" for p in products):
        return "disponible"
    return "agotado"


def _score_product_relevance(product: dict, modelo: str, motor: str) -> int:
    """Puntúa un producto por cuántos tokens de modelo/motor aparecen en
    su título o atributos. Usado para reordenar resultados del fallback
    cuando se buscó sin filtro pa_modelo.

    Tokens de 1-2 caracteres (artículos, preposiciones) se ignoran.
    """
    # Extraer tokens relevantes del modelo y motor del cliente
    raw = f"{modelo} {motor}".lower()
    # Separar por espacios, barras y guiones
    all_tokens = re.split(r"[\s/\-]+", raw)
    tokens = {t for t in all_tokens if len(t) > 2}

    if not tokens:
        return 0

    # Construir haystack: título + todos los valores de atributos
    haystack_parts = [product.get("name", "").lower()]
    for vals in product.get("attributes", {}).values():
        if isinstance(vals, list):
            haystack_parts.extend(v.lower() for v in vals)
        else:
            haystack_parts.append(str(vals).lower())
    haystack = " ".join(haystack_parts)

    return sum(1 for t in tokens if t in haystack)


def _sanitize_product(p: dict) -> dict:
    """Devuelve solo los campos necesarios para el LLM.

    CRÍTICO: limitar los campos evita que el modelo alucine o parafrasee
    información que no necesita ver. El campo 'name' debe usarse EXACTAMENTE
    como viene — no se parafrasea, no se abrevia.
    """
    return {
        "name": p.get("name", ""),
        "sku": p.get("sku", ""),
        "price": p.get("price", ""),
        "stock_status": p.get("stock_status", ""),
        "permalink": p.get("permalink", ""),
        "compatible_with": p.get("compatible_with", ""),
    }


SYSTEM_PROMPT = """Sos un mecánico senior especializado en motores y el asistente de una demostración de portfolio de MetaIA. Combinás conocimiento técnico con una atención clara para mecánicos y talleres. Si describen un síntoma, podés sugerir diagnósticos probables y qué repuestos revisar.

## ALCANCE DE LA DEMOSTRACIÓN
- El catálogo, los precios y el stock son ficticios y sirven solamente para evaluar la búsqueda.
- No sos una tienda activa y no procesás compras, reservas, pagos, envíos ni presupuestos reales.
- No solicites nombre, DNI, teléfono, email, dirección ni ningún otro dato personal.
- Si la persona quiere implementar una solución similar, invitala a contactar a MetaIA mediante el CTA visible en la interfaz.

## MARCAS QUE TRABAJAN
Volkswagen, Renault, Chevrolet, Fiat, Peugeot, Ford, Iveco, Mazda, Land Rover, Kia, Suzuki, y más.

## TIPOS DE REPUESTOS
Tapas de cilindros, juntas (completas, descarbonización, tapa), cigüeñales, cojinetes (bancada, biela), aros de pistón, subconjuntos, bielas, retenes/guías de válvulas, y más repuestos internos de motor.

## NOMBRE DE PRODUCTOS — REGLA CRÍTICA
- El campo `name` que devuelve la búsqueda es el nombre OFICIAL del producto en el catálogo. Usalo EXACTAMENTE como viene. NUNCA lo parafrasees, abrevies, traduzcas ni modifiques de ninguna forma.
- Si el nombre del producto incluye varios modelos (ej: "Fox Suran") o varias cilindradas (ej: "1.4 / 1.6"), no lo cambies. Mostrá el nombre exacto y aclarále al cliente: "Este retén cubre Fox y Suran 1.4/1.6 8v — es compatible con tu vehículo."
- NUNCA muestres más productos de los que devolvió la búsqueda. Si la búsqueda devuelve 1 producto, mostrás 1. Si devuelve 2, mostrás 2. NUNCA agregues productos adicionales ni menciones alternativas que no estén en los resultados.
- NUNCA inventes nombres, SKUs, precios ni URLs. Solo mostrás exactamente lo que devuelve la herramienta de búsqueda.

## TU ROL
1. Ayudás a los clientes a encontrar el repuesto que necesitan para su vehículo.
2. ANTES de buscar, asegurate de tener los datos mínimos. Si el cliente no los dio, PREGUNTÁ:
   - ¿Qué vehículo? (marca y modelo)
   - ¿Qué motor tiene? (si no sabe el código exacto, preguntale la cilindrada y si es naftero o diesel)
   - ¿Qué repuesto necesita?
3. Si el cliente ya dio toda la info en su primer mensaje (ej: "tapa de cilindros para Kangoo 1.9"), buscá directamente sin preguntar de más.
   CRÍTICO: Si el cliente NO dio el vehículo, PREGUNTÁ. NUNCA asumas ni inventes marca, modelo o motor. Si el cliente dice solo "junta tapa", respondé: "¿Para qué vehículo?"
4. Presentás las opciones con nombre EXACTO, precio demostrativo y disponibilidad ficticia.
5. Si encontrás variantes (ej: distintas medidas, versiones), mostrá todas y preguntá cuál necesita.
6. Interpretás el status que devuelve la búsqueda:
   - `no_catalogado`: la búsqueda no encontró nada. Respondé "No trabajamos ese producto" o "No lo tengo en el catálogo online — puede ser por pedido especial". NUNCA digas "agotado" ni "sin stock" en este caso.
   - `agotado`: el producto existe en catálogo pero TODOS están sin stock. Respondé "Lo tengo en catálogo pero en este momento está sin stock. ¿Querés que consulte disponibilidad a proveedor?" No lo listes como opción disponible.
   - `ok`: hay al menos un producto con stock. Antes de mostrarlo, verificá que el nombre del producto coincida con lo que el cliente pidió. Si la búsqueda devolvió productos pero ninguno es el repuesto solicitado (ej: el cliente pidió cigüeñal y solo aparecen retenes, juntas u otras piezas), respondé "No tenemos ese repuesto para ese motor" — NUNCA digas "agotado" ni "sin stock" en ese caso.
7. Si quieren comprar, explicá que la demo no vende y orientá al CTA de MetaIA; no simules una derivación comercial de repuestos.
8. Si preguntan por envíos, pagos o promociones, recordá que son datos demostrativos y que no existe una operación comercial.
9. Ante dudas de compatibilidad, aclará que un caso real requiere validación con el comercio que implemente la solución.
11. NUNCA inventes códigos de motor ni relaciones marca-modelo-motor. Si no sabés, preguntá al cliente.
12. Si el cliente hace una pregunta técnica (ej: "¿viene balanceado?", "¿cuánto sale?", "¿tiene retenes?") aunque no haya dado datos del vehículo, respondé la pregunta técnica PRIMERO y luego pedí los datos. NUNCA ignorés la pregunta técnica para ir directo a pedir marca/modelo. Ej: "Sí, los cigüeñales vienen balanceados de fábrica, listos para colocar. ¿Para qué vehículo lo necesitás?"
    Sobre cigüeñales: se entregan balanceados, con los tapones colocados y listos para instalar.

## ESTRATEGIA DE BÚSQUEDA
1. Si te falta marca, modelo o motor, PREGUNTÁ antes de buscar. Excepción: si el cliente dio suficiente info para buscar (al menos repuesto + marca), podés buscar y después refinar.
2. Usá `search_products_advanced` como PRIMERA opción. En "repuesto" poné el tipo de pieza. Completá marca, modelo y motor con lo que el cliente te haya dicho.
3. Si el cliente dice la cilindrada (ej: "1.9") pero no el código de motor, pasá "1.9" en el campo motor — el sistema lo expande automáticamente a los códigos posibles.
4. Si `search_products_advanced` no devuelve resultados, reintentá con `search_products` (texto libre) combinando repuesto + marca + motor. Incluí el código de motor si lo tenés.
5. Hacé al menos 3 intentos de búsqueda antes de declarar "no encontré". Ejemplo: primero "tapa cilindros kangoo 1.9 F8Q", luego "tapa culata kangoo diesel", luego "tapa kangoo". Solo después de 3 intentos sin resultado decís que no lo tenés.
6. SIEMPRE mostrá los productos con nombre EXACTO del catálogo, precio demostrativo y stock ficticio. No inventes ni muestres enlaces externos.
7. Si encontrás VARIANTES del MISMO producto que difieren en UNA sola característica (ej: con retenes / sin retenes, vertical / horizontal), NO las listes ambas. Preguntá primero: "¿Lo necesitás con retenes o sin retenes?" y mostrá solo la opción correcta cuando el cliente responda. Si las variantes difieren en más de una característica, sí podés listarlas todas.
   Pero si solo encontrás productos DISTINTOS al que pidió el cliente (ej: pidió junta tapa y encontraste juego de juntas completo, o pidió tapa de cilindros y encontraste juntas o bulones), NO los ofrezcas — informá que no lo encontraste en el catálogo online y preguntá si necesita otra cosa.
8. Si el cliente pide más de un producto, hacé búsquedas separadas.
9. NUNCA inventes productos, precios, ni códigos de motor. Solo mostrá lo que devuelve la búsqueda.
10. NUNCA ofrezcas productos que el cliente no pidió. Si pidió una junta tapa, mostrá solo juntas tapa. Juegos de juntas, kits, conjuntos, correas, o cualquier otro repuesto relacionado se ofrecen ÚNICAMENTE si el cliente los pide en forma explícita. Esto incluye NO mencionar que "también hay juegos de juntas" al final de la respuesta.

## ESTILO
- Hablás en español argentino, de forma clara y amigable.
- Sos técnico pero accesible — explicás sin jerga innecesaria.
- Respuestas concisas, no más de 2 párrafos cortos. Sin explicaciones técnicas a menos que el cliente las pida.
- Usás emojis con moderación (máximo 1-2 por mensaje).

## ESTILO DE COMUNICACIÓN
Si te preguntan con quién hablan, respondé que sos el asistente demostrativo de repuestos de MetaIA.

El tono es directo, sin adornos ni padding. Mensajes cortos. Nada de "lamentablemente", "por supuesto", "con gusto", ni frases de asistente genérico.

Ejemplos de tono correcto (few-shot):

**Saludo inicial:**
✓ "Buen dia!"
✗ "¡Hola! ¿En qué te puedo ayudar hoy?"

**No catalogado (búsqueda devuelve no_catalogado):**
✓ "No trabajamos ese block. Puede ser por pedido especial — consultá con el equipo."
✗ "Ese block está agotado en este momento."

**Sin stock (búsqueda devuelve agotado):**
✓ "Lo tengo en catálogo pero en este momento está sin stock. ¿Consulto disponibilidad a proveedor?"
✗ "Lamentablemente, ese producto no se encuentra disponible en este momento."

**Cotización con descuento:**
✓ "$32.162. Tenés 3 cuotas sin interés o un 15% de descuento en efectivo o transferencia bancaria."
✗ "El precio es $32.162. Contamos con opciones de financiación muy convenientes..."

**Envío:**
✓ "No, el envío es aparte. Mandame el código postal y te lo cotizo."
✗ "El costo de envío no está incluido en el precio. Para cotizarlo necesitaría su código postal."

**Límite de rubro:**
✓ "Trabajamos solo cigüeñales para autos y camionetas."
✗ "Ese tipo de producto está fuera de nuestro catálogo actual."

**Pedido de foto por dudas de compatibilidad:**
✓ "¿Me mandás una foto? Hay versiones con calentador vertical y horizontal, quiero asegurarme que sea la correcta."
✗ "Para verificar la compatibilidad, sería ideal si pudiera enviarnos una fotografía del componente."

**Datos técnicos para válvulas:**
✓ "Para las válvulas necesito el diámetro de vástago y el diámetro de cabeza."

**Interés comercial:**
✓ "Esta demo no procesa compras. Si querés una solución similar para tu negocio, podés contactar a MetaIA desde el botón de la página."

## FORMATO DE RESPUESTA
Cuando tu respuesta tenga más de 5-6 líneas, separá el contenido en bloques usando "---SPLIT---" en una línea aparte. La interfaz mostrará cada bloque por separado. Máximo 3 bloques. Ejemplo:

¡Hola! Tu Palio con pérdida de aceite por la tapa seguramente necesita una junta nueva. Busqué en el catálogo:
---SPLIT---
📦 Junta Tapa Fiat Palio 1.4 Fire - Amianto
ID demo: DEMO-0101 | $14.167 (demo) | Stock ficticio

📦 Junta Tapa Fiat Palio 1.4 Fire - Multilámina
ID demo: DEMO-0102 | $20.019 (demo) | Stock ficticio
---SPLIT---
¿Te interesa alguna? ¿Necesitás algo más para ese motor?

NO uses "---SPLIT---" si la respuesta es corta (1-5 líneas).

FORMATO DE PRODUCTOS: Usá siempre este formato exacto para listar productos, sin asteriscos ni guiones de markdown:

📦 [nombre EXACTO del producto tal como figura en el catálogo, sin modificar]
ID demo: DEMO-0000 | $00.000 (demo) | Stock ficticio

NUNCA uses asteriscos (*), guiones (-) ni corchetes para listar productos.

## REGLAS
- NUNCA inventés productos o precios. Solo mostrás lo que devuelve la búsqueda.
- El nombre del producto en tu respuesta debe ser IDÉNTICO al campo `name` que devolvió la herramienta. No lo toques.
- Si no encontrás el producto EXACTO que pide el cliente, NO ofrezcas productos alternativos o relacionados. Informá que no lo encontraste en el catálogo online, sugerí que contacte al equipo para verificar disponibilidad, y preguntá si necesita buscar algún otro repuesto. Ejemplo: "No encontré la tapa de cilindros para tu Focus 2.0 Duratec en el catálogo online. Te sugiero consultarlo directamente con nuestro equipo. ¿Necesitás buscar algún otro repuesto?"
- Si no encontrás resultados, sugerí reformular la búsqueda o contactar directamente.
- Si el visitante quiere comprar, aclará que la demo no realiza ventas ni captura datos personales y señalá el CTA de MetaIA.
- Cerrá cada respuesta con una pregunta que mantenga la conversación: "¿Te interesa alguna?", "¿Necesitás algo más para ese motor?", "¿Querés que busque otra cosa?". Tu objetivo es ayudar al máximo antes de derivar.
- Si la tienda está CERRADA: informalo con naturalidad, pero NUNCA cortés la conversación. Siempre preguntá qué necesita el cliente ("¿En qué te puedo ayudar? Así cuando abramos ya tenemos tu consulta lista"). Seguí tomando el pedido normalmente — buscá en el catálogo, respondé dudas técnicas — para que el equipo pueda ver la consulta completa cuando estén disponibles.

## ESTILO DE COMUNICACIÓN — EJEMPLOS REALES

El siguiente tono es el que usás en todas tus respuestas. Aprendelo de estos intercambios reales:

### Cotización simple con stock disponible
CLIENTE: "Quería saber si tenés tapa de cilindro para fiat motor tipo 1.4 y en qué precio"
VOS:
la tapa nueva 0km pelada te sale $185.960
---SPLIT---
Tenés 3 cuotas sin interés o un 15% de descuento en efectivo o transferencia bancaria.

### Cotización cuando necesitás más datos
CLIENTE: "Precio de tapa cilindros renault 1.9"
VOS:
Comentame que modelo y año es el vehículo

### Cotización cuando hay variante técnica a confirmar
NO preguntes proactivamente por la posición del calentador (vertical/horizontal).
El sistema te lo indica cuando corresponde mediante `needs_clarification = "calentador"`.
Si la búsqueda devuelve la tapa directamente sin clarificación, mostrala — no inventes la pregunta.

### Producto no encontrado exacto — NO ofrecer relacionados, nunca
CLIENTE: "junta tapa Fiat Duna 1.7"
[Búsqueda devuelve solo juego de juntas completo, no junta tapa sola]
VOS:
No encontré la junta de tapa sola para la Duna 1.7 en el catálogo online.
Te recomiendo consultarlo directamente con el equipo. ¿Necesitás buscar algún otro repuesto?

INCORRECTO ❌ (nunca hacer esto): "No encontré la junta de tapa sola. Tenemos el juego de juntas completo, ¿te sirve?"

### Junta tapa con variantes de muescas (1M / 2M / 3M / 4M / 5M)
IMPORTANTE: "Junta Tapa De Cilindros" es exactamente lo que el cliente llama "junta tapa". Son el mismo producto. Si buscás "junta tapa" y encontrás "Junta Tapa De Cilindros", ESE ES EL PRODUCTO CORRECTO — no digas que no lo encontraste.

Las variantes 1M / 2M / 3M / 4M / 5M indican la cantidad de muescas según el sobresaliente del pistón. Son productos distintos. Cuando aparecen, preguntá cuántas muescas tiene la junta usada.

CLIENTE: "junta tapa kangoo 1.9"
[Búsqueda devuelve: Junta Tapa De Cilindros Renault Kangoo 1.9 8v F8Q - 1M, 2M, 3M, 4M, 5M, todas instock]
VOS:
Tengo junta de tapa para Kangoo 1.9 F8Q, $39.571 c/u.
Vienen en 5 medidas (1M a 5M) según el sobresaliente del pistón. ¿Cuántas muescas tiene la junta que sacaste?

### Variantes con una sola diferencia — preguntar antes de listar
CLIENTE: "sí, el juego"
[Búsqueda devuelve: juego con retenes y juego sin retenes]
VOS: ¿Lo necesitás con retenes o sin retenes?

### Sin stock — directo, sin rodeos
CLIENTE: "Block de renault megane II 1.5 dci tendrás?"
VOS: Buen dia! no, ese block no lo tenemos en stock

CLIENTE: "Tapa de cilindro para Chevrolet Astra 2.0"
VOS:
Por el momento esa tapa no la tenemos

### Fuera del rubro — claro y sin drama
CLIENTE: "Pata de motor de Ford Kuga 1.6 ecoboost"
VOS: No tenemos ese tipo de producto, nos dedicamos a partes de motor.

CLIENTE: "Cigüeñal para motor Volvo d11 Renault 440"
VOS: Trabajamos solo cigüeñales para autos y camionetas

### Pregunta sobre origen del producto
CLIENTE: "¿El cigüeñal de Renault Master es de origen brasilero o chino?"
VOS:
Son importados de China

### Envío — precio aparte, pedir datos
CLIENTE: "¿Eso incluye envío? Soy de Tartagal, Salta"
VOS:
No, el envío es aparte
Envianos el código postal y te lo cotizamos

CLIENTE: "¿Hacés envíos? ¿Es gratis?"
VOS:
No, el envío tiene un costo
Envianos la dirección y te lo cotizamos

### Cuando hay varias preguntas acumuladas — responder todo junto
CLIENTE: "¿Tienen cojinetes también? ¿El cigüeñal viene balanceado? ¿Tienen plastigage?"
VOS:
Te podemos ofrecer Correo Argentino a domicilio o Vía Cargo a sucursal. Te voy a pedir también provincia y localidad
No, no tenemos plastigage
El cigüeñal ya viene listo para colocar

### Qué significa "pelada" — solo si el cliente pregunta
CLIENTE: "No entiendo lo de pelada"
VOS:
Que no trae árbol de levas, válvulas, etc
Generalmente las tapas nuevas no vienen armadas

### Confirmar antes de cotizar lista larga
CLIENTE: [pidió 5 ítems incluyendo válvulas]
VOS: Nos había pedido juego de válvulas también ¿Es correcto?

### Corregir un error propio sin drama
VOS: Disculpame, te habían cotizado el juego de juntas de descarbonización
VOS: Ya te cotizo el juego de juntas completo

### Cierre cálido cuando el cliente va a pasar
CLIENTE: "Perfecto, en un rato paso por el local"
VOS: De nada, te esperamos

### Paro o consulta de horario excepcional
CLIENTE: "El lunes si hacen el paro, ¿van a abrir igual?"
VOS:
Abrimos con normalidad

### Clientes revendedores — derivar sin cotizar precio especial
CLIENTE: "¿Mejor número cash para reventa?"
VOS: Los precios son ficticios y esta demo no ofrece condiciones de reventa. Si buscás implementar una solución similar, usá el CTA de MetaIA.

### Presupuesto vs factura — el 15% se aplica al facturar
CLIENTE: "En el detalle no figura el descuento del 15%"
VOS:
No es una factura, es un presupuesto
No te preocupes que al momento de facturar te realizamos el 15%

---

## REGLAS DE TONO DERIVADAS DE LOS CHATS REALES

- **Saludo SOLO en el primer mensaje** de la conversación: "Buenas tardes" / "Buen día" / "Buenas noches" según corresponda, en mensaje separado con `---SPLIT---`. En las respuestas siguientes (incluso en deliveries de productos) NO repetís el saludo — el cliente ya está adentro de la charla y el saludo en cada turno suena robótico. Arrancá directo con la respuesta o con un transition corto ("Perfecto.", "Listo.", "Acá tenés:").
- **Sin "lamentablemente"**, sin "lo siento", sin padding emocional cuando no hay stock. Directo y seco es correcto.
- **Tuteo siempre**, incluso con clientes formales.
- **Precios con punto de miles estilo AR**: $185.960 (no $185960 ni $185,960).
- **"0km pelada"** es la descripción estándar de las tapas nuevas. Usala siempre.
- **La frase de promociones es fija**: "Tenés 3 cuotas sin interés o un 15% de descuento en efectivo o transferencia bancaria." — no la modifiques.
- **No cotices precio de reventa** bajo ningún concepto. Si el cliente menciona reventa, derivá al equipo humano.
- **El envío siempre tiene costo** — nunca asumir que es gratis. Si preguntan, aclararlo y pedir CP + localidad + provincia.
- **El cigüeñal viene listo para colocar** (balanceado) — es dato confirmado del negocio.
- **Para válvulas necesitás**: diámetro de vástago + diámetro de cabeza.
- **Para juntas de tapa necesitás**: cantidad de muescas (relacionada con la sobresaliente del pistón).
- **"Pelada" = sin árbol de levas ni válvulas**. Explicarlo solo si el cliente pregunta.
- **Cuando son muchos ítems**, confirmá la lista antes de cotizar todo.
- **Fotos de compatibilidad**: pedirlas cuando el motor es poco común o cuando hay riesgo de variante (ej: Suzuki Super Carry, tapas con calentadores en distintas posiciones).

## MODO PORTFOLIO — PRIORIDAD ABSOLUTA
Las reglas de esta sección prevalecen sobre cualquier ejemplo histórico anterior:
- Los precios y el stock siempre se describen como demostrativos o ficticios.
- No ofrezcas descuentos, cuotas, envíos, facturación, reventa, reservas ni compras.
- No pidas fotos ni datos personales o de ubicación.
- No digas que existe un local, proveedor o equipo de ventas de repuestos.
- No compartas teléfonos, emails, domicilios, enlaces de producto ni identificadores que no empiecen con DEMO-.
- Ante intención de compra, aclará el alcance ficticio y señalá solamente el CTA de MetaIA de la interfaz.

---

## PREGUNTAS META SOBRE EL CATÁLOGO

Cuando el cliente pregunta qué modelos, marcas o cilindradas cubre la tienda en general
(ej: "qué modelos de Peugeot tenés?", "qué modelos cubren?", "qué marcas trabajan?",
"tenés algo para Ford?"), NO es una búsqueda de producto — es una consulta sobre el
catálogo en sí.

Reglas:
- NUNCA llames a `search_products_advanced` o `search_products` para responder estas preguntas.
- NUNCA reutilices el `modelo` de un turno anterior como si el cliente hubiera preguntado por
  ese mismo modelo. "Qué modelos tenés?" NO significa "mostrame productos del último modelo
  que mencioné".
- Para preguntas sobre modelos de una marca usá `list_available_models` con la marca que
  el cliente mencione (o la marca activa en el contexto si solo dijo "qué modelos tenés?").
- Si la tool responde `status=ok`, listá los modelos al cliente y preguntále cuál tiene.
  Ejemplo: "En Peugeot tenemos repuestos para: 206, 207, 208, 307, 408, 504, 505, Partner,
  Boxer. ¿Cuál es el tuyo?"
- Si responde `status=no_data`, preguntá directamente al cliente qué modelo tiene.
- Para preguntas de marcas usá la lista de MARCAS QUE TRABAJAN de este mismo prompt.

## CLARIFICACIÓN POR MEDIDA

Si `search_products_advanced` retorna `needs_clarification = "medida"`:
- **NO muestres productos ni precios todavía.**
- Preguntá al usuario qué medida necesita, listando las opciones de `available_measures`.
  Ejemplo: "¿En qué medida lo necesitás? Tenemos disponible: STD, 0.25, 0.50"
- Cuando el usuario elija, llamá `search_products_advanced` nuevamente con el mismo query original
  (mismo repuesto, marca, modelo, motor, etc.) **más** el parámetro `medida` con la elección del usuario.
- Recién entonces mostrá los productos con nombre, precio, stock y link.

## CLARIFICACIÓN POR VÁLVULAS

Si `search_products_advanced` retorna `needs_clarification = "valvulas"`:
- **NO muestres productos ni precios todavía.**
- Preguntá cuántas válvulas tiene el motor del cliente, listando las opciones de `available_valvulas`.
  Ejemplo: "¿Tu motor es 8 válvulas o 16 válvulas?"
- Cuando el cliente elija, llamá `search_products_advanced` nuevamente con el query original
  **más** el parámetro `valvulas` ('8v' o '16v') con la elección del cliente.
- Recién entonces mostrá los productos.

## CLARIFICACIÓN POR RETENES

Si `search_products_advanced` retorna `needs_clarification = "retenes"`:
- **NO muestres productos ni precios todavía.**
- Preguntá si lo necesita con o sin retenes, según las opciones de `available_retenes`.
  Ejemplo: "¿La necesitás con retenes o sin retenes?"
- Cuando el cliente elija, llamá `search_products_advanced` nuevamente con el query original
  **más** el parámetro `retenes` ('con' o 'sin') con la elección del cliente.
- Recién entonces mostrá los productos.

## CLARIFICACIÓN POR CALENTADOR (vertical / horizontal)

Si `search_products_advanced` retorna `needs_clarification = "calentador"`:
- **NO muestres productos ni precios todavía.** Aplica casi siempre a tapas de cilindro Diesel.
- Preguntá si los calentadores de la tapa son verticales u horizontales, según `available_calentador`.
  Ejemplo: "¿Sabrías decirme si los calentadores de tu tapa están de forma vertical u horizontal?"
- Cuando el cliente elija, llamá `search_products_advanced` nuevamente con el query original
  **más** el parámetro `calentador` ('vertical' u 'horizontal') con la elección del cliente.
- Recién entonces mostrá los productos.

## CLARIFICACIÓN POR CÓDIGO DE MOTOR (TU5JP4 / THP EP6DT / K4M / K9K / etc.)

Si `search_products_advanced` retorna `needs_clarification = "motor"`:
- **NO muestres productos ni precios todavía.** El catálogo tiene dos o más motores distintos
  para la misma cilindrada + válvulas + combustible del cliente, y los repuestos son piezas
  físicamente diferentes (no se pueden intercambiar).
- El dict trae: `available_motor_codes` (lista), `cilindrada` y `valvulas` para que armes la pregunta.
- Preguntá al cliente cuál motor tiene, usando las opciones de `available_motor_codes`. Si reconocés
  pistas técnicas (aspirado vs turbo, MPI vs HDI, etc.) agregalas para ayudarlo. Ejemplos:
  · "Para Peugeot 207 1.6 16v hay dos motores nafta distintos: TU5JP4 (aspirado) o THP EP6DT (turbo).
     ¿Cuál tenés? Si no estás seguro, podés mirar la chapa del motor o mandarme una foto."
  · "Para Renault Megane 1.6 16v hay dos motores: K4M (aspirado naftero clásico) y otro. ¿Cuál tenés?"
- Cuando el cliente responda, llamá `search_products_advanced` nuevamente con el query original
  **más** el parámetro `motor_code` con el código exacto (ej: 'TU5JP4', 'THP EP6DT'). No incluyas
  cilindrada ni válvulas en `motor_code` — el código solo.
- Recién entonces mostrá los productos.

## ORDEN DE CLARIFICACIONES

Si varias clarificaciones aplican al mismo turno, el código te va a devolver **una sola**
dimensión por vez, en este orden de prioridad:
válvulas → retenes → calentador → motor → medida. Resolvé la que te llegue, no preguntes varias juntas.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Busca repuestos en el catálogo demostrativo. "
                "Usá esta herramienta cuando el cliente pregunte por un repuesto, "
                "pieza, precio o disponibilidad."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Texto de búsqueda. IMPORTANTE: Extraé solo palabras clave del pedido del cliente "
                            "(tipo de repuesto + marca vehículo + modelo). NO copies la frase completa del usuario. "
                            "Máximo 4-5 palabras. Ejemplo: el cliente dice 'necesito una junta de tapa de cilindros "
                            "para mi Volkswagen Gol 2015 1.6L' → buscá 'junta tapa cilindros volkswagen gol'. "
                            "Si no hay resultados, reintentá con menos palabras (ej: 'junta tapa gol')."
                        ),
                    },
                    "sku": {
                        "type": "string",
                        "description": "SKU exacto del producto si el cliente lo proporciona",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products_advanced",
            "description": (
                "Búsqueda avanzada de repuestos filtrando por atributos del vehículo. "
                "USAR ESTA HERRAMIENTA COMO PRIMERA OPCIÓN cuando el cliente menciona marca, modelo o motor. "
                "Si no devuelve resultados, reintentá con search_products (texto libre)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repuesto": {
                        "type": "string",
                        "description": (
                            "Tipo de repuesto a buscar (ej: 'tapa cilindros', 'junta tapa', 'cigueñal'). "
                            "Solo el nombre del repuesto, SIN marca ni modelo."
                        ),
                    },
                    "marca_vehiculo": {
                        "type": "string",
                        "description": "Marca del vehículo (ej: 'Renault', 'Ford', 'Volkswagen'). Opcional.",
                    },
                    "modelo": {
                        "type": "string",
                        "description": "Modelo del vehículo (ej: 'Kangoo', 'Gol', '206'). Opcional.",
                    },
                    "motor": {
                        "type": "string",
                        "description": (
                            "Motor o cilindrada del vehículo. Pasá SIEMPRE lo que mencione el cliente, "
                            "aunque sea solo la cilindrada (ej: '1.9', '1.6', '1.4'). "
                            "Si conocés el código completo, mejor (ej: '1.9 8v F8Q', '1.4 HDI', '1.6 16v'). Opcional."
                        ),
                    },
                    "combustible": {
                        "type": "string",
                        "description": "Combustible del vehículo: 'nafta' o 'diesel'. Pasarlo cuando el cliente lo haya confirmado. Opcional.",
                    },
                    "valvulas": {
                        "type": "string",
                        "description": "Cantidad de válvulas: '8v' o '16v'. Pasarlo cuando el cliente lo haya confirmado. Opcional.",
                    },
                    "medida": {
                        "type": "string",
                        "description": (
                            "Medida del repuesto, solo cuando el cliente la haya especificado explícitamente. "
                            "Formatos aceptados: 'STD', '0.25', '0.50', '1M', '2M', '3M', 'Esp. 1.68mm', 'BA0.30', 'BI0.50'. "
                            "Usarlo únicamente en la segunda llamada, tras haber preguntado la medida al cliente. Opcional."
                        ),
                    },
                    "retenes": {
                        "type": "string",
                        "description": (
                            "Si el repuesto viene con o sin retenes. Solo cuando el cliente lo haya elegido. "
                            "Valores: 'con' o 'sin'. Usarlo en la segunda llamada tras la clarificación. Opcional."
                        ),
                    },
                    "calentador": {
                        "type": "string",
                        "description": (
                            "Posición del calentador (la bujía incandescente) en tapas de cilindros Diesel. "
                            "Solo cuando el cliente lo haya elegido. Valores: 'vertical' u 'horizontal'. "
                            "Usarlo en la segunda llamada tras la clarificación. Opcional."
                        ),
                    },
                    "motor_code": {
                        "type": "string",
                        "description": (
                            "Código específico del motor para desambiguar cuando coexisten dos o más motores "
                            "con misma cilindrada y misma cantidad de válvulas (ej: 'TU5JP4' vs 'THP EP6DT' "
                            "para Peugeot 1.6 16v; 'K4M' vs 'K9K' para Renault 1.5/1.6 16v). "
                            "Usarlo únicamente en la segunda llamada, tras haber preguntado el código al cliente. "
                            "Pasar solo el código (ej: 'TU5JP4', 'THP EP6DT'), no la cilindrada/válvulas. Opcional."
                        ),
                    },
                },
                "required": ["repuesto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": (
                "Obtiene detalles completos de un producto específico por su ID. "
                "Usalo después de una búsqueda para dar más información."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Identificador anónimo del producto (formato DEMO-####)",
                    },
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_available_models",
            "description": (
                "Lista los modelos de vehículo cubiertos por el catálogo demostrativo "
                "para una marca dada. Usar SOLO cuando el visitante pregunta qué modelos están cubiertos "
                "(ej: 'qué modelos de Peugeot tenés?', 'qué modelos trabajan?'). "
                "NO usar esto para buscar productos — para eso usá search_products_advanced. "
                "Esta herramienta no devuelve productos, solo nombres de modelos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "marca_vehiculo": {
                        "type": "string",
                        "description": "Marca del vehículo (ej: 'Peugeot', 'Renault', 'Volkswagen').",
                    },
                },
                "required": ["marca_vehiculo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Orienta a la persona al CTA comercial de MetaIA. "
                "Usalo cuando quiera conocer o implementar una solución similar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo de la derivación",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


_AR_TZ = timezone(timedelta(hours=-3))
_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _get_business_status() -> str:
    """Retorna el estado actual de la tienda (abierta/cerrada) con hora Argentina."""
    now = datetime.now(_AR_TZ)
    weekday = now.weekday()  # 0=lunes, 6=domingo
    t = now.hour + now.minute / 60  # hora decimal

    if weekday >= 5:
        is_open = False
    elif 8.5 <= t < 13.0 or 14.0 <= t < 17.0:
        is_open = True
    else:
        is_open = False

    day = _DAYS_ES[weekday]
    time_str = now.strftime("%H:%M")
    status = "ABIERTA" if is_open else "CERRADA"
    return f"[HORA ACTUAL: {day} {time_str} hs (Argentina) — TIENDA {status}]"


def _get_greeting() -> str:
    """Saludo de apertura según la hora de Argentina.

    Franjas:
      - 00:00–11:59 → "Buen día"
      - 12:00–19:59 → "Buenas tardes"
      - 20:00–23:59 → "Buenas noches"

    Se usa SOLO en el primer mensaje de una conversación; ver
    process_message() / _apply_first_turn_greeting().
    """
    hour = datetime.now(_AR_TZ).hour
    if hour < 12:
        return "Buen día"
    if hour < 20:
        return "Buenas tardes"
    return "Buenas noches"


def _apply_first_turn_greeting(response: str, is_first_turn: bool) -> str:
    """Antepone el saludo + ---SPLIT--- a la respuesta si es el primer turno.

    No saluda si:
      - No es el primer turno.
      - La respuesta ya viene con un saludo (defensa por si el LLM lo agregó).
    """
    if not is_first_turn:
        return response
    if not response:
        return response
    head = response.lstrip().lower()[:30]
    if head.startswith(("buen día", "buen dia", "buenas tardes", "buenas noches", "hola")):
        return response
    return f"{_get_greeting()}\n---SPLIT---\n{response}"


# ---------------------------------------------------------------------------
# Máquina de estados para recolección de datos del vehículo
# ---------------------------------------------------------------------------

# Códigos de motor que por sí solos implican combustible Y válvulas — no hace falta preguntar
_COMPLETE_MOTOR_CODES: frozenset[str] = frozenset({
    "FIRE", "FIREFLY", "ZETEC", "DURATEC", "ECOBOOST", "SIGMA", "DRAGON", "TORQUE",
    "F8Q", "F9Q", "F3R", "F3P", "F4P", "F4R",
    "K9K", "K4M", "K7M", "G9U", "H4M",
    "XUD", "XU5", "XU7", "XU9", "XU10",
    "DCI", "TDI", "HDI", "TDCI", "CDI", "SDI", "JTD",
    "DW8", "DW10", "DV4", "DV6",
    "4EE1", "4JA1", "4JB1", "4M40",
    "HR16", "MR18",
})

_REQUIRED_FIELDS = ["repuesto", "marca", "modelo", "motor"]

_FIELD_QUESTIONS = {
    "repuesto": "¿Qué repuesto necesitás?",
    "marca": "¿Para qué vehículo? Decime la marca.",
    "modelo": "¿Qué modelo?",
    "motor": "¿Qué cilindrada tiene? (ej: 1.4, 1.6, 1.9, 2.0)",
    "combustible": "¿Es nafta o diesel?",
    "valvulas": "¿8 o 16 válvulas?",
}

EXTRACTION_PROMPT = """\
Extraé datos de vehículo y repuesto del mensaje. Devolvé SOLO JSON válido, sin texto extra.
Campos posibles: "repuesto", "marca", "modelo", "motor", "combustible", "valvulas", "es_consulta", "pregunta_tecnica".
- "repuesto": tipo de pieza (ej: "tapa cilindros", "junta tapa", "cigüeñal", "aros piston")
- "marca": marca del vehículo (ej: "Renault", "Fiat", "Ford", "Peugeot", "Chevrolet", "VW")
- "modelo": modelo del vehículo (ej: "Kangoo", "Palio", "Focus", "206", "Gol")
- "motor": SOLO la cilindrada y/o código de motor (ej: "1.9", "1.4 Fire", "2.0 Duratec", "1.9 F8Q")
- "combustible": solo si se menciona explícitamente ("nafta" o "diesel")
- "valvulas": solo si se menciona explícitamente ("8v" o "16v")
- "es_consulta": true si es consulta de repuesto/vehículo, false si es saludo/horario/otro tema
- "pregunta_tecnica": si el mensaje contiene una pregunta técnica sobre el producto (balanceo, medidas, retenes, material, compatibilidad, si es original, etc.), capturala textualmente. Omití si no hay pregunta técnica.
Omití campos no mencionados (no pongas null ni "").
Ejemplos:
- "tapa de cilindros kangoo 1.9" → {{"repuesto": "tapa cilindros", "marca": "Renault", "modelo": "Kangoo", "motor": "1.9", "es_consulta": true}}
- "junta tapa" → {{"repuesto": "junta tapa", "es_consulta": true}}
- "1.9 diesel" → {{"motor": "1.9", "combustible": "diesel", "es_consulta": true}}
- "1.9 diesel 8v" → {{"motor": "1.9", "combustible": "diesel", "valvulas": "8v", "es_consulta": true}}
- "diesel" → {{"combustible": "diesel", "es_consulta": true}}
- "nafta" → {{"combustible": "nafta", "es_consulta": true}}
- "8v" → {{"valvulas": "8v", "es_consulta": true}}
- "16v" → {{"valvulas": "16v", "es_consulta": true}}
- "1.4 Fire" → {{"motor": "1.4 Fire", "es_consulta": true}}
- "cigüeñal, viene balanceado?" → {{"repuesto": "cigüeñal", "es_consulta": true, "pregunta_tecnica": "viene balanceado?"}}
- "sin retenes" → {{"es_consulta": true}}
- "buenas!" → {{"es_consulta": false}}
Mensaje: "{user_text}"
"""


def _motor_has_complete_info(motor: str, combustible: str, valvulas: str) -> tuple[bool, bool]:
    """Retorna (tiene_combustible, tiene_valvulas) para el contexto dado.

    Un código de motor conocido (Fire, F8Q, K9K, etc.) implica ambos datos.
    """
    combined = (motor + " " + combustible + " " + valvulas).upper()

    if any(code in combined for code in _COMPLETE_MOTOR_CODES):
        return True, True

    tiene_combustible = bool(combustible) or any(
        k in combined for k in ["DIESEL", "NAFTA", "NAFTERO", "GASOLINA"]
    )
    tiene_valvulas = bool(valvulas) or "8V" in combined or "16V" in combined

    return tiene_combustible, tiene_valvulas


def _motor_has_known_code(motor: str) -> bool:
    """True si el string de motor contiene un código identificador único.

    Los códigos en _KNOWN_MOTOR_CODES (F8Q, F9Q, K9K, CHT, K4M, Fire, AP, etc.)
    identifican unívocamente un motor. Cuando el cliente da uno de estos,
    el modelo del auto deja de ser necesario para encontrar la pieza —
    el código de motor es identificación más específica.
    """
    if not motor:
        return False
    motor_lower = motor.strip().lower()
    return any(code in motor_lower for code in _KNOWN_MOTOR_CODES)


def _next_missing_field(context: dict) -> str | None:
    motor_str = context.get("motor", "") or ""
    motor_has_code = _motor_has_known_code(motor_str)

    for field in _REQUIRED_FIELDS:
        # Si motor ya contiene un código identificador único (ej: F9Q),
        # saltear el requerimiento de "modelo": el código identifica la
        # pieza con más precisión que el modelo del auto.
        if field == "modelo" and motor_has_code:
            continue

        if field == "combustible":
            tc, _ = _motor_has_complete_info(
                context.get("motor", ""),
                context.get("combustible", ""),
                context.get("valvulas", ""),
            )
            if not tc:
                return "combustible"
        elif field == "valvulas":
            _, tv = _motor_has_complete_info(
                context.get("motor", ""),
                context.get("combustible", ""),
                context.get("valvulas", ""),
            )
            if not tv:
                return "valvulas"
        else:
            if not context.get(field):
                return field
    return None


def _question_for_field(field: str) -> str:
    return _FIELD_QUESTIONS.get(field, "¿Podés darme más datos del vehículo?")


# Patrones que indican que la última respuesta del LLM le pidió un dato extra al
# cliente (válvulas, medida, retenes, muescas, etc.) en vez de cerrar con
# cotización o info final. Cuando esto ocurre, process_message conserva
# state=READY con el contexto del vehículo para que el próximo turno arranque
# con toda la info ya recolectada en lugar de IDLE+{}.
#
# Caso real (25-abr-2026, demo Vectra 2.0 junta de descarbonización):
#   Bot: "...8 válvulas o 16 válvulas, y con retenes o sin retenes."
#   Cliente: "8V"
#   → sin este preserve, el bot respondía "¿Qué repuesto necesitás?" porque el
#     reset de step 4 había borrado repuesto/marca/modelo/motor.
#
# Estrategia: regex sobre la última parte (después del último ---SPLIT---) en
# minúsculas. Si match → es una clarification → preservar context.
_CLARIFICATION_PATTERNS: tuple[str, ...] = (
    r"\b8\s*v[áa]?l?v?u?l?a?s?\s+o\s+16\s*v[áa]?l?v?u?l?a?s?\b",  # "8v o 16v" / "8 válvulas o 16 válvulas"
    r"\bcon\s+(o\s+sin\s+)?reten[ée]s\b",                          # "con retenes" / "con o sin retenes"
    r"\bsin\s+reten[ée]s\b",                                       # "sin retenes"
    r"\bqu[ée]\s+medida\b",                                        # "qué medida"
    r"\ben\s+qu[ée]\s+medida\b",                                   # "en qué medida"
    r"\bcu[áa]ntas\s+muescas\b",                                   # "cuántas muescas"
    r"\bqu[ée]\s+muescas\b",                                       # "qué muescas"
    r"\bSTD\s*[,/]\s*0\.",                                         # "STD, 0.25" / "STD/0.50"
    # Calentador (vertical u horizontal). Caso testigo: Kangoo 1.9 F8Q chat real
    # 14:18-14:19. Sin estos patrones, "horizontal" del cliente despues caia al
    # loop "que repuesto?" porque el state preservation no detectaba la
    # clarificacion previa del bot.
    r"\bcalentador(?:es)?\b.*\b(?:vertical|horizontal)\b",        # "calentadores...vertical u horizontal"
    r"\b(?:vertical|horizontal)\s+[uo]\s+(?:vertical|horizontal)\b",  # "vertical u horizontal" / "horizontal o vertical"
    # Combustible (nafta vs diesel) — el bot a veces lo pregunta explicitamente
    r"\bnafta\s+[uo]\s+diesel\b",                                  # "nafta o diesel"
    # Chaveta postiza/incorporada y perno (arbol de levas) - clarificacion comun.
    # "postiza" e "incorporada" son palabras muy especificas del dominio (solo
    # aparecen en preguntas sobre arbol de levas), asi que alcanza con detectarlas
    # sueltas — es mas robusto que requerir adyacencia con "chaveta".
    r"\b(?:postiza|incorporada)\b",                                 # chaveta postiza/incorporada
    r"\bcon\s+perno\s+[uo]\s+sin\s+perno\b",                     # "con perno o sin perno"
    # Lado del rectificado (BA bancada / BI biela)
    r"\b(?:BA|BI)\s*0?\.\d",                                       # "BA0.30" / "BI 0.50"
    # Diametro de guia (mm) - tapas
    r"\bgu[ií]a\s+(?:de\s+)?\d+\s*mm\b",                        # "guia 7mm"
    r"\b\d+mm\s+[uo]\s+\d+mm\b",                                 # "7mm u 8mm"
)


def _looks_like_clarification(response: str) -> bool:
    """Heurística: ¿el LLM cerró pidiendo un dato más al cliente?

    Mira la última parte del response (después del último ---SPLIT---) y busca
    patrones típicos de pregunta clarificadora sobre dimensiones técnicas que
    no se recolectan en el state machine (válvulas, medida, retenes, muescas).
    Detección por keyword/regex en lugar de "termina con ?" porque el bot
    también cierra respuestas finales con "¿Necesitás algo más?", que NO
    debería preservar contexto (es un cierre de flujo, no una clarification).
    """
    if not response:
        return False
    last_part = response.rsplit("---SPLIT---", 1)[-1].lower()
    for pat in _CLARIFICATION_PATTERNS:
        if re.search(pat, last_part, flags=re.IGNORECASE):
            return True
    return False


# Patrones de saludo de apertura. Cuando el primer mensaje de un turno arranca
# con uno de estos, asumimos que el cliente está abriendo una conversación nueva
# y wipeamos cualquier context preservado de turnos anteriores. Esto contrabalancea
# la decisión de preservar marca/modelo/motor a través de deliveries (mejor UX
# para "ahora aros pistón" sin re-decir el auto), evitando que un cliente
# distinto que toma el WhatsApp después herede contexto stale.
_GREETING_PATTERNS: tuple[str, ...] = (
    r"^\s*hola\b",
    r"^\s*buenas?\b",                          # buenas / buenos
    r"^\s*buen[oa]s?\s+(d[ií]as?|tardes|noches)\b",  # buenos días / buenas tardes / etc.
    r"^\s*buen\s+d[ií]a\b",                    # buen día
    r"^\s*qu[eé]\s+tal\b",                     # qué tal
    r"^\s*hello\b",
    r"^\s*hi\b",
    r"^\s*hey\b",
    r"^\s*che\b",
)


def _looks_like_greeting(text: str) -> bool:
    """¿El mensaje arranca con un saludo de apertura?

    Solo matchea al INICIO del texto (anchor `^`), para que un mensaje como
    "necesito juntas, gracias" no dispare wipe por tener "gracias" suelto.
    No confundir con `_looks_like_clarification` (que mira el response del bot).
    """
    if not text:
        return False
    for pat in _GREETING_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False


class Agent:
    """Agente LLM con function calling para asesoramiento de repuestos."""

    def __init__(
        self,
        model: str,
        temperature: float,
        max_tokens: int,
        wc_client: WooCommerceClient,
        db: Database,
        escalation_phone: str,
        motor_expand_index: dict | None = None,
        brand_models_index: dict | None = None,
        model_brand_index: dict | None = None,
    ):
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._wc_client = wc_client
        self._db = db
        self._escalation_phone = escalation_phone
        # Índice dinámico {(marca, cilindrada): {combustible: [motores]}} construido
        # desde WooCommerce. Se consume en _expand_motor (Fase 4). Puede ser None si
        # todavía no se construyó — en ese caso se cae al _MOTOR_EXPAND hardcodeado.
        self._motor_expand_index = motor_expand_index or {}
        # Índice marca → [modelos] para la tool list_available_models. Construido
        # del mismo catálogo de WC; si está vacío, la tool responde que no hay info.
        self._brand_models_index = brand_models_index or {}
        # Índice inverso modelo → [marcas] para inferir la marca cuando el cliente
        # menciona solo un modelo. Se usa en process_message después de la extracción.
        # Si está vacío, la inferencia no dispara y el flujo normal pregunta la marca.
        self._model_brand_index = model_brand_index or {}

    def set_motor_expand_index(self, index: dict) -> None:
        """Reemplaza el índice motor_expand en runtime (usado por el refresh periódico)."""
        self._motor_expand_index = index or {}

    def set_brand_models_index(self, index: dict) -> None:
        """Reemplaza el índice brand_models en runtime (usado por el refresh periódico)."""
        self._brand_models_index = index or {}

    def set_model_brand_index(self, index: dict) -> None:
        """Reemplaza el índice model_brand en runtime (usado por el refresh periódico)."""
        self._model_brand_index = index or {}

    async def _extract_vehicle_data(self, user_text: str) -> dict:
        """Extrae entidades de vehículo/repuesto del mensaje con un LLM liviano (sin tools)."""
        logger.warning("USANDO NUEVO EXTRACTOR v2")
        prompt = EXTRACTION_PROMPT.replace("{user_text}", user_text.replace('"', "'"))

        # Gemini 2.5 Flash tiene "thinking" activado por default y consume tokens
        # del budget (max_tokens) en razonamiento interno antes del output. Con 300
        # tokens y thinking dinámico, el JSON se corta a 1-2 chars.
        # Fix: max_tokens=2000 da budget de sobra para thinking (~200-500 tokens
        # en tasks estructurales) + la salida JSON (~100-200 tokens). Probamos
        # con `thinking`/`reasoning_effort` pero versiones viejas de LiteLLM
        # levantan UnsupportedParamsError.
        try:
            response = await litellm.acompletion(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=2000,
            )
            original_raw = (response.choices[0].message.content or "{}")
            logger.warning(f"RAW ORIGINAL: {original_raw!r}")

            raw = original_raw.strip()
            raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"^```\s*", "", raw).strip()
            raw = re.sub(r"\s*```$", "", raw).strip()

            if not raw:
                return {}

            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                raw = match.group(0).strip()

            raw = re.sub(r",\s*}", "}", raw)

            logger.warning(f"RAW LIMPIO: {raw!r}")

            if "}" not in raw or len(raw) < 10:
                logger.warning(f"JSON sospechosamente incompleto, descarto: {raw!r}")
                return {}

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"json.loads falló, devuelvo {{}}. raw={raw!r}")
                return {}
        except Exception as e:
            logger.warning(f"_extract_vehicle_data falló para '{user_text}': {e}")
            return {}

    async def process_message(
        self, user_text: str, sender: str, conversation: dict
    ) -> str:
        """Procesa un mensaje del usuario usando máquina de estados para recolección de datos."""
        context = dict(conversation.get("context") or {})

        # Detección de primer turno: si no hay mensajes previos en DB, es el primer
        # mensaje de la conversación → corresponde anteponer un saludo ("Buen día" /
        # "Buenas tardes" / "Buenas noches") como mensaje aparte vía ---SPLIT---.
        # Se aplica en TODAS las ramas de retorno con _apply_first_turn_greeting().
        # Esto es independiente de _looks_like_greeting (que detecta si el USUARIO
        # saludó); acá detectamos si nosotros tenemos que saludar.
        try:
            _prev_msgs = await self._db.get_recent_messages(conversation["id"], limit=1)
            is_first_turn = len(_prev_msgs) == 0
        except Exception:
            is_first_turn = False

        # 0. Reset por saludo de apertura.
        # Después de un delivery (state=IDLE), el context conserva marca/modelo/motor
        # del último auto consultado para mejorar UX cuando el mismo cliente pide
        # otro repuesto para el mismo auto ("ahora aros pistón" sin re-decir el auto).
        # Pero si llega un saludo, asumimos que abre una conversación nueva (puede
        # ser otra persona que tomó el WhatsApp) y limpiamos todo el context preservado.
        # Solo aplica cuando NO había recolección activa (state IDLE/READY); si el
        # bot estaba mid-flow esperando un dato, no romper la cadena.
        db_state_pre = conversation.get("state", "IDLE")
        if context and _looks_like_greeting(user_text) and db_state_pre in ("IDLE", "READY"):
            logger.warning(
                f"[STATE] saludo detectado en mensaje nuevo → wipe de context preservado "
                f"(keys descartadas: {sorted(context.keys())})"
            )
            context = {}

        # 1. Extraer entidades del mensaje actual
        extracted = await self._extract_vehicle_data(user_text)

        # 1b. Inferencia determinística modelo → marca.
        # El extractor solo captura lo que está literal en el texto ("no inventar"),
        # así que si el cliente dijo "Fox" sin decir "VW", el campo marca queda vacío
        # y el state machine caería a COLLECTING_MARCA pese a que Fox es unívocamente
        # Volkswagen en el catálogo. Acá resolvemos ese caso usando el índice inverso
        # construido en bootstrap desde WooCommerce. Solo completa marca cuando el
        # modelo es unívoco (1 sola marca en el catálogo); modelos ambiguos
        # (ej: un "500" que exista en varias marcas) quedan sin marca y el flujo
        # normal pregunta al cliente — principio "no inventar, desambiguar".
        if extracted.get("modelo") and not extracted.get("marca"):
            inferred_marca = lookup_marca_by_modelo(
                self._model_brand_index, extracted["modelo"]
            )
            if inferred_marca:
                logger.warning(
                    f"[MODELO→MARCA] modelo='{extracted['modelo']}' → marca='{inferred_marca}'"
                )
                extracted["marca"] = inferred_marca

        is_vehicle_query = extracted.get("es_consulta", True)
        pregunta_tecnica = extracted.get("pregunta_tecnica") or None
        has_extracted_data = any(v for k, v in extracted.items() if k not in ("es_consulta", "pregunta_tecnica") and v)

        # Snapshot del contexto ANTES del merge - distingue "datos preservados
        # de un delivery anterior" (post-delivery loop) de "datos del mensaje
        # actual". Se consume en step 3 para detectar el patron post-delivery.
        preserved_context = dict(context)

        # Si no hay contexto previo, no se extrajo ningún dato Y no hay recolección activa → LLM libre
        # Cubre: saludos, horarios, y mensajes donde la extracción falla silenciosamente.
        # Si la recolección estaba activa (state=COLLECTING_*), continuar con el state machine aunque
        # la extracción haya fallado — así no se pierde el contexto ya guardado.
        db_state = conversation.get("state", "IDLE")
        if not context and not has_extracted_data and not db_state.startswith("COLLECTING_"):
            response = await self._run_llm(user_text, {}, conversation)
            return _apply_first_turn_greeting(response, is_first_turn)

        # Bypass al LLM cuando el mensaje NO es consulta (cierres, derivacion,
        # chitchat) y NO estamos mid-flow recolectando. Sin esto, despues de
        # un delivery, el state machine pregunta "que repuesto necesitas?" en
        # bucle. Caso testigo: AUTO-0028 cliente dice "derivame con ellos".
        if (
            context
            and not is_vehicle_query
            and not has_extracted_data
            and not pregunta_tecnica
            and not db_state.startswith("COLLECTING_")
        ):
            logger.warning(
                f"[STATE] no-vehicle-query con context preservado (cierre/derivacion) "
                f"-> bypass al LLM (keys: {sorted(context.keys())})"
            )
            response = await self._run_llm(user_text, context, conversation)
            return _apply_first_turn_greeting(response, is_first_turn)

        # 2. Actualizar contexto

        # Si cambió la marca, invalidar todo el resto del context de vehículo
        # (modelo/motor/combustible/valvulas) porque son específicos del auto anterior.
        # También reseteamos db_state para que no dispare la anti-loop logic con datos
        # stale. Ej: venía hablando de Renault Clio 1.4 nafta y ahora dice "Peugeot 1.9"
        # → el modelo "Clio" y el "1.4 nafta" no aplican al Peugeot.
        extracted_marca = (extracted.get("marca") or "").strip().lower()
        ctx_marca = (context.get("marca") or "").strip().lower()
        if extracted_marca and ctx_marca and extracted_marca != ctx_marca:
            logger.warning(
                f"[STATE] cambio de marca '{ctx_marca}' → '{extracted_marca}': "
                f"descarto modelo/motor/combustible/valvulas stale"
            )
            for k in ("modelo", "motor", "combustible", "valvulas"):
                context.pop(k, None)
            db_state = "IDLE"

        if "repuesto" in extracted and extracted["repuesto"] and extracted["repuesto"] != context.get("repuesto"):
            # Cambio de repuesto: mantener vehículo pero actualizar repuesto
            vehicle_data = {k: v for k, v in context.items() if k in ("marca", "modelo", "motor") and v}
            context = vehicle_data
            context["repuesto"] = extracted["repuesto"]

        # Merge incremental — no sobreescribir con vacíos
        for k, v in extracted.items():
            if k not in ("es_consulta", "pregunta_tecnica") and v:
                context[k] = v

        # 3. Determinar siguiente campo faltante
        missing = _next_missing_field(context)

        # Caso especial: post-delivery loop. Si venimos de un delivery anterior
        # (state=IDLE/READY con marca/modelo/motor preservados) y solo falta
        # `repuesto` - porque el delivery limpio ese campo - NO loopear con
        # "que repuesto necesitas?". El LLM puede ver el historial y decidir.
        # Caso testigo: AUTO-0028 turnos 4/6.
        post_delivery_loop = (
            missing == "repuesto"
            and db_state in ("IDLE", "READY")
            and bool(preserved_context.get("marca"))
        )

        if missing and not post_delivery_loop:
            new_state = f"COLLECTING_{missing.upper()}"
            # Si ya estábamos esperando este mismo campo y la extracción no pudo avanzar
            # (el usuario respondió algo pero no obtuvimos el campo que necesitamos),
            # delegar al LLM para que maneje la situación de forma natural en vez de
            # repetir la misma pregunta mecánicamente → previene loops infinitos.
            # También delegar si el usuario hizo una pregunta técnica: el LLM la responde
            # y luego pide los datos que faltan.
            if db_state == new_state or pregunta_tecnica:
                await self._db.update_conversation_state(conversation["id"], new_state, context)
                response = await self._run_llm(user_text, context, conversation, pregunta_tecnica=pregunta_tecnica)
                return _apply_first_turn_greeting(response, is_first_turn)
            await self._db.update_conversation_state(conversation["id"], new_state, context)
            return _apply_first_turn_greeting(_question_for_field(missing), is_first_turn)

        if post_delivery_loop:
            logger.warning(
                f"[STATE] post-delivery loop: falta repuesto pero hay vehiculo "
                f"preservado (keys: {sorted(preserved_context.keys())}) -> "
                f"bypass al LLM con historial"
            )

        # 4. Todos los datos presentes → ejecutar LLM con contexto inyectado
        await self._db.update_conversation_state(conversation["id"], "READY", context)
        response = await self._run_llm(
            user_text, context, conversation,
            pregunta_tecnica=pregunta_tecnica,
            post_delivery_followup=post_delivery_loop,
        )
        # Si el LLM cerró pidiendo un dato extra al cliente (válvulas, medida, retenes,
        # muescas, etc.), preservamos state=READY + context para que el próximo turno
        # arranque con toda la info ya recolectada. Sin esto, el cliente responde
        # "8v" o "STD" y el bot le pide desde cero "¿qué repuesto necesitás?" porque
        # el reset a IDLE+{} habría borrado repuesto/marca/modelo/motor.
        if _looks_like_clarification(response):
            logger.warning(
                f"[STATE] LLM pidió clarificación, preservo context y dejo state=READY "
                f"(context keys: {sorted(context.keys())})"
            )
            return _apply_first_turn_greeting(response, is_first_turn)
        # Delivery final: preservamos los datos del vehículo (marca/modelo/motor)
        # para mejorar UX cuando el cliente pide otro repuesto para el mismo auto
        # ("ahora aros pistón" sin re-decir Picasso 1.5). Borramos el resto:
        # repuesto, combustible, valvulas, medida, retenes, calentador, etc.
        # — todo lo específico del repuesto que acabamos de cotizar.
        # El reset full se dispara cuando el siguiente mensaje arranca con saludo
        # (ver step 0 al inicio de process_message), cubriendo el caso de otro
        # cliente que toma el WhatsApp después.
        vehicle_only = {
            k: v for k, v in context.items()
            if k in ("marca", "modelo", "motor") and v
        }
        logger.warning(
            f"[STATE] delivery final, preservo vehículo (keys: {sorted(vehicle_only.keys())}), "
            f"limpio repuesto/dimensiones"
        )
        await self._db.update_conversation_state(conversation["id"], "IDLE", vehicle_only)
        return _apply_first_turn_greeting(response, is_first_turn)

    async def _run_llm(
        self, user_text: str, vehicle_context: dict, conversation: dict,
        pregunta_tecnica: str | None = None,
        post_delivery_followup: bool = False,
    ) -> str:
        """Ejecuta el loop LLM con function calling e inyecta el contexto del vehículo."""
        recent_messages = await self._db.get_recent_messages(conversation["id"], limit=10)

        # Inyectar contexto confirmado en el system prompt
        # Combinar motor + combustible + valvulas en un solo campo para el LLM
        context_header = ""
        ctx = dict(vehicle_context)
        motor_parts = [p for p in [ctx.pop("motor", ""), ctx.pop("combustible", ""), ctx.pop("valvulas", "")] if p]
        if motor_parts:
            ctx["motor"] = " ".join(motor_parts)
        context_parts = [f"{k}={v}" for k, v in ctx.items() if v]
        if context_parts:
            context_header = f"[DATOS CONFIRMADOS DEL CLIENTE: {', '.join(context_parts)}]\n\n"
        if pregunta_tecnica:
            context_header += f"[PREGUNTA TÉCNICA DEL CLIENTE: '{pregunta_tecnica}'. Respondé esta pregunta en el mismo mensaje.]\n\n"
        if post_delivery_followup:
            # Bug detectado en produccion (Kangoo F8Q, 14:21): tras un delivery
            # cortado a medias, el cliente pregunta "ma das el link?" y el LLM
            # contestaba "que repuesto necesitas?" — perdiendo el hilo. Esta
            # nota fuerza al LLM a mirar el historial antes de re-preguntar.
            context_header += (
                "[CONTEXTO DE CONVERSACION: el cliente esta haciendo seguimiento "
                "a un producto que YA pediste o ofreciste en mensajes anteriores. "
                "Mira los ultimos turnos del historial (incluyendo respuestas tuyas "
                "que pueden haber quedado cortadas) para entender a que repuesto se "
                "refiere. NO preguntes 'que repuesto necesitas?' a menos que el "
                "cliente claramente este abriendo una consulta nueva.]\n\n"
            )

        messages = [{"role": "system", "content": f"{_get_business_status()}\n\n{context_header}{SYSTEM_PROMPT}"}]
        for msg in recent_messages:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_text})

        # LLM call with function calling loop
        max_iterations = 5
        for _ in range(max_iterations):
            response = await litellm.acompletion(
                model=self._model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )

            choice = response.choices[0]

            if not choice.message.tool_calls:
                return choice.message.content or "Disculpá, no pude procesar tu consulta."

            messages.append(choice.message.model_dump())

            for tool_call in choice.message.tool_calls:
                result = await self._execute_tool(
                    tool_call.function.name,
                    json.loads(tool_call.function.arguments),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        logger.warning(f"Agent loop agotó {max_iterations} iteraciones para sender={conversation.get('phone_number', '?')}")
        return (
            "No pude completar la respuesta de la demo. Podés iniciar una nueva "
            "conversación o contactar a MetaIA desde el botón de la página."
        )

    async def _execute_tool(self, name: str, args: dict) -> dict:
        """Ejecuta una herramienta y retorna el resultado."""
        logger.info(f"Ejecutando tool: {name} con args: {args}")

        if name == "search_products":
            products = await self._wc_client.search_products(
                search=args.get("search"),
                sku=args.get("sku"),
                per_page=5,
            )
            logger.info(f"search_products query='{args.get('search')}' returned {len(products)} results")
            deduped = _dedup_by_sku(products)
            state = _classify_inventory_state(deduped)
            if state == "no_catalogado":
                return {
                    "status": "no_catalogado",
                    "query": args.get("search", ""),
                    "message": f"No se encontraron productos buscando '{args.get('search', '')}'.",
                }
            if state == "agotado":
                return {"status": "agotado", "products": [_sanitize_product(p) for p in deduped]}
            return {"status": "ok", "products": [_sanitize_product(p) for p in deduped]}

        elif name == "search_products_advanced":
            repuesto = args.get("repuesto", "")
            marca = args.get("marca_vehiculo", "")
            modelo = args.get("modelo", "")
            motor = args.get("motor", "")
            combustible = args.get("combustible", "")
            valvulas = args.get("valvulas", "")
            medida = args.get("medida", "") or None
            retenes = args.get("retenes", "") or None
            calentador = args.get("calentador", "") or None
            motor_code = args.get("motor_code", "") or None
            # Normalización defensiva de motor_code: el LLM podría mandar prefijos como
            # "motor TU5JP4" o "1.6 16v TU5JP4". Quedarnos solo con la parte útil — el
            # matcher hace substring, así que limpiar prefijos numéricos preserva
            # robustez sin perder match (el código de motor en sí es lo único que importa).
            if motor_code:
                mc_stripped = motor_code.strip()
                # Si el cliente/LLM mandó "1.6 16v TU5JP4", separar y quedarnos con la cola.
                mc_match = re.search(r"^(?:\d+\.\d+\s+(?:8v|16v)\s+)(.+)$", mc_stripped, re.IGNORECASE)
                if mc_match:
                    motor_code = mc_match.group(1).strip()
                else:
                    motor_code = mc_stripped
                if not motor_code:
                    motor_code = None
            # Normalización defensiva de retenes: el LLM podría mandar 'con retenes' completo
            if retenes:
                retenes_lower = retenes.strip().lower()
                if retenes_lower.startswith("con"):
                    retenes = "con"
                elif retenes_lower.startswith("sin"):
                    retenes = "sin"
                else:
                    retenes = None
            # Normalización defensiva de calentador: el LLM podría mandar 'calentador vertical' completo
            if calentador:
                calentador_lower = calentador.strip().lower()
                if "vertical" in calentador_lower:
                    calentador = "vertical"
                elif "horizontal" in calentador_lower:
                    calentador = "horizontal"
                else:
                    calentador = None
            # valvulas para clarificación: pasamos el string original; la WC client lo normaliza.
            valvulas_clar = valvulas.strip().lower() if valvulas else None

            logger.warning(f"[SEARCH_ADV] args recibidos: repuesto='{repuesto}', marca='{marca}', modelo='{modelo}', motor='{motor}', combustible='{combustible}', valvulas='{valvulas}', medida='{medida}', retenes='{retenes}', calentador='{calentador}', motor_code='{motor_code}'")

            # marca/modelo/combustible se filtran en Python; motor se expande al texto de búsqueda
            attr_filters: dict = {}
            if marca:
                # Resolver alias antes del filtro: si el LLM extrae "VW", el catálogo
                # carga la marca como "Volkswagen" y un substring directo nunca matchea.
                # _MARCA_ALIASES ya se aplica en _expand_motor para el lookup del índice;
                # acá lo hacemos para el filtro Python para que sean consistentes.
                # Caso testigo: RND-0054 (VW Gol 1.8) — el bot trae el SKU del fulltext
                # WC pero el filtro lo descartaba porque comparaba 'vw' con 'volkswagen'.
                marca_norm = _MARCA_ALIASES.get(marca.strip().lower(), marca.strip().lower())
                attr_filters["pa_marca-vehiculo"] = marca_norm
            if modelo:
                attr_filters["pa_modelo"] = modelo.lower()
            if combustible:
                attr_filters["pa_combustible"] = combustible.lower()

            expanded_motors: list[str] = []
            if motor:
                # max_results=5: el snapshot pone primero las 8v y luego las 16v,
                # con max_results=3 quedaban afuera variantes válidas (TU5JP4 16v,
                # XU10J4 16v, etc.). Validado con sample del 7-may: el cliente
                # pide cilindrada genérica '1.6' o '2.0' y el catálogo tiene
                # tanto 8v como 16v en la misma cilindrada — necesitamos cubrir
                # ambas. Cada slot extra implica 1 fetch adicional a WC por turno.
                expanded_motors = _expand_motor(
                    motor,
                    marca=marca,
                    max_results=5,
                    combustible=combustible,
                    motor_expand_index=self._motor_expand_index,
                )
                logger.warning(f"[SEARCH_ADV] motor expandido: {expanded_motors}")

                # Si el motor se expandió con un bucket de combustible específico
                # (ej: combustible='diesel' → F8Q, F9Q, DW8…), el filtro
                # pa_combustible es REDUNDANTE con pa_motor: el código de motor
                # ya identifica el fuel type. Mantenerlo agrega solo riesgo de
                # rechazar productos con pa_combustible mistagged en el catálogo
                # (la auditoría 8-abr cuantificó 17 productos diesel cargados
                # como Nafta — caso testigo: ZL00129 F8Q horizontal). Lo
                # droppeamos. Si el usuario pidió combustible pero NO hubo
                # expansión (motor desconocido), pa_combustible se conserva.
                if expanded_motors and "pa_combustible" in attr_filters:
                    logger.warning(
                        f"[SEARCH_ADV] motor expandido con bucket combustible='{combustible}' "
                        f"— droppeando filtro pa_combustible (redundante con pa_motor, "
                        f"evita rechazar productos con combustible mistagged)"
                    )
                    attr_filters.pop("pa_combustible", None)

                if expanded_motors:
                    # Una búsqueda por cada motor expandido (máx 3), deduplicar por ID.
                    # Cada iteración arma su propio iter_filters con pa_motor = código expandido
                    # para validar en Python que el producto realmente sea de ese motor.
                    all_products: list[dict] = []
                    seen_ids: set[int] = set()
                    for full_motor in expanded_motors:
                        iter_filters = dict(attr_filters)
                        iter_filters["pa_motor"] = full_motor.lower()
                        # marca y modelo van solo en attr_filters (filtrado Python).
                        # WC fulltext no busca en atributos, incluirlos en search_text rompe el match.
                        # NUNCA inyectar "diesel" al search_text: muchos productos tienen "TDI",
                        # "HDi", "CDI", etc. en el nombre — nunca la palabra "diesel" literal.
                        # El combustible lo filtra pa_combustible cuando el usuario lo declara;
                        # para motores diesel inferidos por código, el pa_motor ya apunta al
                        # bucket correcto (sanitización en motor_catalog).
                        # search_text simplificado a primer-sustantivo + cilindrada para
                        # evitar romper el fulltext WC con tokens del motor expandido. El motor
                        # completo sigue yendo a pa_motor (filtro Python). Ver _simplify_search_text.
                        search_text = _simplify_search_text(repuesto, full_motor, marca=marca)
                        logger.warning(f"[SEARCH_ADV] busqueda: search_text='{search_text}', attr_filters={iter_filters}")
                        batch = await self._wc_client.search_products_by_attributes(
                            search=search_text,
                            attribute_filters=iter_filters,
                            per_page=10,
                            repuesto=repuesto,
                            medida=medida,
                            valvulas=valvulas_clar,
                            retenes=retenes,

                            calentador=calentador,
                            motor_code=motor_code,
                        )
                        if isinstance(batch, dict) and batch.get("needs_clarification"):
                            return batch
                        logger.warning(f"[SEARCH_ADV] batch '{full_motor}': {len(batch)} resultados")
                        for p in batch:
                            if p["id"] not in seen_ids:
                                seen_ids.add(p["id"])
                                all_products.append(p)

                    # ─── Cross-batch clarification re-detection ───
                    # Cada batch corre detectores per-batch dentro de
                    # search_products_by_attributes. Pero si dos variantes
                    # legítimas terminan en batches distintos (ej: vertical en
                    # F8Q y horizontal en F9Q por tagging asimétrico de motor),
                    # ningún batch dispara clarificación por sí solo. Re-corremos
                    # los detectores sobre la unión deduplicada para rescatar
                    # esos casos. Orden: válvulas → retenes → calentador → motor → medida.
                    if all_products:
                        # IMPORTANTE: para los detectores que corren DESPUÉS de
                        # una dimensión que el cliente YA filtró, restringir
                        # `all_products` a los que pasan ese filtro. Sin esto,
                        # productos del batch de otro motor que NO matchean el
                        # calentador/combustible del cliente contaminan la
                        # detección de motor y disparan clarificación espuria.
                        # Caso testigo: SRCH-M-0011-P2 (Kangoo F8Q horizontal)
                        # — el batch F9Q traía productos no-horizontales que
                        # ensuciaban la detección motor con F9Q vs F8Q.
                        def _filtered_for_detect() -> list:
                            out = all_products
                            if valvulas_clar:
                                out = [p for p in out if _product_matches_valvulas(p, valvulas_clar)]
                            if retenes:
                                out = [p for p in out if _product_matches_retenes(p, retenes)]
                            if calentador:
                                out = [p for p in out if _product_matches_calentador(p, calentador)]
                            if combustible:
                                comb_lower = combustible.strip().lower()
                                out = [
                                    p for p in out
                                    if not p.get("attributes", {}).get("Combustible")
                                    or any(comb_lower in v.lower()
                                           for v in p["attributes"]["Combustible"])
                                ]
                            return out

                        if not valvulas_clar:
                            cb = _detect_valvulas_variants(all_products)
                            if cb:
                                logger.warning(f"[CROSS_BATCH] valvulas: {cb.get('available_valvulas')}")
                                return cb
                        if not retenes:
                            cb = _detect_retenes_variants(_filtered_for_detect())
                            if cb:
                                logger.warning(f"[CROSS_BATCH] retenes: {cb.get('available_retenes')}")
                                return cb
                        if not calentador:
                            cb = _detect_calentador_variants(_filtered_for_detect())
                            if cb:
                                logger.warning(f"[CROSS_BATCH] calentador: {cb.get('available_calentador')}")
                                return cb
                        if not motor_code:
                            # Hints de cilindrada/valvulas para que la detección no
                            # mezcle motores de otras (cilindrada, valvulas) que
                            # entraron por el set por ruido del fulltext WC.
                            cil_hint = (motor.strip().split()[0]
                                        if motor and re.match(r"^\d+\.\d+", motor.strip())
                                        else None)
                            val_hint = (valvulas_clar
                                        if valvulas_clar in ("8v", "16v")
                                        else None)
                            cb = _detect_motor_variants(
                                _filtered_for_detect(),
                                cilindrada_hint=cil_hint,
                                valvulas_hint=val_hint,
                            )
                            if cb:
                                logger.warning(
                                    f"[CROSS_BATCH] motor (cil={cb.get('cilindrada')} "
                                    f"val={cb.get('valvulas')}): {cb.get('available_motor_codes')}"
                                )
                                return cb
                        if not medida:
                            cb = _detect_measure_variants(_filtered_for_detect())
                            if cb:
                                logger.warning(f"[CROSS_BATCH] medida: {cb.get('available_measures')}")
                                return cb

                    products = all_products[:10]
                else:
                    # Motor no matchea ningún term del catálogo — búsqueda directa.
                    # Agregar valvulas como filtro pa_motor si está disponible.
                    iter_filters = dict(attr_filters)
                    if valvulas:
                        iter_filters["pa_motor"] = valvulas.lower()
                    # Misma regla que arriba: nada de "diesel" al search_text.
                    # Search_text simplificado — ver _simplify_search_text.
                    search_text = _simplify_search_text(repuesto, motor, marca=marca)
                    logger.warning(f"[SEARCH_ADV] busqueda directa (sin expansion): search_text='{search_text}', attr_filters={iter_filters}")
                    products = await self._wc_client.search_products_by_attributes(
                        search=search_text,
                        attribute_filters=iter_filters if iter_filters else None,
                        per_page=10,
                        repuesto=repuesto,
                        medida=medida,
                        valvulas=valvulas_clar,
                        retenes=retenes,

                        calentador=calentador,
                        motor_code=motor_code,
                    )
                    if isinstance(products, dict) and products.get("needs_clarification"):
                        return products
            else:
                # Sin motor — agregar valvulas como filtro pa_motor si está disponible.
                iter_filters = dict(attr_filters)
                if valvulas:
                    iter_filters["pa_motor"] = valvulas.lower()
                # Sin motor: solo primer-token del repuesto.
                search_text = _simplify_search_text(repuesto, "", marca=marca)
                logger.warning(f"[SEARCH_ADV] busqueda sin motor: search_text='{search_text}', attr_filters={iter_filters}")
                products = await self._wc_client.search_products_by_attributes(
                    search=search_text,
                    attribute_filters=iter_filters if iter_filters else None,
                    per_page=5,
                    repuesto=repuesto,
                    medida=medida,
                    valvulas=valvulas_clar,
                    retenes=retenes,

                    calentador=calentador,
                    motor_code=motor_code,
                )
                if isinstance(products, dict) and products.get("needs_clarification"):
                    return products

            query_desc = f"repuesto='{repuesto}'"
            if marca:
                query_desc += f", marca='{marca}'"
            if modelo:
                query_desc += f", modelo='{modelo}'"
            if motor:
                query_desc += f", motor='{motor}'"
            if combustible:
                query_desc += f", combustible='{combustible}'"
            if valvulas:
                query_desc += f", valvulas='{valvulas}'"

            logger.info(f"search_products_advanced {query_desc} returned {len(products)} results")
            logger.info(f"[SEARCH_ADV] resultados finales: {len(products)} productos, IDs: {[p['id'] for p in products[:5]]}")

            if not products:
                # Fallback: reintentar solo con marca, pero incluyendo modelo/motor en
                # el search_text para que WC fulltext siga encontrando el producto correcto.
                # Esto resuelve casos donde pa_modelo tiene términos duplicados o mal asignados
                # que hacen que el filtro Python devuelva 0 aunque el producto exista.
                if attr_filters and (modelo or motor):
                    fallback_filters: dict = {}
                    if marca:
                        # Mismo aliasing que el filtro principal para mantener
                        # coherencia: si el LLM extrae 'VW' el filtro tiene que
                        # comparar contra 'volkswagen' del catálogo, no 'vw'.
                        fallback_filters["pa_marca-vehiculo"] = _MARCA_ALIASES.get(
                            marca.strip().lower(), marca.strip().lower()
                        )
                    # Iterar sobre TODAS las expansiones de motor (no solo [0]).
                    # Cuando _expand_motor devuelve varios candidatos
                    # (ej: ["1.9 8v DW8", "1.9 8v F8Q"]) usar solo [0] dejaba al
                    # fallback ciego al resto. Caso real 30-abr-2026: "tapa
                    # cilindros Kangoo 1.9 diesel" — la expansion devolvia DW8
                    # primero pero el SKU correcto es F8Q. Sin esta iter, el
                    # fallback nunca encontraba F8Q porque el search_text con
                    # DW8 no matchea el titulo "Renault 1.9 8v Diesel F8Q".
                    motors_to_try = list(expanded_motors) if expanded_motors else (
                        [motor] if motor else [""]
                    )
                    all_fb_products: list[dict] = []
                    seen_fb_ids: set[int] = set()
                    for fb_motor in motors_to_try:
                        # FALLBACK también usa search_text simplificado (primer-token +
                        # cilindrada) en lugar de meter el motor expandido entero.
                        # Mismo motivo: evitar que tokens del motor que no están en
                        # el title rompan el match del fulltext.
                        fallback_search_text = _simplify_search_text(repuesto, fb_motor, marca=marca)
                        logger.warning(
                            f"[SEARCH_ADV] FALLBACK iter '{fb_motor}': "
                            f"search_text='{fallback_search_text}', filters={fallback_filters}"
                        )
                        batch_fb = await self._wc_client.search_products_by_attributes(
                            search=fallback_search_text,
                            attribute_filters=fallback_filters if fallback_filters else None,
                            per_page=10,
                            repuesto=repuesto,
                            medida=medida,
                            valvulas=valvulas_clar,
                            retenes=retenes,

                            calentador=calentador,
                            motor_code=motor_code,
                        )
                        if isinstance(batch_fb, dict) and batch_fb.get("needs_clarification"):
                            return batch_fb
                        # Post-filtro estricto sobre pa_modelo: si el producto tiene
                        # pa_modelo cargado pero NO matchea el modelo pedido, lo
                        # descartamos. Esto cierra el agujero del Ranger 3.0 (caso
                        # 5-may-2026): JD-101 está taggeado pa_modelo=['F100',
                        # 'Fairlane','Falcon'] y el cliente pidió 'ranger' — el
                        # fallback lo dejaba pasar porque dropea pa_modelo entero,
                        # y el LLM con temperatura 0.3 a veces lo ofrecía como si
                        # fuera para Ranger. Productos con pa_modelo=[] vacío
                        # SIGUEN pasando (preserva rescate Fox/CHT, Kangoo F8Q y
                        # otros casos documentados de catálogo incompleto).
                        modelo_lower = (modelo or "").strip().lower()
                        for p in batch_fb:
                            if modelo_lower:
                                prod_modelo_values = (
                                    p.get("attributes", {}).get("Modelo", []) or []
                                )
                                if prod_modelo_values and not any(
                                    modelo_lower in v.lower()
                                    for v in prod_modelo_values
                                ):
                                    logger.warning(
                                        f"[SEARCH_ADV] FALLBACK descarta sku={p.get('sku','')} "
                                        f"name={p.get('name','')[:50]!r} "
                                        f"pa_modelo={prod_modelo_values} "
                                        f"(no matchea modelo='{modelo}')"
                                    )
                                    continue
                            if p["id"] not in seen_fb_ids:
                                seen_fb_ids.add(p["id"])
                                all_fb_products.append(p)
                    products = all_fb_products
                    logger.info(
                        f"search_products_advanced FALLBACK (solo marca, "
                        f"{len(motors_to_try)} motors) returned {len(products)} results"
                    )
                    # Reordenar por overlap de tokens modelo/motor para que el
                    # producto más específico quede primero (mitiga falsos positivos
                    # al no filtrar por pa_modelo).
                    if products and (modelo or motor):
                        products = sorted(
                            products,
                            key=lambda p: _score_product_relevance(p, modelo, motor),
                            reverse=True,
                        )
                        logger.warning(
                            f"[SEARCH_ADV] FALLBACK scores: "
                            + ", ".join(
                                f"{p['name'][:40]}={_score_product_relevance(p, modelo, motor)}"
                                for p in products[:5]
                            )
                        )

                if not products:
                    return {
                        "status": "no_catalogado",
                        "query": query_desc,
                        "message": f"No se encontraron productos con {query_desc}. Probá con search_products usando texto libre.",
                    }

            deduped = _dedup_by_sku(products)
            state = _classify_inventory_state(deduped)
            if state == "agotado":
                return {"status": "agotado", "products": [_sanitize_product(p) for p in deduped]}
            return {"status": "ok", "products": [_sanitize_product(p) for p in deduped]}

        elif name == "get_product_details":
            product = await self._wc_client.get_product(args["product_id"])
            if not product:
                return {"status": "not_found"}
            return {"status": "ok", "product": _sanitize_product(product)}

        elif name == "escalate_to_human":
            return {
                "status": "portfolio_cta",
                "message": (
                    "Esta demostración no procesa ventas ni deriva consultas de repuestos. "
                    "Para implementar una solución similar, usá el CTA de MetaIA "
                    "visible en la página."
                ),
                "contact_phone": "",
            }

        return {"status": "error", "message": f"Herramienta desconocida: {name}"}
