"""
Corrige los datos de subparcelas de Manzana 159 usando centroides exactos
del WFS de IDERA en lugar de las coordenadas inexactas del catastro.

Parcelas afectadas:
  - 7 y 9:   datos duplicados (el click cayó dos veces sobre parcela 7)
  - 14 y 15: datos duplicados (comparten coordenadas en catastro)
  - 16, 18:  comparten coordenadas → 0 filas
  - 2, 19, 20, 21: click fuera de la parcela → 0 filas

NOTA: carto.arba.gov.ar/cartoArba agregó autenticación Keycloak.
Para usar getInfo se requiere:
  TOKEN    = JWT de Keycloak (parámetro ?token=eyJ...)
  JSESSIONID = cookie de sesión autenticada

Cómo obtenerlos:
  1. Loguearse en https://carto.arba.gov.ar/cartoArba/
  2. DevTools → Network → XHR → hacer click en el mapa
  3. Buscar el request a client/getInfo
  4. Copiar el valor del parámetro 'token' de la URL
  5. Copiar el valor de la cookie JSESSIONID de los Headers
  6. Pegar abajo o pasar por variables de entorno TOKEN y JSESSIONID
"""

import json
import math
import os
import time
import httpx

# ── Constantes ────────────────────────────────────────────────────────────

IDERA_WFS = "https://geo.arba.gov.ar/geoserver/idera/wfs"
CARTO_BASE = "https://carto.arba.gov.ar/cartoArba"

PARCELAS_A_REQUERY = {2, 9, 14, 15, 16, 18, 19, 20, 21}
PARCELA_7_PRIMERA_PARTIDA = "136071968"  # para detectar datos duplicados

# ── Credenciales (pegar acá o usar variables de entorno) ──────────────────

# JWT del parámetro ?token= de un request getInfo autenticado
TOKEN = os.environ.get("CARTO_TOKEN", "")

# Cookie JSESSIONID de la sesión autenticada
JSESSIONID = os.environ.get("CARTO_JSESSIONID", "")


# ── Geometría ─────────────────────────────────────────────────────────────

def centroid_ring(coords: list) -> tuple[float, float]:
    """Centroide simple de un anillo de coordenadas [[lon, lat], ...]."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def centroid_geometry(geom: dict) -> tuple[float, float]:
    gtype = geom["type"]
    if gtype == "Polygon":
        return centroid_ring(geom["coordinates"][0])
    if gtype == "MultiPolygon":
        # mayor anillo exterior
        best = max(geom["coordinates"], key=lambda p: len(p[0]))
        return centroid_ring(best[0])
    raise ValueError(f"Tipo de geometría no soportado: {gtype}")


# ── IDERA WFS ─────────────────────────────────────────────────────────────

def fetch_idera_centroids(client: httpx.Client) -> dict[int, tuple[float, float]]:
    """
    Devuelve {parc_num: (lon, lat)} con centroides exactos del WFS de IDERA.
    """
    print("[IDERA] Descargando geometrías de Manzana 159...", end=" ", flush=True)
    r = client.get(
        IDERA_WFS,
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "idera:Parcela",
            "CQL_FILTER": "cca LIKE '136020C0000000000000000000000159%'",
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()
    features = r.json().get("features", [])
    print(f"{len(features)} features")

    centroids: dict[int, list] = {}
    for feat in features:
        cca = feat.get("properties", {}).get("cca", "")
        if len(cca) < 42:
            continue
        parc_num = int(cca[32:39].lstrip("0") or "0")
        try:
            lon, lat = centroid_geometry(feat["geometry"])
        except Exception:
            continue
        # acumular para promediar en caso de varias features por parcela
        centroids.setdefault(parc_num, []).append((lon, lat))

    return {
        pn: (
            sum(c[0] for c in pts) / len(pts),
            sum(c[1] for c in pts) / len(pts),
        )
        for pn, pts in centroids.items()
    }


# ── Conversión EPSG:4326 → EPSG:3857 ─────────────────────────────────────

def to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 20037508.34 / math.pi
    return x, y


# ── carto.arba.gov.ar getInfo ─────────────────────────────────────────────

def init_session(client: httpx.Client) -> None:
    """Inicializa la sesión: visita el portal y opcionalmente inyecta credenciales."""
    print("[Carto] Obteniendo sesión...", end=" ", flush=True)
    client.get(f"{CARTO_BASE}/", timeout=15)
    # Inyectar JSESSIONID autenticado si fue provisto
    if JSESSIONID:
        client.cookies.set("JSESSIONID", JSESSIONID, domain="carto.arba.gov.ar")
    jsid = client.cookies.get("JSESSIONID", "?")
    auth_str = " [autenticado]" if TOKEN else " [anónimo — getInfo puede fallar]"
    print(f"JSESSIONID={jsid[:12]}...{auth_str}")


def getInfo(client: httpx.Client, lon: float, lat: float, pad: float = 0.0003) -> dict | None:
    cx, cy = to_3857(lon, lat)
    dx = dy = pad * 20037508.34 / 180
    W = H = 800
    params = {
        "x": str(W // 2),
        "y": str(H // 2),
        "epsg": "EPSG:3857",
        "xmin": str(cx - dx),
        "ymin": str(cy - dy),
        "xmax": str(cx + dx),
        "ymax": str(cy + dy),
        "layerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Manzanas,carto:Fracciones,"
            "carto:Radios,carto:Partidos"
        ),
        "layerlistnotbaselayer": "",
        "querylayerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Manzanas,carto:Fracciones,"
            "carto:Radios,carto:Partidos"
        ),
        "querylayerlistnotbaselayer": "",
        "stylelayerlistbaselayer": (
            "macizos,parcelasRurales,parcelas,subparcelas,"
            "manzanas,fracciones,radios,partidos"
        ),
        "stylelayerlistnotbaselayer": "",
        "listidslayervisibles": "",
        "listidslayersidevisibles": "",
        "listidsoperativosfisca": "",
        "listidsLayerdpout": "",
        "listLayersRRwms": "",
        "listLayersRRwfs": "",
        "scale": "846",
        "width": str(W),
        "height": str(H),
        "lon": str(cx),
        "lat": str(cy),
    }
    if TOKEN:
        params["token"] = TOKEN
    try:
        r = client.get(
            f"{CARTO_BASE}/client/getInfo",
            params=params,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{CARTO_BASE}/",
            },
            timeout=20,
        )
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                return r.json()
            print(f"    [warn] non-JSON response ({r.status_code}): {r.text[:80]}")
        elif r.status_code == 403:
            print(f"    [403] sesión expirada o token inválido")
        else:
            print(f"    [{r.status_code}]")
    except Exception as e:
        print(f"    [error] {e}")
    return None


# ── Parser de subparcelas ─────────────────────────────────────────────────

def extraer_subparcelas(data: dict) -> tuple[str, list[dict]]:
    """
    Devuelve (nomencla, [subparcelas]).
    Cada subparcela: {partida, s_terreno_m2, sp}
    """
    nomencla = ""
    for bloque in data.get("data", []):
        bd = bloque.get("data", {})

        # Capturar nomenclatura catastral
        for key, val in bd.items():
            if isinstance(val, str) and "Partido:" in val:
                nomencla = val.strip()

        tabla = bd.get("table", {})
        if not tabla:
            continue
        first = next(iter(tabla.values()), {})
        if "partida" not in first:
            continue

        rows = []
        for _, row in sorted(tabla.items(), key=lambda x: int(x[0])):
            if "partida" in row:
                rows.append({
                    "partida": "136" + str(row["partida"]).zfill(6),
                    "s_terreno_m2": str(row.get("s_terreno", "")),
                    "sp": str(row.get("sp", "")),
                })
        return nomencla, rows

    return nomencla, []


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    with open("ituzaingo_manzana_159.json") as f:
        doc = json.load(f)

    # Índice rápido por parcela_num
    carto_idx: dict[int, dict] = {
        e["parcela_num"]: e for e in doc["subparcelas_carto"]
    }

    with httpx.Client(follow_redirects=True) as client:

        # 1. Centroides exactos de IDERA
        idera_cents = fetch_idera_centroids(client)
        print(f"   Centroides obtenidos: {sorted(idera_cents.keys())}\n")

        # 2. Sesión carto
        init_session(client)
        print()

        # 3. Re-queries
        nuevos: dict[int, dict] = {}

        for pn in sorted(PARCELAS_A_REQUERY):
            if pn not in idera_cents:
                print(f"  Parcela {pn:2d}: sin centroide IDERA — salteo")
                continue

            lon, lat = idera_cents[pn]
            print(f"  Parcela {pn:2d}  ({lat:.6f}, {lon:.6f})...", end=" ", flush=True)

            data = getInfo(client, lon, lat)
            if data is None:
                print("sin respuesta")
                nuevos[pn] = {"nomencla": "", "n_subparcelas": -1, "subparcelas": []}
                continue

            nomencla, rows = extraer_subparcelas(data)
            n_sub = len(rows) - 1 if rows else 0

            # Detectar duplicado con parcela 7
            primera = rows[0]["partida"] if rows else ""
            if pn == 9 and primera == PARCELA_7_PRIMERA_PARTIDA:
                print(f"DUPLICADO de parcela 7 (misma primera partida)")
                nuevos[pn] = {"nomencla": nomencla, "n_subparcelas": -1, "subparcelas": []}
            else:
                print(f"{len(rows)} filas  →  n_sub={n_sub}  {nomencla[:50]}")
                nuevos[pn] = {"nomencla": nomencla, "n_subparcelas": n_sub, "subparcelas": rows}

            time.sleep(0.5)

        # 4. Aplicar cambios a subparcelas_carto
        print()
        print("Aplicando cambios...")
        for pn, nuevo in nuevos.items():
            if pn not in carto_idx:
                continue
            entry = carto_idx[pn]
            old_n = entry["n_subparcelas"]
            entry["nomencla"] = nuevo["nomencla"] or entry.get("nomencla", "")
            entry["n_subparcelas"] = nuevo["n_subparcelas"]
            entry["subparcelas"] = nuevo["subparcelas"]
            print(f"  Parcela {pn:2d}: {old_n} → {nuevo['n_subparcelas']}")

        # 5. Agregar n_uf_carto a parcelas_por_calle
        for pentry in doc["parcelas_por_calle"]:
            pn = pentry["parcela"]
            ce = carto_idx.get(pn)
            if ce is None:
                pentry["n_uf_carto"] = None
                continue
            ns = ce["n_subparcelas"]
            if ns == -1:
                pentry["n_uf_carto"] = None
            elif ns == 0 and ce["subparcelas"]:
                # 1 fila = 1 unidad (el propio predio sin subparcelas)
                pentry["n_uf_carto"] = 1
            else:
                pentry["n_uf_carto"] = max(ns, 1) if ce["subparcelas"] else None

        # 6. Actualizar resumen
        total_carto = sum(
            e["n_subparcelas"]
            for e in doc["subparcelas_carto"]
            if e["n_subparcelas"] > 0
        )
        doc["resumen"]["total_uf_carto"] = total_carto

        # 7. Guardar
        with open("ituzaingo_manzana_159.json", "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        print(f"\nGuardado. total_uf_carto={total_carto}")

        # 8. Tabla final
        print()
        print("=" * 58)
        print(f"{'DOMICILIO':<26}  {'ARBA':>6}  {'CARTO':>6}")
        print("=" * 58)
        for pentry in doc["parcelas_por_calle"]:
            arba = pentry["n_uf"]
            carto = pentry.get("n_uf_carto")
            carto_str = str(carto) if carto is not None else "?"
            flag = ""
            if carto and carto > 1:
                flag = " ◄"
            print(f"  {pentry['domicilio']:<24}  {arba:>6}  {carto_str:>6}{flag}")
        print("=" * 58)
        print(f"  {'TOTAL ARBA':<24}  {sum(p['n_uf'] for p in doc['parcelas_por_calle']):>6}")
        print(f"  {'TOTAL CARTO (confirmados)':<24}  {total_carto:>6}")


if __name__ == "__main__":
    main()
