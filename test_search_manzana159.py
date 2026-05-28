"""
Búsqueda web (DuckDuckGo) por dirección de Manzana 159 para detectar
indicios de edificios multi-unidad: pisos, deptos, PH, oficinas.

Fuentes: ZonaProp, Argenprop, MercadoLibre, clasificados, etc.
"""

import asyncio
import json
import re
import time
from collections import defaultdict

from duckduckgo_search import DDGS

# ── Datos de Manzana 159 (de catastro_136_2C_completo.json) ──────────────

with open("catastro_136_2C_completo.json") as f:
    CATASTRO = json.load(f)

MZ159 = [r for r in CATASTRO if r["manzana"] == "159"]


# ── Patrones para detectar unidades en texto ─────────────────────────────

UNIT_PATTERNS = [
    # piso N / P.N / Nº piso
    (r'\bpiso\s*(\d{1,2})\b',                          "piso"),
    (r'\b(\d{1,2})[°º]\s*(?:piso|p\.?)\b',             "piso"),
    # dpto / departamento + letra o número
    (r'\bdpto\.?\s*([A-Z\d]{1,4})\b',                  "dpto"),
    (r'\bdepartamento\s*([A-Z\d]{1,4})\b',              "dpto"),
    # PH + letra opcional
    (r'\b(ph\s*[a-z]?)\b',                              "ph"),
    # piso+letra estilo "3B", "2A"
    (r'\b(\d{1,2})\s*[°º]?\s*([A-D])\b',               "piso_letra"),
    # oficina / local / unidad
    (r'\boficina\s*(\d{1,3})\b',                        "oficina"),
    (r'\blocal\s*(\d{1,3})\b',                          "local"),
    (r'\bunidad\s*(\d{1,3})\b',                         "unidad"),
    # "X ambientes" → evidencia de departamentos
    (r'\b(\d)\s*ambientes?\b',                          "ambientes"),
    (r'\bmonoambiente\b',                               "ambientes"),
]


def extraer_indicios(texto: str) -> list[dict]:
    """Extrae todos los indicios de multi-unidad de un bloque de texto."""
    t = texto.lower()
    hallazgos = []
    for patron, tipo in UNIT_PATTERNS:
        for m in re.finditer(patron, t, re.IGNORECASE):
            hallazgos.append({"tipo": tipo, "match": m.group(0).strip(), "raw": texto[max(0,m.start()-30):m.end()+30].strip()})
    return hallazgos


def inferir_min_unidades(indicios: list[dict]) -> int:
    """Estima unidades mínimas a partir de los indicios."""
    pisos = set()
    letras = set()
    has_ph = False

    for ind in indicios:
        m_text = ind["match"].lower()
        if ind["tipo"] == "piso":
            nums = re.findall(r'\d+', m_text)
            if nums:
                pisos.add(int(nums[0]))
        elif ind["tipo"] == "piso_letra":
            nums = re.findall(r'\d+', m_text)
            letras_m = re.findall(r'[A-Da-d]', m_text)
            if nums:
                pisos.add(int(nums[0]))
            if letras_m:
                letras.add(letras_m[0].upper())
        elif ind["tipo"] == "ph":
            has_ph = True
        elif ind["tipo"] in ("dpto", "unidad"):
            nums = re.findall(r'\d+', m_text)
            if nums and int(nums[0]) > 0:
                pisos.add(int(nums[0]))

    if not pisos and not has_ph:
        return 0

    piso_max = max(pisos) if pisos else 0
    dptos_piso = (max(ord(l) - ord('A') + 1 for l in letras) if letras else 1)

    if piso_max == 0 and has_ph:
        return 2  # mínimo 2: PB + PH
    return piso_max * dptos_piso + (dptos_piso if has_ph else 0)


# ── Motor de búsqueda ────────────────────────────────────────────────────

def buscar_direccion(calle: str, altura: str, localidad: str = "Ituzaingo") -> dict:
    """
    Realiza 2 búsquedas en DuckDuckGo para una dirección y extrae indicios.
    """
    queries = [
        f'"{calle} {altura}" {localidad} departamento piso',
        f'"{calle} {altura}" {localidad} PH alquiler venta',
    ]

    todos_indicios = []
    fuentes = []

    with DDGS() as ddgs:
        for q in queries:
            try:
                resultados = ddgs.text(q, max_results=8, region="ar-es")
                for r in (resultados or []):
                    texto = f"{r.get('title','')} {r.get('body','')}"
                    indicios = extraer_indicios(texto)
                    if indicios:
                        todos_indicios.extend(indicios)
                        fuentes.append(r.get("href", ""))
                time.sleep(0.8)  # respetar rate limit
            except Exception as e:
                print(f"    [ddg error] {e}")

    unidades_inf = inferir_min_unidades(todos_indicios)

    return {
        "indicios": todos_indicios,
        "fuentes": list(set(fuentes)),
        "unidades_inf_web": unidades_inf,
    }


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("MANZANA 159 | Búsqueda web de multi-unidad")
    print("=" * 65)

    # Deduplicar por (calle, altura)
    ya_buscadas: dict[tuple, dict] = {}
    resultados = []

    total = len(MZ159)
    for i, r in enumerate(MZ159, 1):
        calle  = r["calle"]
        altura = r["altura"] if r["altura"] != "S/N" else ""
        partida = r["partida"]
        arba_u  = r["unidades"]

        key = (calle.lower(), altura)
        print(f"[{i:02d}/{total}] {calle} {altura or 'S/N'} ...", end=" ", flush=True)

        if not altura:
            print("sin altura — salteo")
            resultados.append({**r, "web_indicios": [], "web_fuentes": [], "unidades_inf_web": 0})
            continue

        if key not in ya_buscadas:
            res = buscar_direccion(calle, altura)
            ya_buscadas[key] = res
        else:
            res = ya_buscadas[key]

        n_ind = len(res["indicios"])
        u_inf = res["unidades_inf_web"]
        print(f"{n_ind} indicios  →  ≥{u_inf} unidades")

        resultados.append({
            **r,
            "web_indicios": res["indicios"],
            "web_fuentes": res["fuentes"],
            "unidades_inf_web": u_inf,
        })

    # ── Tabla ────────────────────────────────────────────────────────────
    print()
    print("-" * 70)
    print(f"{'PARTIDA':<13} {'CALLE':<18} {'ALT':>4}  {'ARBA':>4}  {'WEB':>4}  INDICIOS")
    print("-" * 70)

    for r in resultados:
        arba_str = str(r["unidades"])
        web_str  = str(r["unidades_inf_web"]) if r["unidades_inf_web"] else "-"
        tipos    = list({ind["tipo"] for ind in r["web_indicios"]})
        tipos_str = ",".join(sorted(tipos)) if tipos else ""
        alt_str  = r["altura"] if r["altura"] != "S/N" else "S/N"
        print(f"{r['partida']:<13} {r['calle']:<18} {alt_str:>4}  {arba_str:>4}  {web_str:>4}  {tipos_str}")

    print("-" * 70)

    # ── Detalle de indicios por dirección ────────────────────────────────
    print("\n=== DETALLE DE HALLAZGOS ===\n")
    for r in resultados:
        if not r["web_indicios"]:
            continue
        print(f"  {r['calle']} {r['altura']}  (partida {r['partida']})")
        vistos = set()
        for ind in r["web_indicios"]:
            key_ind = ind["match"]
            if key_ind not in vistos:
                vistos.add(key_ind)
                print(f"    [{ind['tipo']}] «{ind['raw'][:80]}»")
        if r["web_fuentes"]:
            for u in r["web_fuentes"][:2]:
                print(f"    → {u}")
        print()

    # ── Guardar ──────────────────────────────────────────────────────────
    out = "manzana159_web_search.json"
    with open(out, "w") as f:
        json.dump(resultados, f, indent=2, ensure_ascii=False)
    print(f"Guardado en: {out}")


if __name__ == "__main__":
    main()
