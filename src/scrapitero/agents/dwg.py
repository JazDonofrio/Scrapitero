"""DXF → DWG con el ODA File Converter (Open Design Alliance, gratuito).

Por qué no LibreDWG (que está compilado en el server): sólo escribe hasta R2000, y R2000 no
tiene objeto GeoData — el DWG saldría sin el sistema de coordenadas asociado, que es justo lo
que pide el cliente (Jaz, 14-oct-2026). ODA escribe ACAD2018 y conserva la GeoData tal cual
(verificado contra el molde de Map 3D: XML idéntico y vértices iguales).

Instalado a mano en /opt/oda (AppImage extraído; necesita `libopengl0` y `xvfb-run`, porque
es una app Qt aunque se use sin ventanas). Se actualiza bajando el AppImage de
https://www.opendesign.com/guestfiles/oda_file_converter y volviendo a extraerlo.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

ODA = Path("/opt/oda/squashfs-root/AppRun")


def disponible() -> bool:
    return ODA.exists() and shutil.which("xvfb-run") is not None


def dxf_a_dwg(dxf: str, dwg: str, version: str = "ACAD2018", timeout_s: int = 900) -> None:
    """Convierte `dxf` en `dwg`. Lanza RuntimeError si no salió el archivo."""
    if not disponible():
        raise RuntimeError("El conversor a DWG no está instalado en el servidor")
    with tempfile.TemporaryDirectory(prefix="oda_") as tmp:
        ent, sal = Path(tmp, "in"), Path(tmp, "out")
        ent.mkdir()
        sal.mkdir()
        # ODA convierte carpetas enteras: una carpeta por llamada con un solo archivo.
        shutil.copy(dxf, ent / "plano.dxf")
        try:
            subprocess.run(["xvfb-run", "-a", str(ODA), str(ent), str(sal), version, "DWG",
                            "0", "1", "*.DXF"],
                           capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"La conversión a DWG tardó más de {timeout_s} s")
        hecho = sal / "plano.dwg"
        if not hecho.exists():
            # ODA deja el motivo en un .err junto al archivo que no pudo convertir
            err = next(sal.glob("*.err"), None)
            raise RuntimeError("No se pudo convertir a DWG"
                               + (f": {err.read_text(errors='replace')[:300]}" if err else ""))
        shutil.move(str(hecho), dwg)
