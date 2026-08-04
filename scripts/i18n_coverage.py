#!/usr/bin/env python3
"""Cobertura del idioma medida sobre la página RENDERIZADA (Playwright).

Complemento de `i18n_audit.py`, que sólo mira el fuente. Éste abre la app en un
navegador con el idioma en portugués, despliega lo que puede (formulario, wizard,
tarjetas) y busca texto que haya quedado en español.

Dos señales, en orden de confianza:

  (1) COINCIDENCIA EXACTA CON UNA CLAVE del diccionario. Si en pantalla aparece
      textualmente una frase que `i18n.js` sabe traducir, es un fallo seguro: o
      el nodo no pasa por t(), o el walker no lo alcanza. Cero falsos positivos.

  (2) HEURÍSTICA de español. Marca texto con rasgos que el portugués no tiene
      (ñ, ¿, ¡, «-ción», «-miento») o palabras cuya forma portuguesa es distinta
      (calle→rua, vivienda→moradia). Sirve para lo que nunca se agregó al
      diccionario. Tiene falsos positivos; por eso se listan aparte.

Lo que NO ve, y hay que cubrir a mano o con el lint del fuente:
alert()/confirm() (son modales bloqueantes), las ramas de error de red, y los
estados transitorios de botón ("⟳ Creando…"). "0 sin traducir" acá NO significa
"todo traducido".

Necesita la app corriendo SIN OPERADOR_PASSWORD (así no hay que firmar la cookie)
y con datos: sin al menos un relevamiento brasilero con parcelas, renderCard casi
no corre y la medición no dice nada.

Uso:
    .venv/bin/python scripts/i18n_coverage.py --url http://127.0.0.1:8766
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
I18N = RAIZ / "src" / "scrapitero" / "web" / "static" / "i18n.js"

# Vocabulario que NO se traduce a propósito: taxonomía fija del cliente brasilero
# (docs/TIPOS_PROPIEDAD.md), columnas de la operadora y datos del relevamiento.
_WHITELIST = re.compile(
    r"^(HOTEL|MOTEL|FLAT|PENSÃO|RESIDÊNCIA|APARTAMENTO|LOTE VAZIO|COMÉRCIO EM GERAL|"
    r"INDÚSTRIA|SHOPPING|SUPERMERCADO|BAR|RESTAURANTE|ESCOLA.*|HOSPITAL.*|IGREJA.*|"
    r"DSC_\w+|COD_\w+|NUM_\w+|R\$.*|AiMapping|OSM|SmartGIS|BCI PDF|Parser|Footprints|"
    # Fallback de stepLabel() para una clave de paso que no está en STEP_LABELS:
    # humaniza el identificador crudo que definió Hermes (`scope_calles_filter` →
    # «▸ Scope Calles Filter»). Es un nombre de skill, no texto de interfaz.
    r"(✓ |✗ |⏳ )?▸ .*)$"
)

# Rasgos que el portugués no tiene, o cuya forma portuguesa difiere.
_MARCAS_ES = re.compile(
    r"[ñ¿¡]|"
    r"\b\w+(ción|ciones|miento|mientos)\b|"
    r"\b(calle|calles|vivienda|viviendas|relevamiento|relevamientos|parcela|parcelas|"
    r"ciudad|edificio|edificios|habitaciones|todavía|aún|elegí|subí|podés|querés|"
    r"archivo|búsqueda|año|años|desconocido|guardar|ninguna|ninguno|"
    r"anterior es|hay que|el relevamiento|la parcela|los lotes)\b",
    re.I,
)


def _claves() -> set[str]:
    """Claves cuya traducción portuguesa DIFIERE del español.

    Las que se traducen igual ('Cancelar', '⬇ CSV', 'País'…) no sirven como señal:
    verlas en pantalla en portugués es lo correcto, no un fallo. Incluirlas haría
    que el chequeo exacto —que debe tener cero falsos positivos— dejara de valer."""
    fuente = I18N.read_text(encoding="utf-8")
    cuerpo = fuente[fuente.index("var I18N_PT = {"):]
    pares = re.findall(r"^\s*'((?:[^'\\]|\\.)*)':\s*\n?\s*'((?:[^'\\]|\\.)*)'", cuerpo, re.M)
    des = lambda x: x.replace("\\n", "\n").replace("\\'", "'").strip()
    return {des(k) for k, v in pares if des(k) != des(v)}


_JS_VOLCADO = """() => {
  const out = [];
  document.querySelectorAll('*').forEach(e => {
    if (['SCRIPT','STYLE','NOSCRIPT','TITLE'].includes(e.tagName)) return;
    [...e.childNodes].forEach(n => {
      if (n.nodeType === 3 && n.textContent.trim()) out.push(['texto', n.textContent.trim()]);
    });
    ['title','placeholder','aria-label'].forEach(a => {
      const v = e.getAttribute && e.getAttribute(a);
      if (v && v.trim()) out.push([a, v.trim()]);
    });
  });
  out.push(['title', document.title]);
  return out;
}"""


def _volcar(pw, url: str, path: str, lang: str):
    b = pw.chromium.launch()
    ctx = b.new_context()
    ctx.add_init_script(f"localStorage.setItem('aim-lang','{lang}');"
                        f"localStorage.setItem('aim-theme','light')")
    p = ctx.new_page()
    errores: list[str] = []
    p.on("pageerror", lambda e: errores.append(str(e)))
    p.goto(url + path, wait_until="domcontentloaded")
    p.wait_for_timeout(2500)
    # Desplegar todo lo que se pueda para que corran los renders
    for accion in (
        lambda: p.click("button.op-only.btn-primary", timeout=1500),          # + Nuevo relevamiento
        lambda: p.click("input[value=actualizacion]", timeout=1500),          # wizard de actualización
    ):
        try:
            accion()
            p.wait_for_timeout(500)
        except Exception:
            pass
    for chev in p.query_selector_all(".chevron")[:4]:                         # expandir tarjetas
        try:
            chev.click()
            p.wait_for_timeout(400)
        except Exception:
            pass
    p.wait_for_timeout(2500)
    datos = p.evaluate(_JS_VOLCADO)
    b.close()
    return datos, errores


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--rutas", default="/operador,/")
    ap.add_argument("--detalle", type=int, default=25, help="cuántos casos listar")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright   # import tardío: sólo si se usa

    claves = _claves()
    total_fallos = 0

    with sync_playwright() as pw:
        for path in args.rutas.split(","):
            for lang in ("es", "pt"):
                datos, errores = _volcar(pw, args.url, path, lang)
                textos = {v for _, v in datos}
                exactos = sorted(t for t in textos if t in claves)
                heur = sorted(t for t in textos
                              if t not in claves and not _WHITELIST.match(t)
                              and _MARCAS_ES.search(t))

                if lang == "es":
                    # Corrida de control: en español TIENE que encontrar mucho. Si acá
                    # el número es bajo, la medición de portugués no significa nada.
                    print(f"\n[control ES] {path}: {len(exactos)} frases del diccionario "
                          f"visibles (denominador), {len(textos)} nodos")
                    if len(exactos) < 20:
                        print("  ⚠ El control encontró muy poco: la página no se desplegó "
                              "o falta que la base tenga relevamientos con parcelas.")
                    continue

                cubiertas = len(claves)
                print(f"\n[PT] {path}: {len(textos)} nodos de texto")
                print(f"  sin traducir (coincide con una clave del diccionario): {len(exactos)}")
                for x in exactos[: args.detalle]:
                    print(f"     ✗ {x[:110]}")
                print(f"  con pinta de español (heurística, puede tener falsos positivos): {len(heur)}")
                for x in heur[: args.detalle]:
                    print(f"     ? {x[:110]}")
                if errores:
                    print(f"  ⚠ pageerrors: {errores[:3]}")
                    total_fallos += len(errores)
                total_fallos += len(exactos)

    print(f"\n{'✓ sin fallos seguros' if not total_fallos else f'✗ {total_fallos} fallo(s)'}")
    return 1 if total_fallos else 0


if __name__ == "__main__":
    sys.exit(main())
