"""Mapeo CNAE → (categoría, descripción) para clasificar establecimientos del CNPJ de
Receita según la taxonomía del cliente (R-Residencial / C-Comercial / E-Especial).

La fuente es el dump de CNPJ (Estabelecimentos: CNAE principal; Empresas: natureza jurídica).
El CNAE no mapea 1:1 a estas etiquetas, así que el mapeo es **aproximado y tuneable**: se
elige el prefijo de CNAE más largo que matchea (lo específico gana al catch-all).

Categorías que NO salen del CNPJ (vienen del catastro/BCI, no de acá):
  R: RESIDÊNCIA, APARTAMENTO, FLAT · y LOTE VAZIO. (una vivienda/terreno no tiene CNPJ)

público/particular y estadual/municipal se derivan de `natureza_juridica` (públicos = código
que empieza con '1'); para HOSPITAL/CLÍNICA/CONSULTÓRIO y ESCOLA se arma la variante exacta.
"""

from __future__ import annotations

from typing import Optional

# (prefijo CNAE 7 díg sin puntuación) → (categoría, descripción base). Longest-prefix gana.
_CNAE_MAP: dict[str, tuple[str, str]] = {
    # ── Gastronomía (C) ──
    "5611201": ("C", "RESTAURANTE"),
    "5611202": ("C", "BAR"),
    "5611203": ("C", "LANCHONETE"),
    "5611204": ("C", "LANCHONETE"),
    "5611205": ("C", "BAR"),
    "5620101": ("C", "BUFFET"),
    "5620102": ("C", "BUFFET"),
    "9329801": ("C", "CASA NOTURNA"),
    # ── Padaria (C) ──
    "4721102": ("C", "PADARIA"),
    "1091101": ("C", "PADARIA"),
    "1091102": ("C", "PADARIA"),
    # ── Automotor (C) / posto (E) ──
    "45111": ("C", "AGÊNCIA DE AUTOMOVEIS"),
    "45112": ("C", "AGÊNCIA DE AUTOMOVEIS"),
    "45120": ("C", "AGÊNCIA DE AUTOMOVEIS"),
    "45200": ("C", "OFICINA"),
    "4731800": ("E", "POSTO DE GASOLINA"),
    # ── Inmobiliaria / financiera (C) ──
    "68210": ("C", "IMOBILIÁRIA"),
    "68218": ("C", "IMOBILIÁRIA"),
    "641": ("C", "INSTITUIÇÃO FINANCEIRA"),
    "642": ("C", "INSTITUIÇÃO FINANCEIRA"),
    "643": ("C", "INSTITUIÇÃO FINANCEIRA"),
    "649": ("C", "INSTITUIÇÃO FINANCEIRA"),
    "6550": ("C", "INSTITUIÇÃO FINANCEIRA"),
    # ── Comercio / supermercado (C / E) ──
    "4711301": ("E", "SUPERMERCADO"),
    "4711302": ("E", "SUPERMERCADO"),
    "47": ("C", "COMÉRCIO EM GERAL"),          # catch-all varejo (prefijo corto = baja prioridad)
    # ── Estacionamiento (E) ──
    "5223100": ("E", "ESTACIONAMENTO"),
    # 6822 = administração de propriedade imobiliária → IMOBILIÁRIA (NO es shopping: ese CNAE
    # captura todas las administradoras/inmobiliarias. Los shoppings reales salen de OSM/Google).
    "6822": ("C", "IMOBILIÁRIA"),
    # ── Hospedaje (E) / pensão (R) ──
    "5510801": ("E", "HOTEL"),
    "5510802": ("E", "FLAT"),                   # apart-hotel ≈ flat
    "5510803": ("E", "MOTEL"),
    "5590603": ("R", "PENSÃO"),
    # ── Salud (E) — público/particular se refina con natureza ──
    "8610": ("E", "HOSPITAL"),
    "8630": ("E", "CLÍNICA"),                   # atenção ambulatorial (clínica/consultório)
    "8640": ("E", "MÉDICO / HOSPITALAR"),       # laboratórios / diagnóstico
    "8650": ("E", "MÉDICO / HOSPITALAR"),       # profissionais da área de saúde
    "8660": ("E", "MÉDICO / HOSPITALAR"),
    "8690": ("E", "MÉDICO / HOSPITALAR"),
    # ── Educación (E) ──
    "8511": ("E", "CRECHE"),
    "8512": ("E", "ESCOLA"),
    "8513": ("E", "ESCOLA"),
    "8520": ("E", "ESCOLA"),
    "8531": ("E", "UNIVERSIDADE/FACULDADE"),
    "8532": ("E", "UNIVERSIDADE/FACULDADE"),
    "8533": ("E", "UNIVERSIDADE/FACULDADE"),
    # ── Asociación / deporte / órgano público (E) ──
    "94": ("E", "ASSOCIAÇÃO / SINDICATO"),
    "9311": ("E", "INSTITUICAO ESPORTIVA"),
    "9312": ("E", "INSTITUICAO ESPORTIVA"),
    "9313": ("E", "INSTITUICAO ESPORTIVA"),
    "84": ("E", "ÓRGÃO PÚBLICO"),
    # ── Servicios profesionales (C) — catch-all de baja prioridad ──
    "69": ("C", "ESCRITÓRIO DE SERVICOS"),
    "70": ("C", "ESCRITÓRIO DE SERVICOS"),
    "71": ("C", "ESCRITÓRIO DE SERVICOS"),
    "73": ("C", "ESCRITÓRIO DE SERVICOS"),
    "74": ("C", "ESCRITÓRIO DE SERVICOS"),
    "82": ("C", "ESCRITÓRIO DE SERVICOS"),
}
# Indústria: divisões 05..33 (extractiva + transformação) → C INDÚSTRIA (prefijo 2 díg).
for _d in range(5, 34):
    _CNAE_MAP.setdefault(f"{_d:02d}", ("C", "INDÚSTRIA"))

# CNAEs que nos interesan (para el filtro del scan): todos los prefijos de arriba.
CNAE_PREFIJOS = tuple(sorted(_CNAE_MAP, key=len, reverse=True))

# natureza_juridica (4 díg): público = empieza con '1'. Subniveles estadual/municipal.
_NAT_ESTADUAL = {"1023", "1112", "1114", "1141", "1147", "1163"}
_NAT_MUNICIPAL = {"1031", "1113", "1115", "1142", "1148", "1164"}


def _es_publico(natureza: Optional[str]) -> bool:
    return bool(natureza) and natureza.strip()[:1] == "1"


def clasificar(cnae: Optional[str], natureza: Optional[str] = None
               ) -> Optional[tuple[str, str]]:
    """(categoría, descripción) para un CNAE (7 díg sin puntuación), o None si no mapea a
    ninguna categoría de la taxonomía. Refina público/particular con `natureza_juridica`."""
    if not cnae:
        return None
    cnae = "".join(ch for ch in str(cnae) if ch.isdigit())
    base = None
    for n in range(len(cnae), 1, -1):           # longest-prefix match
        hit = _CNAE_MAP.get(cnae[:n])
        if hit:
            base = hit
            break
    if not base:
        return None
    cat, desc = base
    pub = _es_publico(natureza)
    if desc == "HOSPITAL":
        desc = "HOSPITAL PÚBLICO" if pub else "HOSPITAL PARTICULAR"
    elif desc == "CLÍNICA":
        desc = "CLÍNICA PUBLICA" if pub else "CLÍNICA PARTICULAR"
    elif desc == "ESCOLA":
        if pub:
            nat = (natureza or "").strip()[:4]
            desc = ("ESCOLA PÚBLICA ESTADUAL" if nat in _NAT_ESTADUAL else
                    "ESCOLA PÚBLICA MUNICIPAL" if nat in _NAT_MUNICIPAL else
                    "ESCOLA PÚBLICA")
        else:
            desc = "ESCOLA PARTICULAR"
    return cat, desc
