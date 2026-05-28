import asyncio
import re
import httpx
from typing import Optional

BASE = "https://utilitygo.widergy.com"

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "channel": "web",
    "utility-id": "19",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "origin": "https://edenordigital.com",
    "referer": "https://edenordigital.com/",
}


class EdenorClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> dict:
        h = dict(HEADERS)
        if self._token:
            h["Authorization"] = self._token
        return h

    async def login(self, email: str, password: str) -> bool:
        """Autentica con edenordigital.com. Devuelve True si el login fue exitoso."""
        self._client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        r = await self._client.post(
            f"{BASE}/api/v1/users/sessions",
            json={"email": email, "password": password},
            headers=self._headers(),
        )
        if r.status_code == 200:
            data = r.json()
            # El token viene en el body o en headers
            token = (
                data.get("token")
                or data.get("access_token")
                or data.get("jwt")
                or r.headers.get("authorization")
                or r.headers.get("access-token")
            )
            if not token:
                # Buscar token en cualquier campo del response
                for v in _flatten_values(data):
                    if isinstance(v, str) and v.startswith("eyJ"):
                        token = v
                        break
            if token:
                self._token = token
                print(f"  [login] OK — token obtenido ({len(token)} chars)")
                return True
            print(f"  [login] Respuesta 200 pero no se encontró token: {str(data)[:200]}")
        else:
            try:
                err = r.json()
            except Exception:
                err = r.text[:200]
            print(f"  [login] Falló ({r.status_code}): {err}")
        return False

    async def get_accounts(self) -> list:
        """Devuelve todas las cuentas vinculadas al usuario autenticado."""
        r = await self._client.get(
            f"{BASE}/api/v1/accounts",
            headers=self._headers(),
        )
        if r.status_code == 200:
            return r.json()
        print(f"  [accounts] Error {r.status_code}: {r.text[:200]}")
        return []

    async def search_by_client_number(self, client_number: str) -> dict:
        """Busca una cuenta por número de cliente (NIS de la factura). Async job."""
        r = await self._client.get(
            f"{BASE}/api/v1/accounts/associations",
            params={"client_number": client_number},
            headers=self._headers(),
        )
        if r.status_code == 202:
            body = r.json()
            job_url = body.get("url")
            if job_url:
                return await self._resolve_job(job_url)
        elif r.status_code == 200:
            return r.json()
        return {"error": r.status_code, "body": r.text[:200]}

    async def search_places(self, street: str, number: str) -> dict:
        """Busca suministros por dirección. Requiere permisos elevados."""
        r = await self._client.get(
            f"{BASE}/api/v1/places",
            params={"street": street, "number": number},
            headers=self._headers(),
        )
        if r.status_code == 200:
            return r.json()
        return {"error": r.status_code, "body": r.text[:200]}

    async def _resolve_job(self, job_url: str, max_attempts: int = 10) -> dict:
        """Espera y resuelve un job asíncrono de Widergy."""
        for i in range(max_attempts):
            await asyncio.sleep(1.5)
            r = await self._client.get(job_url, headers=self._headers())
            if r.status_code == 200:
                data = r.json()
                state = data.get("state") or data.get("status", "")
                if state not in ("pending", "processing", "queued", ""):
                    return data
                if i == 0:
                    print("    polling", end="", flush=True)
                print(".", end="", flush=True)
            else:
                return {"error": r.status_code}
        print()
        return {"error": "timeout"}

    async def close(self):
        if self._client:
            await self._client.aclose()


def _flatten_values(obj, depth=0):
    """Itera recursivamente todos los valores de un dict/list."""
    if depth > 5:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_values(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten_values(item, depth + 1)
    else:
        yield obj


# ──────────────────────────────────────────────────────────────────────
# Análisis de dirección → unidades
# ──────────────────────────────────────────────────────────────────────

def parsear_piso_depto(address: str) -> Optional[tuple[int, str]]:
    """
    Extrae (piso, depto) de una dirección de Edenor.
    Formatos observados: "BELGRANO GRAL 2343 3 A", "BELGRANO GRAL 2343 1 D", "BELGRANO GRAL 2343 B 3"
    Devuelve (piso: int, depto: str) o None si es unidad simple.
    """
    # Formato "... NÚMERO LETRA" al final → piso=NÚMERO, depto=LETRA
    m = re.search(r'\b(\d{1,2})\s+([A-Z])\s*$', address.strip())
    if m:
        return int(m.group(1)), m.group(2)

    # Formato "... LETRA NÚMERO" al final → piso=NÚMERO, depto=LETRA
    m = re.search(r'\b([A-Z])\s+(\d{1,2})\s*$', address.strip())
    if m:
        return int(m.group(2)), m.group(1)

    return None


def inferir_unidades_edificio(cuentas: list[dict]) -> dict:
    """
    Dado un grupo de cuentas con la misma dirección base,
    infiere el número mínimo de unidades del edificio.
    """
    pisos_vistos: dict[int, set[str]] = {}
    dptos_pb: set[str] = set()

    for cuenta in cuentas:
        addr = cuenta.get("address", "")
        parsed = parsear_piso_depto(addr)
        if parsed:
            piso, depto = parsed
            if piso == 0:
                dptos_pb.add(depto)
            else:
                pisos_vistos.setdefault(piso, set()).add(depto)

    if not pisos_vistos:
        return {"unidades_min": len(cuentas), "fuente": "solo_cuentas", "detalle": {}}

    piso_max = max(pisos_vistos)
    max_letra = max(
        (ord(d) - ord("A") + 1 for dptos in pisos_vistos.values() for d in dptos),
        default=1,
    )
    dptos_por_piso = max_letra
    unidades_pisos = piso_max * dptos_por_piso
    unidades_pb = len(dptos_pb) if dptos_pb else dptos_por_piso

    return {
        "piso_max_visto": piso_max,
        "dptos_por_piso_min": dptos_por_piso,
        "unidades_min": unidades_pisos + unidades_pb,
        "pb_dptos_vistos": sorted(dptos_pb),
        "fuente": "inferencia",
        "detalle": {p: sorted(d) for p, d in sorted(pisos_vistos.items())},
    }
