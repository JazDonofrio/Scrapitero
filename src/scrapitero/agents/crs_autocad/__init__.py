"""Sistema de coordenadas «asociado» al dibujo, como lo guarda AutoCAD Map 3D.

Un DXF con las coordenadas bien igual abre en AutoCAD SIN sistema asignado: el CRS vive en
el objeto GeoData (AC1024 en adelante), y si no está hay que hacer `_MAPCSASSIGN` a mano.

No armamos la GeoData nosotros: se copia la que escribió Map 3D. Cada `<EPSG>.json` de esta
carpeta sale de un DWG guardado por Map 3D con el sistema asignado (`dwgread -O JSON`,
objeto GEODATA): la definición XML del sistema, el punto de diseño/referencia y la grilla
de control. Nada de eso depende del dibujo —el punto de referencia es el centro de la faja—,
así que el mismo molde vale para cualquier archivo en ese sistema.

Sin molde para el EPSG, no se escribe nada (el archivo sale como antes).
Moldes: 22175 (PGA98.Argentina-5), de relevamiento_Bolivar_2026-10-13.dwg (14-oct-2026).
"""

import json
from pathlib import Path
from typing import Optional

from ezdxf.math import Vec2, Vec3

_DIR = Path(__file__).parent


def molde(epsg: int) -> Optional[dict]:
    f = _DIR / f"{epsg}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def asociar_crs(msp, epsg: int) -> bool:
    """Agrega al modelspace la GeoData de Map 3D para `epsg`. True si había molde."""
    m = molde(epsg)
    if not m:
        return False
    g = msp.new_geodata()
    g.dxf.version = 3
    g.dxf.coordinate_type = m["coord_type"]
    g.dxf.block_record_handle = msp.layout_key
    g.dxf.design_point = Vec3(m["design_pt"])
    g.dxf.reference_point = Vec3(m["ref_pt"])
    g.dxf.horizontal_unit_scale = 1.0
    g.dxf.horizontal_units = m["units_value_horiz"]
    g.dxf.vertical_unit_scale = 1.0
    g.dxf.vertical_units = m["units_value_vert"]
    g.dxf.up_direction = Vec3(0, 0, 1)
    g.dxf.north_direction = Vec3(0, 1, 0)
    g.dxf.scale_estimation_method = m["scale_est"]
    g.dxf.user_scale_factor = 1.0
    g.dxf.sea_level_correction = 0
    g.dxf.sea_level_elevation = 0
    g.dxf.coordinate_projection_radius = 0
    g.coordinate_system_definition = m["coord_system_def"]
    g.source_vertices = [Vec2(s) for s, _ in m["geomesh_pts"]]
    g.target_vertices = [Vec2(t) for _, t in m["geomesh_pts"]]
    return True
