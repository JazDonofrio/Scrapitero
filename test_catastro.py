import asyncio
import json
from src.cadastre import (
    consultar_arba_por_nomenclatura,
    consultar_lados_por_manzana,
    consultar_subparcelas_por_manzana,
)
from src.geocoder import obtener_direccion_por_coordenadas

PARTIDO = "136"
CIRC = "2"
SECC = "C"

MANZANAS = [
    "197", "198", "199", "200", "201", "202",
    "184", "185", "187",
    "178", "179", "180", "181",
    "168", "169", "170", "171",
    "159", "160", "161", "162",
    "155", "156", "157",
    "153", "154",
]


async def geocodificar_con_cache(
    lat: float, lon: float, cache: dict
) -> dict:
    """Nominatim con caché agresivo: redondea a 3 decimales (~111m)."""
    if lat == 0.0 and lon == 0.0:
        return {"calle": "S/D", "altura": "S/N", "localidad": "S/D"}
    key = (round(lat, 3), round(lon, 3))
    if key not in cache:
        cache[key] = await obtener_direccion_por_coordenadas(lat, lon)
    return cache[key]


async def main():
    print(f"\n=== CATASTRO + DIRECCIONES | Partido {PARTIDO} - Circ {CIRC} - Secc {SECC} ===")
    print(f"Manzanas: {len(MANZANAS)}  |  consultando ARBA WFS...\n")

    geo_cache: dict = {}
    registros: list = []

    for i, manzana in enumerate(MANZANAS, 1):
        print(f"[{i:02d}/{len(MANZANAS)}] Mzna {manzana}...", end=" ", flush=True)

        # Las tres consultas a ARBA van en paralelo
        parcelas, lados, subparcelas = await asyncio.gather(
            consultar_arba_por_nomenclatura(PARTIDO, CIRC, SECC, manzana),
            consultar_lados_por_manzana(PARTIDO, CIRC, SECC, manzana),
            consultar_subparcelas_por_manzana(PARTIDO, CIRC, SECC, manzana),
        )

        print(f"{len(parcelas)} parcelas", end=" | ", flush=True)

        for p in parcelas:
            cca = p.nomenclatura or ""

            # Altura: desde Lado_Catastral (FRENTE); fallback a S/N
            lado = lados.get(cca)
            altura_num = lado["altura"] if lado else None
            altura_str = str(altura_num) if altura_num else "S/N"

            # Coordenadas: usa midpoint del frente si disponible, sino centroid
            if lado and lado["lat"] != 0.0:
                geo_lat, geo_lon = lado["lat"], lado["lon"]
            else:
                geo_lat = p.coordenadas.get("lat", 0.0)
                geo_lon = p.coordenadas.get("lon", 0.0)

            # Geocoding (cacheado)
            geo = await geocodificar_con_cache(geo_lat, geo_lon, geo_cache)

            # Unidades funcionales
            unidades = subparcelas.get(cca, 1)

            registros.append({
                "manzana": manzana,
                "partida": p.partida.strip(),
                "nomenclatura": cca,
                "tipo": p.tipo_propiedad.strip(),
                "calle": geo.get("calle", "S/D"),
                "altura": altura_str,
                "localidad": geo.get("localidad", "S/D"),
                "partido_nombre": geo.get("partido", "S/D"),
                "unidades": unidades,
                "lat": p.coordenadas.get("lat"),
                "lon": p.coordenadas.get("lon"),
            })

        print(f"geo_cache={len(geo_cache)}")

    # Resumen
    total = len(registros)
    total_unidades = sum(r["unidades"] for r in registros)
    multi = [r for r in registros if r["unidades"] > 1]
    sin_altura = [r for r in registros if r["altura"] == "S/N"]

    print(f"\n{'='*60}")
    print(f"  Parcelas relevadas     : {total}")
    print(f"  Total unidades vivienda: {total_unidades}")
    print(f"  Parcelas multi-unidad  : {len(multi)}")
    print(f"  Sin altura catastral   : {len(sin_altura)}")
    print(f"  Llamadas a Nominatim   : {len(geo_cache)}")
    print(f"{'='*60}\n")

    # Tabla muestra (primeras 20)
    print(f"{'MZNA':<5} {'PARTIDA':<12} {'TIPO':<14} {'CALLE':<28} {'ALT':>4} {'UNID':>4}  LOCALIDAD")
    print("-" * 85)
    for r in registros[:20]:
        calle = (r["calle"][:25] + "...") if len(r["calle"]) > 28 else r["calle"]
        print(
            f"{r['manzana']:<5} {r['partida']:<12} {r['tipo']:<14} "
            f"{calle:<28} {r['altura']:>4} {r['unidades']:>4}  {r['localidad']}"
        )
    if total > 20:
        print(f"  ... y {total - 20} registros más")

    # Guardar completo
    out = "catastro_136_2C_completo.json"
    with open(out, "w") as f:
        json.dump(registros, f, indent=2, ensure_ascii=False)
    print(f"\nCompleto guardado en: {out}")


if __name__ == "__main__":
    asyncio.run(main())
