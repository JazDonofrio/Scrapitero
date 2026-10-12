"""Perfiles de nomenclatura del DXF: cómo se rotula y en qué capa va cada tipo de unidad.

El DXF sale con el estándar del cliente de Brasil (BL): `350R`, `25R-2`, `S/NC`, `S/NE` en el
rótulo; `2V 1C`, `60E` y `VAZIO` en el resumen; `VAZ` para el baldío; capas
`SDU_RES`/`SDU_COM`/`SDU_ESP`/`SDU_VAZ` y `MDU_RES`/`MDU_COM`/`MDU_ESP`. Ese es el perfil
**«Estándar BL (Brasil)»** y vive en el código (`PERFIL_BL`): elegirlo no toca el archivo.

Para otro cliente el operador arma un perfil propio desde la web (Jaz, 8-oct-2026) y se guarda
en `dxf_nomenclatura_perfil`. **Se aplica sobre el DXF ya generado** —se renombran capas y se
reescriben letras—, así que el generador no cambia y sirve también para un plano fijado a mano.

Qué se reescribe:
  · bloques `SDU`/`MDU` del plano de entrega: capa, atributos `TP`, `N_C1_3_TP_QT` y `RESUMO`;
  · textos de las capas `UF_VIVIENDA`/`UF_COMERCIO` del dibujo interno (el DXF de Argentina):
    la letra del rótulo, y la capa pasa a la «capa casa» de vivienda/comercio del perfil.
El número de puerta, la calle y el resto de los atributos no se tocan.
"""

from __future__ import annotations

import copy
import re
from typing import Optional

TIPOS = ("vivienda", "comercio", "especial", "baldio")

PERFIL_BL: dict = {
    "vivienda": {"rotulo": "R", "resumen": "V", "capa_casa": "SDU_RES", "capa_edificio": "MDU_RES"},
    "comercio": {"rotulo": "C", "resumen": "C", "capa_casa": "SDU_COM", "capa_edificio": "MDU_COM"},
    "especial": {"rotulo": "E", "resumen": "E", "capa_casa": "SDU_ESP", "capa_edificio": "MDU_ESP"},
    "baldio": {"rotulo": "VAZ", "resumen": "VAZIO", "capa_casa": "SDU_VAZ", "capa_edificio": ""},
}
NOMBRE_BL = "Estándar BL (Brasil)"

_CAPA_INVALIDA = re.compile(r'[<>/\\":;?*|=`,]')


def validar(config: dict) -> tuple[Optional[dict], Optional[str]]:
    """Normaliza un perfil que llega de la web. Devuelve (perfil, None) o (None, error)."""
    out = copy.deepcopy(PERFIL_BL)
    for t in TIPOS:
        d = (config or {}).get(t) or {}
        for k in out[t]:
            if k in d:
                out[t][k] = str(d[k] or "").strip()
    for t in TIPOS:
        for k in ("rotulo", "resumen"):
            v = out[t][k]
            if not v or len(v) > 8 or " " in v:
                return None, f"{t}: «{k}» tiene que ser un texto corto sin espacios"
        for k in ("capa_casa", "capa_edificio"):
            v = out[t][k]
            if t == "baldio" and k == "capa_edificio":
                continue
            if not v or len(v) > 60 or _CAPA_INVALIDA.search(v):
                return None, f"{t}: el nombre de capa «{v}» no es válido para AutoCAD"
    letras = [out[t]["rotulo"] for t in ("vivienda", "comercio", "especial")]
    if len(set(letras)) < 3:
        return None, "vivienda, comercio y especial necesitan letras de rótulo distintas"
    resumenes = [out[t]["resumen"] for t in ("vivienda", "comercio", "especial")]
    if len(set(resumenes)) < 3:
        return None, "vivienda, comercio y especial necesitan letras de resumen distintas"
    capas = [out[t][k] for t in TIPOS for k in ("capa_casa", "capa_edificio") if out[t][k]]
    if len(set(c.upper() for c in capas)) < len(capas):
        return None, "cada tipo tiene que ir en una capa distinta"
    return out, None


def es_bl(perfil: Optional[dict]) -> bool:
    return not perfil or validar(perfil)[0] == PERFIL_BL


# ── reescritura ───────────────────────────────────────────────────────────────
_ROTULO = re.compile(r"^(?P<num>.+?)(?P<tp>[RCE])(?P<qt>-\d+)?$")
_TOKEN_RESUMEN = re.compile(r"^(?P<n>\d+)(?P<l>[VCE])$")


def _maps(p: dict):
    rot = {"R": p["vivienda"]["rotulo"], "C": p["comercio"]["rotulo"], "E": p["especial"]["rotulo"]}
    res = {"V": p["vivienda"]["resumen"], "C": p["comercio"]["resumen"], "E": p["especial"]["resumen"]}
    capas = {}
    for t, (casa, edif) in {"vivienda": ("SDU_RES", "MDU_RES"), "comercio": ("SDU_COM", "MDU_COM"),
                            "especial": ("SDU_ESP", "MDU_ESP"), "baldio": ("SDU_VAZ", None)}.items():
        capas[casa] = p[t]["capa_casa"]
        if edif:
            capas[edif] = p[t]["capa_edificio"]
    # dibujo interno (Argentina): los rótulos de UF van a la capa «casa» del tipo
    capas["UF_VIVIENDA"] = p["vivienda"]["capa_casa"]
    capas["UF_COMERCIO"] = p["comercio"]["capa_casa"]
    return rot, res, capas


def rotulo(txt: str, p: dict) -> str:
    """`350R` → `350<letra vivienda>`, `25R-2` → `25<letra>-2`, `VAZ` → rótulo de baldío."""
    if txt == "VAZ":
        return p["baldio"]["rotulo"]
    m = _ROTULO.match(txt or "")
    if not m:
        return txt
    rot = _maps(p)[0]
    return f"{m.group('num')}{rot[m.group('tp')]}{m.group('qt') or ''}"


def resumen(txt: str, p: dict) -> str:
    """`2V 1C` → con las letras del perfil; `VAZIO` → resumen de baldío."""
    if (txt or "").strip() == "VAZIO":
        return p["baldio"]["resumen"]
    res = _maps(p)[1]
    out = []
    for tok in (txt or "").split(" "):
        m = _TOKEN_RESUMEN.match(tok)
        out.append(f"{m.group('n')}{res[m.group('l')]}" if m else tok)
    return " ".join(out)


def aplicar(doc, perfil: dict) -> dict:
    """Aplica el perfil sobre un documento ezdxf abierto. Devuelve un conteo de lo tocado."""
    p, err = validar(perfil)
    if err:
        raise ValueError(err)
    rot, _res, capas = _maps(p)
    cuenta = {"bloques": 0, "textos": 0, "capas": 0}
    msp = doc.modelspace()
    for e in msp.query("INSERT"):
        if e.dxf.name not in ("SDU", "MDU"):
            continue
        for a in e.attribs:
            tag, v = a.dxf.tag, a.dxf.text
            if tag == "TP" and v in rot:
                a.dxf.text = rot[v]
            elif tag == "N_C1_3_TP_QT":
                a.dxf.text = rotulo(v, p)
            elif tag == "RESUMO":
                a.dxf.text = resumen(v, p)
        cuenta["bloques"] += 1
    for e in msp.query("TEXT"):
        if e.dxf.layer in ("UF_VIVIENDA", "UF_COMERCIO"):
            e.dxf.text = rotulo(e.dxf.text, p)
            cuenta["textos"] += 1
    # Renombrar capas al final: `Layer.rename` arrastra a las entidades que la usan.
    for viejo, nuevo in capas.items():
        if not nuevo or viejo == nuevo or viejo not in doc.layers:
            continue
        if nuevo in doc.layers:
            # La capa destino ya existe (dos tipos a la misma no se permite; esto es una
            # capa ajena del dibujo con ese nombre): se mueven las entidades.
            for e in doc.entitydb.values():
                if hasattr(e, "dxf") and e.dxf.hasattr("layer") and e.dxf.layer == viejo:
                    e.dxf.layer = nuevo
        else:
            doc.layers.get(viejo).rename(nuevo)
        cuenta["capas"] += 1
    return cuenta
