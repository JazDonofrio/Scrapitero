"""
Consulta de subparcelas por manzana — carto.arba.gov.ar + IDERA WFS

Fuentes de datos:
  1. IDERA WFS (geo.arba.gov.ar)     — geometrías y centroides de parcelas
  2. Google Maps Geocoding API        — dirección (calle y número) por coordenadas
     Fallback: Nominatim (OSM)        — cuando GOOGLE_API_KEY no está configurada
  3. carto.arba.gov.ar / getInfo      — subparcelas, partidas, superficies

Variables de entorno requeridas:
  CARTO_JSESSIONID   Cookie de sesión autenticada en carto.arba.gov.ar
  GOOGLE_API_KEY     Clave de Google Maps Geocoding API (opcional, mejora direcciones)

Cómo obtener el JSESSIONID:
  1. Iniciar sesión en https://carto.arba.gov.ar/cartoArba/
  2. DevTools → Application → Cookies → carto.arba.gov.ar → JSESSIONID

Uso:
  python3 consulta_manzana.py
  CARTO_JSESSIONID="ABC..." GOOGLE_API_KEY="AIza..." python3 consulta_manzana.py
"""

import csv
import json
import math
import os
import re
import time
import httpx

# ── Endpoints ─────────────────────────────────────────────────────────────────

IDERA_WFS  = "https://geo.arba.gov.ar/geoserver/idera/wfs"
CARTO_BASE = "https://carto.arba.gov.ar/cartoArba"
GMAPS_GEO  = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM  = "https://nominatim.openstreetmap.org/reverse"

# ── Configuración ─────────────────────────────────────────────────────────────

COCHERA_M2         = 25   # s_m2 < 25 → cochera;  s_m2 >= 25 → UF;  s_m2 == 0 → ignorar
JSESSIONID_DEFAULT = "C7BDDC7E7181BF3C24B69C6AAA324EA2"

GOOGLE_API_KEY  = os.environ.get("GOOGLE_API_KEY", "AIzaSyBcBveB2soQ221-i4iEFgb1-l9hh1GPkuQ")
CARTO_JSESSIONID = os.environ.get("CARTO_JSESSIONID", JSESSIONID_DEFAULT)


# ── Input ─────────────────────────────────────────────────────────────────────

def pedir_nomenclatura() -> tuple[str, str, str, list[str]]:
    print("Nomenclatura catastral (Enter = defaults: Partido 136 / Circ 2 / Secc C / Mza 159):")
    pdo     = input("  Partido        [136] : ").strip() or "136"
    circ    = input("  Circunscripción  [2] : ").strip() or "2"
    secc    = input("  Sección          [C] : ").strip().upper() or "C"
    raw_mza = input("  Manzana(s)     [159] : ").strip() or "159"
    mzas    = [m.strip() for m in raw_mza.split(",") if m.strip()]
    return pdo, circ, secc, mzas


# ── CCA ───────────────────────────────────────────────────────────────────────

def cca_prefix(pdo: str, circ: str, secc: str, mza: str) -> str:
    return pdo.zfill(3) + circ.zfill(2) + "0" + secc.upper() + "0" * 21 + mza.zfill(4)


def parc_num_from_cca(cca: str) -> int:
    return int(cca[32:39].lstrip("0") or "0")


# ── IDERA WFS: geometrías de parcelas ─────────────────────────────────────────

def fetch_idera(client: httpx.Client, prefix: str) -> dict[tuple, dict]:
    """
    Devuelve {(parc_num, sufijo): {lon, lat, pdas}} con centroides calculados.
    El sufijo es la letra al final del CCA (ej: 'G', 'M', 'A', '').
    Parcelas con múltiples sub-registros en IDERA (ej: 2G y 2M) se tratan
    como entradas separadas.
    """
    r = client.get(
        IDERA_WFS,
        params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "idera:Parcela",
            "CQL_FILTER": f"cca LIKE '{prefix}%'",
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()

    parcelas: dict[tuple, dict] = {}
    for feat in r.json().get("features", []):
        props = feat.get("properties", {})
        cca   = props.get("cca", "")
        if len(cca) < 39:
            continue
        pn     = parc_num_from_cca(cca)
        sufijo = cca[39:].strip("0") if len(cca) > 39 else ""
        key    = (pn, sufijo)
        geom   = feat.get("geometry")
        if not geom:
            continue
        coords = (
            geom["coordinates"][0][0]
            if geom["type"] == "MultiPolygon"
            else geom["coordinates"][0]
        )
        lon = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
        if key not in parcelas:
            parcelas[key] = {"lon": lon, "lat": lat, "pdas": []}
        parcelas[key]["pdas"].append(props.get("pda", ""))

    return parcelas


# ── Geocodificación inversa ────────────────────────────────────────────────────

def _addr_from_google(lat: float, lon: float) -> str:
    """
    Consulta Google Maps Geocoding API con hasta 3 intentos ante REQUEST_DENIED.
    Retorna "Calle Número" o "" si no hay resultado o la key no está configurada.
    Imprime el detalle de la respuesta para diagnóstico.
    """
    if not GOOGLE_API_KEY:
        print("        Google Maps: GOOGLE_API_KEY no configurada — usando Nominatim")
        return ""

    for intento in range(1, 4):
        try:
            r = httpx.get(
                GMAPS_GEO,
                params={"latlng": f"{lat},{lon}", "key": GOOGLE_API_KEY},
                timeout=10,
            )
            d       = r.json()
            status  = d.get("status")
            results = d.get("results", [])

            if status == "REQUEST_DENIED":
                print(f"        Google Maps: REQUEST_DENIED (intento {intento}/3) — {d.get('error_message', '')!r}")
                if intento < 3:
                    time.sleep(1.5)
                    continue
                return ""

            if status != "OK" or not results:
                print(f"        Google Maps: status={status!r}  error={d.get('error_message', '—')!r}")
                return ""

            r0    = results[0]
            comps = {
                t: c["long_name"]
                for c in r0.get("address_components", [])
                for t in c["types"]
            }
            road = comps.get("route", "")
            num  = comps.get("street_number", "")
            addr = f"{road} {num}".strip() if road else ""

            print(f"        Google Maps: {r0.get('formatted_address')!r}  →  {addr!r}")
            return addr

        except Exception as exc:
            print(f"        Google Maps: excepción (intento {intento}/3) — {exc}")
            if intento < 3:
                time.sleep(1.5)

    return ""


def _addr_from_nominatim(client: httpx.Client, lat: float, lon: float) -> str:
    """
    Consulta Nominatim (OpenStreetMap) como fallback.
    Retorna "Calle Número" o solo "Calle" si OSM no tiene la altura.
    """
    try:
        r = client.get(
            NOMINATIM,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 18},
            headers={"User-Agent": "Scrapitero/1.0"},
            timeout=10,
        )
        if r.status_code == 200:
            d    = r.json()
            addr = d.get("address", {})
            road = (
                addr.get("road")
                or addr.get("pedestrian")
                or addr.get("footway")
                or ""
            )
            num = addr.get("house_number", "")
            return f"{road} {num}".strip() if road else d.get("display_name", "")[:50]
    except Exception:
        pass
    return ""


def geocodificar(client: httpx.Client, lat: float, lon: float) -> tuple[str, str]:
    """
    Intenta Google Maps primero; si falla o no hay key, usa Nominatim.
    Retorna (dirección, fuente) donde fuente es 'google' | 'nominatim' | 'sin datos'.
    """
    addr = _addr_from_google(lat, lon)
    if addr:
        return addr, "google"

    if GOOGLE_API_KEY:
        print(f"        Google Maps: sin resultado — intentando Nominatim")

    time.sleep(1.1)   # rate limit de Nominatim (1 req/s)
    addr = _addr_from_nominatim(client, lat, lon)
    if addr:
        return addr, "nominatim"

    return "", "sin datos"


# ── carto.arba.gov.ar ─────────────────────────────────────────────────────────

def to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 20037508.34 / math.pi
    return x, y


def getInfo(client: httpx.Client, lon: float, lat: float, token: str = "", pad: float = 0.0004) -> dict | None:
    cx, cy = to_3857(lon, lat)
    dx = dy = pad * 20037508.34 / 180
    W = H = 800
    params = {
        "x": str(W // 2), "y": str(H // 2), "epsg": "EPSG:3857",
        "xmin": str(cx - dx), "ymin": str(cy - dy),
        "xmax": str(cx + dx), "ymax": str(cy + dy),
        "layerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Equipamiento_Comunitario,"
            "carto:Espacio Verde,carto:Cotas,carto:Cotas_sp,"
            "carto:Seccion,carto:Circunscripcion,carto:Partidos,"
            "carto:Calles,carto:Limites,carto:Cuerpos_de_agua,"
            "carto:Red_Ferroviaria,carto:ign_cursos_de_agua"
        ),
        "layerlistnotbaselayer": "",
        "querylayerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Cotas,carto:Cotas_sp,"
            "carto:Seccion,carto:Circunscripcion,carto:Partidos"
        ),
        "querylayerlistnotbaselayer": "",
        "stylelayerlistbaselayer": (
            "carto:Carto_Macizos,carto:Carto_Parcelas_Rurales,"
            "carto:Carto_Parcelas,carto:Carto_Subparcelas,"
            "carto:Equipamiento_Comunitario,carto:Carto_Espacio_Verde,"
            "carto:empty,carto:empty,carto:Carto_Seccion,"
            "carto:Carto_Circunscripcion,carto:Partidos,carto:empty,"
            "carto:Limite,carto:Cuerpos_de_agua,"
            "carto:Red_Ferrocarril,carto:Cursos_de_agua"
        ),
        "stylelayerlistnotbaselayer": "",
        "listidslayervisibles": "60", "listidslayersidevisibles": "",
        "listidsoperativosfisca": "", "listidsLayerdpout": "",
        "listLayersRRwms": "", "listLayersRRwfs": "",
        "scale": "846", "width": str(W), "height": str(H),
        "lon": str(cx), "lat": str(cy),
    }
    if token:
        params["token"] = token
    try:
        r = client.get(
            f"{CARTO_BASE}/client/getInfo", params=params,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{CARTO_BASE}/"},
            timeout=25,
        )
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" in ct:
            return r.json()
        print(f"      getInfo HTTP {r.status_code} — {r.text[:120]!r}")
        if r.status_code in (401, 403):
            print("      → JSESSIONID inválido o sesión expirada")
    except Exception as exc:
        print(f"      getInfo excepción: {exc}")
    return None


def parsear_subparcelas(data: dict) -> tuple[str, str, str, list[dict]]:
    """
    Extrae del response de getInfo:
      - nomencla:   cadena completa de la fila 'Abierta' (Nomenclatura Catastral)
      - domicilio:  dirección registrada en carto (sección 'Direccion', si existe)
      - parc_label: texto después de 'Parcela:' en la nomenclatura
      - rows:       lista de subparcelas [{partida, s_m2, sp}]
    """
    nomencla   = ""
    domicilio  = ""
    parc_label = ""
    rows_out: list[dict] = []

    for bloque in data.get("data", []):
        title = str(bloque.get("title", "")).lower()
        bd    = bloque.get("data", {})
        if not isinstance(bd, dict):
            continue

        if "nomenclatura" in title:
            for row in bd.get("table", {}).values():
                if row.get("clave") == "Abierta":
                    nomencla = row.get("valor", "").strip()
                    m = re.search(r'Parcela:\s*(\S+)', nomencla, re.IGNORECASE)
                    if m:
                        parc_label = m.group(1).strip()
                    break

        elif "valores" in title or "básic" in title or "basic" in title:
            tabla = bd.get("table", {})
            if not tabla or "partida" not in next(iter(tabla.values()), {}):
                continue
            for _, row in sorted(tabla.items(), key=lambda x: int(x[0])):
                if "partida" not in row:
                    continue
                try:
                    s_m2 = int(float(row.get("s_terreno", 0) or 0))
                except (ValueError, TypeError):
                    s_m2 = 0
                rows_out.append({
                    "partida": "136" + str(row["partida"]).zfill(6),
                    "s_m2": s_m2,
                    "sp": str(row.get("sp", "")),
                })

        elif "direcci" in title:
            partes = [v.strip() for v in bd.values() if isinstance(v, str) and v.strip()]
            domicilio = " ".join(partes)

    return nomencla, domicilio, parc_label, rows_out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    pdo, circ, secc, mzas = pedir_nomenclatura()

    print()
    print(f"Configuración:")
    print(f"  CARTO_JSESSIONID : {'(env var)' if os.environ.get('CARTO_JSESSIONID') else '(valor por defecto)'} {CARTO_JSESSIONID[:8]}...")
    print(f"  GOOGLE_API_KEY   : {'configurada (' + GOOGLE_API_KEY[:8] + '...)' if GOOGLE_API_KEY else 'NO configurada — se usará Nominatim'}")
    print(f"  Geocodificación  : {'Google Maps → fallback Nominatim' if GOOGLE_API_KEY else 'Nominatim'}")
    print()

    resultados = []
    token = os.environ.get("CARTO_TOKEN", "")

    with httpx.Client(follow_redirects=True) as client:

        # Inicializar sesión carto
        client.get(f"{CARTO_BASE}/", timeout=15)
        client.cookies.set("JSESSIONID", CARTO_JSESSIONID, domain="carto.arba.gov.ar")

        for mza in mzas:
            prefix = cca_prefix(pdo, circ, secc, mza)
            print(f"══ Manzana {mza}  (Partido {pdo} / Circ {circ} / Secc {secc}) ══")

            # 1. IDERA WFS
            print(f"  [1/3] IDERA WFS — geometrías de parcelas...", end=" ", flush=True)
            parcelas = fetch_idera(client, prefix)
            print(f"{len(parcelas)} parcelas encontradas")
            if not parcelas:
                print("       Sin resultados. Verificá la nomenclatura.")
                continue

            # 2. Geocodificación
            print(f"  [2/3] Geocodificación inversa ({('Google Maps' if GOOGLE_API_KEY else 'Nominatim')})...")
            for (pn, sufijo), info in sorted(parcelas.items()):
                print(f"      Parcela {pn:3d}{sufijo}  ({info['lat']:.5f}, {info['lon']:.5f})")
                addr, fuente = geocodificar(client, info["lat"], info["lon"])
                info["direccion"] = addr
                info["dir_fuente"] = fuente
                print(f"        → [{fuente}] {addr!r}")

            # 3. carto getInfo
            print(f"  [3/3] carto.arba.gov.ar — subparcelas y nomenclatura...")
            prev_partidas = None

            for (pn, sufijo), info in sorted(parcelas.items()):
                lon, lat = info["lon"], info["lat"]
                nombre   = ""
                print(f"      Parcela {pn:3d}{sufijo}  ({lat:.5f}, {lon:.5f})...", end=" ", flush=True)

                data = getInfo(client, lon, lat, token=token)
                if data is None:
                    print("sin respuesta")
                    resultados.append({
                        "manzana": mza, "parcela": pn, "sufijo": sufijo, "nombre": nombre,
                        "lat": lat, "lon": lon,
                        "direccion": info["direccion"], "dir_fuente": info["dir_fuente"],
                        "n_partidas": None, "n_cocheras": None, "n_uf": None,
                        "nomencla": "",
                    })
                    time.sleep(0.4)
                    continue

                nomencla, domicilio_carto, parc_label, rows = parsear_subparcelas(data)

                nombre = parc_label  # vacío si carto no lo devuelve

                if domicilio_carto:
                    direccion = domicilio_carto
                    fuente    = "carto"
                else:
                    direccion = info["direccion"]
                    fuente    = info["dir_fuente"]

                partidas_actuales = [r["partida"] for r in rows]
                if partidas_actuales and partidas_actuales == prev_partidas:
                    print(f"DUPLICADO (click cayó en parcela anterior)")
                    resultados.append({
                        "manzana": mza, "parcela": pn, "sufijo": sufijo, "nombre": nombre,
                        "lat": lat, "lon": lon,
                        "direccion": direccion, "dir_fuente": fuente,
                        "n_partidas": None, "n_cocheras": None, "n_uf": None,
                        "nomencla": nomencla,
                    })
                    time.sleep(0.4)
                    continue
                prev_partidas = partidas_actuales

                cocheras = sum(1 for r in rows if 0 < r["s_m2"] < COCHERA_M2)
                uf       = sum(1 for r in rows if r["s_m2"] >= COCHERA_M2)
                total    = cocheras + uf

                print(f"{len(rows)} filas → {total} subparcelas | cocheras={cocheras}  UF={uf}  dir=[{fuente}] {direccion!r}")

                resultados.append({
                    "manzana": mza, "parcela": pn, "sufijo": sufijo, "nombre": nombre,
                    "lat": lat, "lon": lon,
                    "direccion": direccion, "dir_fuente": fuente,
                    "n_partidas": total, "n_cocheras": cocheras, "n_uf": uf,
                    "nomencla": nomencla,
                })
                time.sleep(0.4)

    # ── Tabla resumen ──────────────────────────────────────────────────────────
    W = 100
    print()
    print("=" * W)
    print(
        f"  {'#':>3}  {'MZA':>4}  {'PARC':>4}  {'NOMBRE':<6}  "
        f"{'DIRECCIÓN':<30}  {'COORD':<21}  "
        f"{'PARTIDAS':>8}  {'COCH':>5}  {'UF':>5}"
    )
    print("=" * W)

    tot_partidas = tot_coch = tot_uf = 0
    mza_actual   = None
    contador_mza = 0

    for r in resultados:
        if r["manzana"] != mza_actual:
            if mza_actual is not None:
                print("-" * W)
            mza_actual   = r["manzana"]
            contador_mza = 0

        contador_mza += 1
        p_str  = str(r["n_partidas"]) if r["n_partidas"] is not None else "?"
        c_str  = str(r["n_cocheras"]) if r["n_cocheras"] is not None else "?"
        uf_str = str(r["n_uf"])       if r["n_uf"]       is not None else "?"
        coord  = f"{r['lat']:.5f},{r['lon']:.5f}"
        flag   = " ◄" if (r["n_uf"] or 0) > 1 else ""
        print(
            f"  {contador_mza:>3}  {r['manzana']:>4}  {r['parcela']:>4}  {r['nombre']:<6}  "
            f"{r['direccion']:<30}  {coord:<21}  "
            f"{p_str:>8}  {c_str:>5}  {uf_str:>5}{flag}"
        )
        if r["n_partidas"] is not None:
            tot_partidas += r["n_partidas"]
            tot_coch     += r["n_cocheras"]
            tot_uf       += r["n_uf"]

    print("=" * W)
    print(
        f"  {'':>3}  {'':>4}  {'':>4}  {'TOTAL':<6}  "
        f"{'':30}  {'':21}  "
        f"{tot_partidas:>8}  {tot_coch:>5}  {tot_uf:>5}"
    )
    print()

    # ── Guardar archivos ───────────────────────────────────────────────────────
    tag = "_".join(mzas) if len(mzas) <= 5 else f"{mzas[0]}_y{len(mzas)-1}mas"

    # JSON
    out_json = f"manzana{tag}_subparcelas.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "manzanas": mzas, "partido": pdo, "circ": circ, "secc": secc,
            "cochera_umbral_m2": COCHERA_M2,
            "parcelas": resultados,
        }, f, indent=2, ensure_ascii=False)

    # CSV
    out_csv = f"manzana{tag}_subparcelas.csv"
    campos  = ["manzana", "parcela", "nombre", "direccion", "lat", "lon",
               "n_partidas", "n_cocheras", "n_uf", "dir_fuente", "nomencla"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(resultados)

    print(f"Resultados guardados en:")
    print(f"  {out_json}")
    print(f"  {out_csv}")


if __name__ == "__main__":
    main()
