"""
Test Manzana 159 — Partido 136 (Ituzaingó) — Circ 2 — Secc C
Cruza datos ARBA con consulta a Edenor para detectar multi-unidad.

Lógica de inferencia:
  Si un abonado vive en depto "6B" → al menos 6 pisos × 2 dptos/piso = 12 unidades mínimas.
  La letra del depto indica cuántos dptos hay por piso (A=1, B=2, C=3, ...).
"""

import asyncio
import json
import re
import httpx

# ──────────────────────────────────────────────────────────────
# 1. DATOS DE MANZANA 159 (desde catastro ARBA)
# ──────────────────────────────────────────────────────────────

PARCELAS_MANZANA_159 = [
    {"partida": "136000096", "calle": "Gascon",        "altura": "160"},
    {"partida": "136000102", "calle": "Gascon",        "altura": "110"},
    {"partida": "136000104", "calle": "Gascon",        "altura": "160"},
    {"partida": "136000105", "calle": "24 de Octubre", "altura": "20"},
    {"partida": "136000106", "calle": "24 de Octubre", "altura": "20"},
    {"partida": "136000107", "calle": "Gascon",        "altura": "20"},
    {"partida": "136000108", "calle": "Medrano",       "altura": "20"},
    {"partida": "136071968", "calle": "Medrano",       "altura": "20"},
    {"partida": "136000110", "calle": "Medrano",       "altura": "20"},
    {"partida": "136000122", "calle": "Medrano",       "altura": "71"},   # ← OSM: apartments
    {"partida": "136000123", "calle": "Medrano",       "altura": "71"},   # ← OSM: apartments
    {"partida": "136000125", "calle": "Medrano",       "altura": None},   # ← OSM: apartments (9m)
    {"partida": "136000126", "calle": "Medrano",       "altura": None},   # ← OSM: apartments (11m)
    {"partida": "136000128", "calle": "Medrano",       "altura": "160"},
    {"partida": "136000129", "calle": "Medrano",       "altura": "160"},
    {"partida": "136000136", "calle": "Medrano",       "altura": "160"},
    {"partida": "136000137", "calle": "Medrano",       "altura": "160"},
    {"partida": "136000138", "calle": "Medrano",       "altura": "160"},
    {"partida": "136000139", "calle": "Gascon",        "altura": "160"},
    {"partida": "136000140", "calle": "Gascon",        "altura": "160"},
]

# ──────────────────────────────────────────────────────────────
# 2. INFERENCIA DE UNIDADES POR IDENTIFICADOR DE DEPTO
# ──────────────────────────────────────────────────────────────

def inferir_edificio(depto_raw: str) -> dict:
    """
    Dado un identificador de depto, infiere el tamaño mínimo del edificio.

    Ejemplos:
      "6B"   → 6 pisos × 2 dptos/piso = 12 unidades mínimas
      "3C"   → 3 pisos × 3 dptos/piso = 9 unidades mínimas
      "PB A" → PB con al menos 1 depto (edificio pequeño)
      "12"   → al menos 12 pisos, 1 depto/piso mínimo
    """
    d = depto_raw.strip().upper().replace(" ", "")

    # Patrón: número + letra  →  "6B", "3A", "12D"
    m = re.match(r'^(\d+)([A-Z])$', d)
    if m:
        piso = int(m.group(1))
        letra = m.group(2)
        dptos_piso = ord(letra) - ord('A') + 1   # A=1, B=2, C=3...
        # Todos los pisos (1 a piso) × dptos/piso + PB estimado
        unidades_min = piso * dptos_piso + dptos_piso
        return {
            "pisos_min": piso,
            "dptos_por_piso_min": dptos_piso,
            "unidades_min": unidades_min,
            "nota": f"{piso} pisos × {dptos_piso} dptos + PB estimado",
        }

    # Patrón: sólo letra  →  "A", "B"  (suelen ser PB)
    m_letra = re.match(r'^([A-Z])$', d)
    if m_letra:
        letra = m_letra.group(1)
        dptos_piso = ord(letra) - ord('A') + 1
        return {
            "pisos_min": 1,
            "dptos_por_piso_min": dptos_piso,
            "unidades_min": dptos_piso,
            "nota": "planta baja o sin piso indicado",
        }

    # Patrón: PB + letra  →  "PBA", "PBB"
    m_pb = re.match(r'^PB([A-Z]?)$', d)
    if m_pb:
        letra = m_pb.group(1)
        dptos_piso = ord(letra) - ord('A') + 1 if letra else 1
        return {
            "pisos_min": 0,
            "dptos_por_piso_min": dptos_piso,
            "unidades_min": dptos_piso,
            "nota": "planta baja",
        }

    # Patrón: sólo número  →  "6", "12"
    m_num = re.match(r'^(\d+)$', d)
    if m_num:
        piso = int(m_num.group(1))
        return {
            "pisos_min": piso,
            "dptos_por_piso_min": 1,
            "unidades_min": piso,
            "nota": "sin letra, 1 depto/piso mínimo",
        }

    return {"pisos_min": None, "dptos_por_piso_min": None, "unidades_min": 1, "nota": "no parseable"}


# ──────────────────────────────────────────────────────────────
# 3. CLIENTE EDENOR
# ──────────────────────────────────────────────────────────────

EDENOR_BASE = "https://www.edenor.com.ar"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-AR,es;q=0.9",
    "Origin": EDENOR_BASE,
    "Referer": EDENOR_BASE + "/",
}


async def _get_nextjs_build_id(client: httpx.AsyncClient) -> str | None:
    """Extrae el buildId del HTML de Edenor para construir rutas _next/data."""
    try:
        r = await client.get(EDENOR_BASE + "/", headers=HEADERS_BASE, timeout=10)
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
        # Alternativa: extraer de src de script
        m2 = re.search(r'/_next/static/([a-zA-Z0-9_-]+)/_buildManifest', r.text)
        return m2.group(1) if m2 else None
    except Exception:
        return None


async def buscar_suministros_edenor(
    client: httpx.AsyncClient,
    calle: str,
    altura: str | None,
    build_id: str | None,
) -> dict:
    """
    Intenta múltiples endpoints de Edenor para encontrar cuentas en una dirección.
    Devuelve {"ok": bool, "endpoint": str, "raw": ..., "cuentas": [...]}
    """
    altura_str = str(altura) if altura else ""

    endpoints = [
        # Estrategia 1: API route Next.js directa
        ("GET", f"/api/suministros/buscar-por-direccion",
         {"calle": calle, "numero": altura_str, "localidad": "Ituzaingó"}),

        # Estrategia 2: Autocomplete de calles (para validar que la calle existe)
        ("GET", f"/api/calles/autocomplete",
         {"q": calle, "localidad": "Ituzaingó"}),

        # Estrategia 3: Búsqueda por NNP (Nomenclador)
        ("GET", f"/api/nnp/buscar",
         {"calle": calle, "numero": altura_str}),

        # Estrategia 4: Next.js data route (requiere build_id)
        *([("GET", f"/_next/data/{build_id}/suministros/buscar.json",
            {"calle": calle, "numero": altura_str})] if build_id else []),

        # Estrategia 5: Endpoint de solicitud de nueva conexión
        ("GET", f"/api/tramites/nueva-conexion/validar-direccion",
         {"calle": calle, "numero": altura_str, "localidad": "Ituzaingo"}),

        # Estrategia 6: REST genérico con localidad
        ("GET", f"/api/v1/suministros",
         {"street": calle, "number": altura_str}),
    ]

    for method, path, params in endpoints:
        url = EDENOR_BASE + path
        try:
            if method == "GET":
                r = await client.get(url, params=params, headers=HEADERS_BASE, timeout=10)
            else:
                r = await client.post(url, json=params, headers=HEADERS_BASE, timeout=10)

            ct = r.headers.get("content-type", "")
            if "json" in ct and r.status_code < 400:
                try:
                    data = r.json()
                    return {"ok": True, "endpoint": path, "status": r.status_code, "raw": data, "cuentas": _extraer_cuentas(data)}
                except Exception:
                    pass
            # Si devuelve HTML para cualquier ruta → ese endpoint no existe en este build
        except httpx.TimeoutException:
            print(f"    [timeout] {path}")
        except Exception as e:
            print(f"    [error]   {path}: {e}")

    return {"ok": False, "endpoint": None, "status": None, "raw": None, "cuentas": []}


def _extraer_cuentas(data) -> list:
    """
    Intenta extraer lista de cuentas/NIS de la respuesta JSON de Edenor.
    La estructura puede variar según el endpoint.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("cuentas", "suministros", "nis", "results", "data", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def _parsear_depto_de_cuenta(cuenta: dict) -> str | None:
    """Extrae el identificador de depto de un objeto de cuenta Edenor."""
    for key in ("piso", "depto", "departamento", "unidad", "piso_depto", "pisoDep"):
        v = cuenta.get(key)
        if v:
            return str(v).strip()
    # También buscar en dirección completa
    direccion = cuenta.get("direccion", cuenta.get("domicilio", ""))
    m = re.search(r'\b(\d+[A-Z]|PB[A-Z]?)\b', str(direccion).upper())
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────
# 4. MAIN
# ──────────────────────────────────────────────────────────────

async def main():
    print("=" * 65)
    print("MANZANA 159 | Partido 136 Ituzaingó — Circ 2 — Secc C")
    print("Cruz catastro ARBA ↔ suministros Edenor")
    print("=" * 65)

    async with httpx.AsyncClient(follow_redirects=True) as client:

        # ── Discovery de buildId Edenor ──────────────────────────────
        print("\n[Edenor] Obteniendo build ID del portal...", end=" ", flush=True)
        build_id = await _get_nextjs_build_id(client)
        print(f"{'OK: ' + build_id if build_id else 'no encontrado'}")

        # ── Procesar cada parcela ────────────────────────────────────
        print()
        resultados = []

        # Deduplicar: una consulta por (calle, altura) única
        ya_consultadas = {}

        for p in PARCELAS_MANZANA_159:
            calle  = p["calle"]
            altura = p["altura"]
            key    = (calle.lower(), str(altura).lower())

            if key not in ya_consultadas:
                print(f"  → Edenor: {calle} {altura or 'S/N'} ...", end=" ", flush=True)
                resultado = await buscar_suministros_edenor(client, calle, altura, build_id)
                ya_consultadas[key] = resultado
                estado = f"✓ {resultado['endpoint']}" if resultado["ok"] else "✗ sin endpoint JSON"
                print(estado)
            else:
                resultado = ya_consultadas[key]

            cuentas  = resultado.get("cuentas", [])
            unidades_edenor = len(cuentas) if cuentas else None

            # Inferir por número de depto si hay cuentas con depto identificado
            inferencia_max = None
            for cuenta in cuentas:
                depto = _parsear_depto_de_cuenta(cuenta)
                if depto:
                    inf = inferir_edificio(depto)
                    u   = inf.get("unidades_min", 1)
                    if inferencia_max is None or u > inferencia_max:
                        inferencia_max = u

            resultados.append({
                **p,
                "edenor_ok"       : resultado["ok"],
                "cuentas_raw"     : len(cuentas),
                "unidades_edenor" : unidades_edenor,
                "unidades_inf"    : inferencia_max,
                "arba_unidades"   : 1,  # ARBA dice 1 para todas en esta zona
            })

        # ── Tabla de resultados ──────────────────────────────────────
        print()
        print("-" * 65)
        print(f"{'PARTIDA':<13} {'CALLE':<15} {'ALT':>4}  "
              f"{'ARBA':>4}  {'EDENOR':>6}  {'INF':>4}  OSM")
        print("-" * 65)

        osm_flag = {
            "136000122": "🏢 apartments",
            "136000123": "🏢 apartments",
            "136000125": "🏢 apartments",
            "136000126": "🏢 apartments",
        }

        for r in resultados:
            edenor_str = str(r["unidades_edenor"]) if r["unidades_edenor"] is not None else "?"
            inf_str    = str(r["unidades_inf"])    if r["unidades_inf"]    is not None else "-"
            osm_str    = osm_flag.get(r["partida"], "")
            alt_str    = str(r["altura"]) if r["altura"] else "S/N"
            print(
                f"{r['partida']:<13} {r['calle']:<15} {alt_str:>4}  "
                f"{r['arba_unidades']:>4}  {edenor_str:>6}  {inf_str:>4}  {osm_str}"
            )

        print("-" * 65)

        # ── Resumen ─────────────────────────────────────────────────
        total_arba   = sum(r["arba_unidades"] for r in resultados)
        total_edenor = sum(r["unidades_edenor"] or 0 for r in resultados)
        total_inf    = sum(r["unidades_inf"]    or 0 for r in resultados)

        print(f"\n{'ARBA (catastro)':.<35} {total_arba} unidades")
        print(f"{'Edenor (suministros)':.<35} {total_edenor if total_edenor else '?'} cuentas")
        print(f"{'Inferencia por nro depto':.<35} {total_inf if total_inf else 'N/D'} unidades mínimas")

        if not any(r["edenor_ok"] for r in resultados):
            print("""
⚠  Edenor no devolvió JSON en ningún endpoint.
   El portal es una SPA (Next.js) — las llamadas reales se hacen
   desde el browser. Opciones para la próxima iteración:
     A) Capturar las requests con DevTools → Network tab → filtrar XHR
        mientras se hace una búsqueda manual en www.edenor.com.ar
        y copiar el endpoint real aquí.
     B) Usar Playwright para ejecutar el JS del portal.
""")

        # ── Guardar JSON ─────────────────────────────────────────────
        with open("manzana159_resultado.json", "w") as f:
            json.dump(resultados, f, indent=2, ensure_ascii=False)
        print("Resultado guardado en: manzana159_resultado.json")


if __name__ == "__main__":
    asyncio.run(main())
