"""
Test de autenticación Python → Edenor (Widergy backend).

Uso:
    python3 test_edenor_login.py

Si usás Google para loguearte en Edenor, pegá tu token de DevTools en TOKEN (abajo).
Cómo obtenerlo: DevTools → Network → cualquier request a utilitygo.widergy.com
                → Headers → Authorization (es el valor completo, empieza con eyJ)
"""

import asyncio
import json
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from src.scrapers.edenor import EdenorClient

# ── Configuración ──────────────────────────────────────────────────────────
# Pegá acá el token copiado de DevTools (sin "Bearer ", el eyJ... directo):
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo3MzIzOTAzLCJ2ZXJpZmljYXRpb25fY29kZSI6ImduNW16elJlVF9xWVNwY2FmV1pyQUNCS1BVVTJXTXJTUDRqeXpjNnNpLVBqVXpzdWg2bUxDeFI1VnNWakt4eDciLCJyZW5ld19pZCI6Il9MTnIxRHhZbTk1UXFfYTJKYXVqcGNnSlpWZC1QN1h1IiwibWF4aW11bV91c2VmdWxfZGF0ZSI6MTgxMzQwMzg2NCwiZXhwaXJhdGlvbl9kYXRlIjoxODEwMzc5ODY0LCJ3YXJuaW5nX2V4cGlyYXRpb25fZGF0ZSI6MTc3ODkzMDI2NCwiYXVkIjpbInV0aWxpdHlnby1hcGkiLCJhZ2VudC1nby1hcGkiXSwidXRpbGl0eV9jb2RlIjoxOSwiaWF0IjoxNzc4ODQzODY0LCJleHAiOjE4MTAzNzk4NjQsImlzcyI6Imh0dHBzOi8vdXRpbGl0eWdvLWFwaS53aWRlcmd5LmNvbSJ9.rwtU3xjnR7msY3PqwUgLeRD43khDdDSXuXP7HYCdjTQ"

# Número de cliente de tu factura (NIS) para probar associations:
NIS_PRUEBA = ""  # ej: "12345678"

# Dirección para probar search_places:
CALLE_PRUEBA  = "BELGRANO"
NUMERO_PRUEBA = "2343"
# ──────────────────────────────────────────────────────────────────────────


async def main():
    client = EdenorClient()

    token = TOKEN or input("Pegá el token (eyJ...): ").strip()
    client._token = token
    import httpx
    client._client = httpx.AsyncClient(timeout=20, follow_redirects=True)
    print(f"  Token cargado ({len(token)} chars)")

    if not token:
        print("Sin token. Saliendo.")
        sys.exit(1)

    # ── 1. Listar cuentas propias ──────────────────────────────────────────
    print("\n[1] Cuentas asociadas al usuario...")
    cuentas = await client.get_accounts()
    print(f"  → {len(cuentas)} cuenta(s)")
    for c in cuentas[:5]:
        print(f"     {json.dumps(c, ensure_ascii=False)}")
    if len(cuentas) > 5:
        print(f"     ... y {len(cuentas)-5} más")

    # ── 2. Buscar por NIS (associations) ──────────────────────────────────
    nis = NIS_PRUEBA
    if not nis and cuentas:
        # Intentar extraer NIS de la primera cuenta
        primera = cuentas[0]
        nis = (
            str(primera.get("client_number") or primera.get("nis") or "")
            or ""
        )
    if not nis:
        nis = input("\nNIS / número de cliente (de tu factura): ").strip()

    if nis:
        print(f"\n[2] Buscar por NIS {nis!r} (/api/v1/accounts/associations)...")
        resultado = await client.search_by_client_number(nis)
        print(f"  → {json.dumps(resultado, ensure_ascii=False, indent=2)[:600]}")
    else:
        print("\n[2] Sin NIS — salteando.")

    # ── 3. Buscar por dirección (places) ──────────────────────────────────
    print(f"\n[3] Buscar suministros por dirección ({CALLE_PRUEBA} {NUMERO_PRUEBA}) (/api/v1/places)...")
    lugares = await client.search_places(CALLE_PRUEBA, NUMERO_PRUEBA)
    print(f"  → {json.dumps(lugares, ensure_ascii=False, indent=2)[:800]}")

    await client.close()
    print("\n✓ Test completo.")


if __name__ == "__main__":
    asyncio.run(main())
