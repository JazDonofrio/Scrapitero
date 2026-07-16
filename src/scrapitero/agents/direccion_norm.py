"""Normalización de direcciones para la comparativa entre relevamientos.

El matching por dirección (survey actual vs relevamiento anterior/baseline) necesita
que "AV. BRIG. EDUARDO GOMES 1234", "Avenida Brigadeiro Eduardo Gomes Nº 1234" y
"RUA - BRIG. EDUARDO GOMES, 1234" colapsen en la misma clave. La clave es
`(calle_norm, numero_norm)`:

  - minúsculas, sin acentos, sin puntuación;
  - tipo de vía y títulos canonicalizados a su forma CORTA por diccionario
    (avenida/avda→av, doutor/doctor/dra→dr) — solo en posición inicial, para no
    pisar iniciales dentro del nombre;
  - preposiciones (de/do/da/del/la…) eliminadas en cualquier posición: las fuentes
    son inconsistentes con ellas ("Avenida do Independente" vs "Avenida Independente");
  - número: solo dígitos, sin ceros a la izquierda; "S/N", "s n", "0" → "" (sin número).

Es heurística: lo que no se reconoce queda tal cual (nunca se pierde el nombre).
Cubre español y portugués — mismo normalizador para ARG y BRA.
"""

from __future__ import annotations

import re
import unicodedata

# Tipo de vía → forma corta canónica (ES + PT). Solo se aplica al PRIMER token.
TIPOS_VIA = {
    "avenida": "av", "avda": "av", "av": "av", "aven": "av",
    "calle": "c", "cl": "c", "c": "c",
    "rua": "r", "r": "r",
    "travessa": "tv", "trav": "tv", "tv": "tv",
    "alameda": "al", "al": "al",
    "estrada": "est", "est": "est",
    "rodovia": "rod", "rod": "rod", "ruta": "ruta", "rta": "ruta",
    "praca": "pc", "pc": "pc", "pca": "pc", "plaza": "pza", "pza": "pza",
    "pasaje": "pje", "pje": "pje", "psje": "pje", "passagem": "pje",
    "diagonal": "diag", "diag": "diag",
    "bulevar": "bv", "boulevard": "bv", "blvd": "bv", "bv": "bv", "bvar": "bv",
    "camino": "cno", "cno": "cno",
    "beco": "beco", "viela": "viela", "via": "via", "largo": "largo",
    "ladeira": "ladeira", "marginal": "marginal",
}

# Títulos honoríficos/profesionales → forma corta canónica (ES + PT).
# Se aplica al token siguiente al tipo de vía (o al primero si no hay tipo).
TITULOS = {
    "doutor": "dr", "doutora": "dr", "doctor": "dr", "doctora": "dr",
    "dr": "dr", "dra": "dr",
    "professor": "prof", "professora": "prof", "profesor": "prof",
    "profesora": "prof", "prof": "prof", "profa": "prof",
    "engenheiro": "eng", "ingeniero": "eng", "eng": "eng", "ing": "eng",
    "general": "gral", "gral": "gral", "gal": "gral", "gen": "gral",
    "coronel": "cnel", "cnel": "cnel", "cel": "cnel",
    "brigadeiro": "brig", "brigadier": "brig", "brig": "brig",
    "marechal": "mal", "mariscal": "mal", "mal": "mal",
    "capitao": "cap", "capitan": "cap", "cap": "cap",
    "teniente": "tte", "tenente": "tte", "tte": "tte", "ten": "tte",
    "sargento": "sgt", "sgt": "sgt",
    "almirante": "alm", "alm": "alm",
    "comandante": "cmte", "cmte": "cmte",
    "presidente": "pte", "pte": "pte", "pres": "pte",
    "senador": "sen", "sen": "sen",
    "deputado": "dip", "diputado": "dip", "dep": "dip", "dip": "dip",
    "governador": "gob", "gobernador": "gob", "gov": "gob", "gob": "gob",
    "prefeito": "pref", "pref": "pref",
    "vereador": "ver",
    "ministro": "min", "min": "min",
    "monsenhor": "mons", "monsenor": "mons", "mons": "mons",
    "padre": "pe", "pe": "pe", "frei": "frei", "fray": "frei",
    "santo": "san", "santa": "sta", "san": "san", "sta": "sta", "sao": "san",
    # "S" sola antes de un nombre = São (R S BENTO = Rua São Bento). Lo mismo que hace
    # _calle_display al mostrar el nombre lindo → así el scope (del nombre lindo) matchea
    # con la dirección cruda. Solo se aplica al token siguiente al tipo de vía.
    "s": "san",
}

# Variantes de grafía del mismo nombre → forma canónica (se aplica a CUALQUIER token). El
# catastro y el relevamiento anterior a veces escriben distinto el mismo nombre propio: sin esto,
# `av pte artur bernardes` (catastro) ≠ `av pte arthur bernardes` (scope) y el filtro las separa.
_VARIANTES = {"arthur": "artur", "thereza": "teresa", "tereza": "teresa"}

# Preposiciones/artículos que se ELIMINAN en cualquier posición.
PREPOSICIONES = {"de", "del", "do", "da", "dos", "das", "la", "las", "los",
                 "el", "e", "y"}

# Sin número: variantes de S/N y cero.
_SIN_NUMERO = {"sn", "s n", "s/n", "sin numero", "sem numero", "0", ""}


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


_VIA_TODAS = set(TIPOS_VIA) | set(TIPOS_VIA.values())


def _tiene_nombre(prefijo: str) -> bool:
    """True si el texto antes de un número tiene al menos una palabra de NOMBRE real
    (2+ letras que NO es un tipo de vía: av/rua/tv…). Sirve para distinguir la altura
    ('AV FOO 66') del número que es parte del nombre ('RUA 15 DE NOVEMBRO')."""
    toks = [_sin_acentos(t) for t in re.findall(r"[^\W\d_]{2,}", (prefijo or "").lower())]
    return any(t not in _VIA_TODAS for t in toks)


def normalizar_calle(calle: str | None) -> str:
    """Nombre de calle normalizado para matching. '' si no hay calle."""
    if not calle:
        return ""
    s = _sin_acentos(str(calle)).lower()
    s = re.sub(r"[^\w\s]", " ", s)          # puntuación (incl. el " - " del BCI) → espacio
    tokens = [_VARIANTES.get(t, t) for t in s.split() if t not in PREPOSICIONES]
    if not tokens:
        return ""

    out: list[str] = []
    i = 0
    # Tipo de vía: solo en posición inicial (evita pisar iniciales del nombre).
    if tokens[i] in TIPOS_VIA:
        out.append(TIPOS_VIA[tokens[i]])
        i += 1
    # Título: el token que sigue al tipo (o el primero si no hubo tipo).
    if i < len(tokens) and len(tokens) > i + 1 and tokens[i] in TITULOS:
        out.append(TITULOS[tokens[i]])
        i += 1
    out.extend(tokens[i:])
    # Cortar anotaciones catastrales pegadas al final ("mat 42 790", "q 03 l 03",
    # "esq com ..."), nunca antes del 3er token.
    for j in range(2, len(out)):
        if out[j] in _CORTE_CALLE:
            out = out[:j]
            break
    return " ".join(out)


_VIA_VALORES = set(TIPOS_VIA.values())
_TITULO_VALORES = set(TITULOS.values())


def nucleo_calle(calle: str | None) -> str:
    """Núcleo del nombre de calle para MATCHING tolerante: `normalizar_calle` sin el tipo de vía
    ni los títulos honoríficos. Así "Rua Governador Pedro Pedrossian" y "Rua Pedro Pedrossian"
    (el catastro a veces omite el título), o "Presidente Arthur/Artur Bernardes", colapsan al
    mismo núcleo ("pedro pedrossian" / "artur bernardes"). Sirve para comparar el scope (del
    relevamiento anterior) contra la dirección del catastro sin que una variante los separe."""
    norm = normalizar_calle(calle)
    if not norm:
        return ""
    toks = norm.split()
    if toks and toks[0] in _VIA_VALORES:      # sacar tipo de vía inicial
        toks = toks[1:]
    toks = [t for t in toks if t not in _TITULO_VALORES]   # sacar títulos (gob/pte/cnel…)
    return " ".join(toks) or norm             # si quedó vacío, volver al norm (nombre = puro título)


def normalizar_numero(numero: str | int | None) -> str:
    """Número de puerta normalizado: solo dígitos, sin ceros a la izquierda.
    '' = sin número (cubre S/N, 0, vacío)."""
    if numero is None:
        return ""
    s = _sin_acentos(str(numero)).lower().strip()
    if s in _SIN_NUMERO or s.replace("/", " ").strip() in _SIN_NUMERO:
        return ""
    m = re.search(r"\d+", s)
    if not m:
        return ""
    return m.group(0).lstrip("0") or ""


# Complementos típicos detrás del número de puerta (se descartan para la clave):
# "RUA FOO 123 LOTE 15 QD 3" → numero=123.
_COMPLEMENTO_RE = re.compile(
    r"^(?:lote|lt|l|quadra|qd|qda|q|casa|apto?|apart\w*|ap|depto|dpto|dto|piso|"
    r"bloco|bl|block|esq\w*|fundos|fdo|galpao|galpão|sala|loja|lj|km|uf|ph|"
    r"local|oficina|of|mat\w*|área|area)\b", re.IGNORECASE)

# Tokens de complemento/anotación catastral que a veces vienen pegados al nombre de la
# calle SIN número ("RUA BOM JESUS MAT.42.790", "AV X ESQ. COM A..."): se corta la calle
# normalizada en el primero de estos (solo desde el 3er token, para no romper nombres
# cortos legítimos como "RUA L").
_CORTE_CALLE = {"mat", "matricula", "esq", "esquina", "q", "qd", "qda", "quadra",
                "l", "lt", "lot", "lote", "loteamento", "area", "casa", "apto", "apt",
                "bloco", "bl", "sala", "loja", "km", "fundos", "desmembrada", "remembrada"}
_SN_FINAL_RE = re.compile(r"[\s,]+s/?n\.?$", re.IGNORECASE)


def separar_numero(direccion: str | None) -> tuple[str, str]:
    """Separa una dirección completa en (calle, numero) cuando vienen juntas.

    Reglas (en orden):
      1. número con marcador explícito: "Calle 9 Nº 433" → ("Calle 9", "433");
      2. primer número suelto precedido de una palabra y seguido de fin o de un
         complemento ("RUA FOO 123 LOTE 15" → 123; "9 de Julio 1500" → 1500);
      3. si ninguno cumple, el último número suelto precedido de palabra
         ("RUA 15 DE NOVEMBRO 850" → 850).
    Lo que sigue al número (complemento) se descarta. Sin número → (direccion, "")."""
    if not direccion:
        return "", ""
    s = _SN_FINAL_RE.sub("", str(direccion).strip())
    if not s:
        return "", ""

    # 1) marcador explícito Nº/no./num
    m = re.search(r"(?:^|[\s,])n[oº°]?\.?\s*(\d+)\b", s, re.IGNORECASE)
    if m and s[:m.start()].strip(" ,"):
        return s[:m.start()].strip(" ,"), m.group(1)

    # números sueltos con al menos una palabra (2+ letras) antes
    candidatos = [m for m in re.finditer(r"(?:^|[\s,])(\d+)(?=$|[\s,\-])", s)
                  if re.search(r"[^\W\d_]{2,}", s[:m.start()])]
    if not candidatos:
        return s, ""

    # 2) la altura es el PRIMER número precedido por una palabra de NOMBRE real (no un tipo
    #    de vía): distingue "AV COUTO MAGALHAES 66 AC 23" (altura=66, resto complemento) de
    #    "RUA 15 DE NOVEMBRO 850" (15 es parte del nombre → altura=850). El resto se descarta.
    for m in candidatos:
        if _tiene_nombre(s[:m.start()]):
            return s[:m.start()].strip(" ,"), m.group(1)

    # 3) fallback: primer número con complemento/fin detrás; si no, el último.
    for m in candidatos:
        resto = s[m.end():].strip(" ,-")
        if not resto or _COMPLEMENTO_RE.match(resto):
            return s[:m.start()].strip(" ,"), m.group(1)
    m = candidatos[-1]
    return s[:m.start()].strip(" ,"), m.group(1)


def clave_direccion(calle: str | None, numero: str | int | None) -> str:
    """Clave de matching: 'calle_norm|numero_norm'. '' si no hay calle."""
    cn = normalizar_calle(calle)
    if not cn:
        return ""
    return f"{cn}|{normalizar_numero(numero)}"
