#!/usr/bin/env python3
"""Auditoría del idioma de la web UI (ver `static/i18n.js` para la convención).

No hay tests de la web, así que esto es la red de seguridad del trabajo de i18n.
Hace dos chequeos, ambos de sólo lectura:

  (A) STRINGS SIN ENVOLVER — texto español en el fuente que todavía no pasa por
      t()/tAttr(). No parsea JavaScript: escanea sólo las cuatro posiciones donde
      de verdad aparece texto de interfaz, que es lo que separa una herramienta
      usable de uno que escupe 1500 falsos positivos:

        1. argumento de alert( / confirm(
        2. lado derecho de .textContent = / .innerHTML =
        3. title= / placeholder= / aria-label= dentro de template literals
        4. nodos de texto >…< dentro de template literals

  (B) CLAVES HUÉRFANAS — entradas del diccionario que ya no aparecen en el
      fuente. Caza el modo de falla número uno de la convención "la clave es el
      texto español": alguien edita la frase en el call site (una coma, una
      tilde, un «...» por «…»), la clave deja de matchear y la traducción
      DESAPARECE EN SILENCIO, sin error ni aviso.

Lo que esto NO puede ver: si la traducción es correcta, y el texto que sólo
existe en runtime. Para medir cobertura sobre la página renderizada está
`i18n_coverage.py`, que abre la app en un navegador.

Uso:
    .venv/bin/python scripts/i18n_audit.py            # ambos chequeos
    .venv/bin/python scripts/i18n_audit.py --sin-envolver
    .venv/bin/python scripts/i18n_audit.py --huerfanas

Sale con código 1 si encuentra algo, para poder encadenarlo.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
STATIC = RAIZ / "src" / "scrapitero" / "web" / "static"
INDEX = STATIC / "index.html"
I18N = STATIC / "i18n.js"
APP = RAIZ / "src" / "scrapitero" / "web" / "app.py"

# Señal de que un literal es texto para una persona y no una clave/id/CSS.
# Acentos y signos de apertura son la señal fuerte; las palabras funcionales
# atrapan lo que no lleva tilde ('Sin datos', 'Crear relevamiento').
_ACENTOS = "áéíóúüñÁÉÍÓÚÜÑ¿¡"
_PALABRAS = (
    r"\b(de la|de los|del|con|para|sin|los|las|una|este|esta|todos|todas|hay|"
    r"relevamiento|parcela|parcelas|zona|calle|altura|habitante|habitantes|"
    r"crear|guardar|actualizar|nuevo|nueva|error|buscar|estimar|cargando|"
    r"ninguno|ninguna|no hay|sí|desconocido|dirección|direcciones|comercio|"
    r"vivienda|edificio|hotel|hoteles|fuente|estado|resultado|total)\b"
)

# Vocabulario contractual del cliente brasilero: ya está en portugués y NO se
# traduce (ver docs/TIPOS_PROPIEDAD.md y TIPOS_EDIFICACION en web/app.py).
_NO_TRADUCIBLE = re.compile(
    r"^(HOTEL|MOTEL|FLAT|PENSÃO|RESIDÊNCIA|APARTAMENTO|LOTE VAZIO|"
    r"COMÉRCIO EM GERAL|INDÚSTRIA|ESCOLA|HOSPITAL|SHOPPING|"
    r"DSC_\w+|COD_\w+|NUM_\w+|IND_\w+|QTD_\w+)$"
)


def _es_texto_humano(s: str) -> bool:
    """¿Este literal es una frase para una persona?"""
    s = s.strip()
    if len(s) < 3 or _NO_TRADUCIBLE.match(s):
        return False
    # Descarta lo que es claramente código/marcado y no prosa.
    if re.fullmatch(r"[\w./#:%-]+", s):          # identificador, clase, ruta, color
        return False
    if s.startswith(("http", "/api/", "${")):
        return False
    return bool(set(s) & set(_ACENTOS)) or bool(re.search(_PALABRAS, s, re.I))


def _ya_envuelto(linea: str, pos: int) -> bool:
    """¿El literal que arranca en `pos` viene precedido por t( o tAttr(?"""
    return bool(re.search(r"\bt(?:Attr)?\(\s*$", linea[:pos]))


# Las cuatro posiciones donde vive el texto de interfaz.
_POSICIONES = [
    ("alert/confirm", re.compile(r"\b(?:alert|confirm)\(\s*(['\"])(.+?)\1")),
    ("textContent",   re.compile(r"\.(?:textContent|innerHTML)\s*=\s*(['\"])(.+?)\1")),
    ("atributo",      re.compile(r"(?:title|placeholder|aria-label)=(['\"])([^'\"$][^'\"]*?)\1")),
    ("nodo de texto", re.compile(r">([^<>{}`$]{3,}?)<")),
]


def _lineas_en_alcance(archivo: Path) -> list[tuple[int, str]]:
    """De app.py sólo entra `_login_page`: el resto (mensajes de error de la API,
    avisos de Telegram, cabeceras de CSV) quedó fuera del alcance acordado."""
    lineas = list(enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1))
    if archivo.name != "app.py":
        return lineas
    ini = next((n for n, l in lineas if l.startswith("def _login_page")), None)
    if ini is None:
        return []
    fin = next((n for n, l in lineas if n > ini and l.startswith(("def ", "@app"))), 10**9)
    return [(n, l) for n, l in lineas if ini <= n < fin]


def sin_envolver() -> list[tuple[str, int, str, str]]:
    hallazgos = []
    for archivo in (INDEX, APP):
        # Un data-i18n-html abarca varias líneas (los párrafos de ayuda llevan
        # <strong>/<br> adentro): el walker ya los traduce enteros, así que se
        # saltea hasta el cierre del elemento.
        en_bloque_html = False
        # Un tag abierto en varias líneas (`<button …\n title="…">`) lleva el
        # data-i18n-attr en la primera: los atributos de las siguientes también
        # están cubiertos.
        en_tag_marcado = False
        for n, linea in _lineas_en_alcance(archivo):
            if en_bloque_html:
                if re.search(r"</(p|div|span|label)>", linea):
                    en_bloque_html = False
                continue
            if "data-i18n-html" in linea and not re.search(r"</(p|div|span|label)>", linea):
                en_bloque_html = True
                continue
            if en_tag_marcado:
                if ">" in linea:
                    en_tag_marcado = False
                continue
            if "data-i18n-attr" in linea and ">" not in linea.split("data-i18n-attr")[1]:
                en_tag_marcado = True
                continue
            if linea.lstrip().startswith(("//", "*", "#")):
                continue                        # comentarios: no son UI
            for etiqueta, rx in _POSICIONES:
                for m in rx.finditer(linea):
                    texto = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
                    if not _es_texto_humano(texto):
                        continue
                    if _ya_envuelto(linea, m.start(m.lastindex or 1)):
                        continue
                    # El patrón de nodo de texto (>…<) cruza concatenaciones y a
                    # veces captura un tramo que YA pasa por t(). No es pendiente.
                    if re.search(r"\bt(?:Attr)?\(", texto):
                        continue
                    # data-i18n en el markup estático ya lo cubre el walker
                    if "data-i18n" in linea:
                        continue
                    hallazgos.append((archivo.name, n, etiqueta, texto.strip()))
    return hallazgos


def _claves_diccionario() -> list[str]:
    """Las claves de I18N_PT, tal cual están escritas en i18n.js.

    Acepta los dos delimitadores: una clave que contiene comillas simples se
    escribe con dobles ("…'Comparar con…'"). Leyendo sólo las simples, esas
    entradas quedaban invisibles y el chequeo (C) las reportaba como sin traducir."""
    fuente = I18N.read_text(encoding="utf-8")
    cuerpo = fuente[fuente.index("var I18N_PT = {"):]
    return [m.group(2) for m in
            re.finditer(r"^\s*(['\"])((?:(?!\1)[^\\]|\\.)*)\1:", cuerpo, re.M)]


def _norm(s: str) -> str:
    """Colapsa espacios y resuelve el escape de comilla simple: el mismo texto se
    escribe `\\'` dentro de un literal 'simple' y `'` dentro de uno "doble"."""
    return re.sub(r"\s+", " ", s).replace("\\'", "'").strip()


def huerfanas() -> list[str]:
    """Claves del diccionario que ya no aparecen en el fuente.

    Las claves salen de i18n.js con los escapes SIN resolver (`\\n` son dos
    caracteres, no un salto) y en index.html/app.py están escritas igual, así que
    se comparan crudas; sólo se colapsan espacios, en los dos lados por igual.

    Una clave cuenta como usada si:
      · aparece como literal COMPLETO entre comillas — así se escribe en t('…').
        Tiene que ser completo: con una simple búsqueda de substring, borrar el
        botón '⬇ CSV' no se detectaba nunca, porque '⬇ CSV Operadora' lo contiene.
      · o aparece dentro del markup estático, donde el texto va suelto en el HTML
        (los data-i18n) y no entre comillas.
    """
    index = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    # Markup estático: todo lo anterior al <script> inline principal. Arranca en el
    # <head> y no en el <body> porque el <title> también lleva data-i18n.
    fin = index.index("\n<script>\n", index.index("<body>"))
    markup = _norm(index[:fin] + app)
    codigo = index + app

    sueltas = []
    for clave in _claves_diccionario():
        if not _norm(clave):
            continue
        # Un mismo texto se escribe distinto según el delimitador: dentro de un
        # literal 'simple' la comilla va como \' y dentro de uno "doble" va tal cual.
        plano = clave.replace("\\'", "'")
        literal = (f"'{plano.replace(chr(39), chr(92) + chr(39))}'" in codigo
                   or f'"{plano}"' in codigo)
        if not literal and _norm(clave) not in markup:
            sueltas.append(clave)
    return sueltas


def sin_traduccion() -> list[str]:
    """Textos que YA pasan por t()/tAttr() pero no tienen entrada en el diccionario.

    Es el hueco simétrico de `huerfanas()`: sin este chequeo se puede envolver una
    frase y olvidar la traducción, y nadie se entera — el texto sale en español,
    que es exactamente el fallback de diseño, así que no hay error ni pista.
    """
    claves = {_norm(c) for c in _claves_diccionario()}
    usados: set[str] = set()
    for archivo in (INDEX, APP):
        fuente = archivo.read_text(encoding="utf-8")
        for m in re.finditer(r"\bt(?:Attr)?\(\s*'((?:[^'\\]|\\.)*)'", fuente):
            usados.add(_norm(m.group(1)))
    return sorted(u for u in usados if u and u not in claves)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sin-envolver", action="store_true", help="sólo el chequeo (A)")
    ap.add_argument("--huerfanas", action="store_true", help="sólo el chequeo (B)")
    ap.add_argument("--sin-traducir", action="store_true", help="sólo el chequeo (C)")
    args = ap.parse_args()
    ambos = not (args.sin_envolver or args.huerfanas or args.sin_traducir)
    problemas = 0

    if ambos or args.sin_envolver:
        h = sin_envolver()
        print(f"── (A) strings sin envolver en t(): {len(h)}")
        for archivo, n, etiqueta, texto in h:
            print(f"   {archivo}:{n}  [{etiqueta}]  {texto[:100]}")
        problemas += len(h)

    if ambos or args.huerfanas:
        claves = _claves_diccionario()
        s = huerfanas()
        print(f"\n── (B) claves del diccionario que ya no están en el fuente: "
              f"{len(s)} de {len(claves)}")
        for clave in s:
            print(f"   {clave[:100]}")
        if s:
            print("   ⚠ Estas traducciones NO se aplican: el texto del fuente cambió.")
        problemas += len(s)

    if ambos or args.sin_traducir:
        f = sin_traduccion()
        print(f"\n── (C) textos envueltos en t() que no tienen traducción: {len(f)}")
        for txt in f:
            print(f"   {txt[:100]}")
        if f:
            print("   ⚠ Estos salen en español aunque la UI esté en portugués.")
        problemas += len(f)

    print(f"\n{'✓ sin pendientes' if not problemas else f'✗ {problemas} pendiente(s)'}")
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
