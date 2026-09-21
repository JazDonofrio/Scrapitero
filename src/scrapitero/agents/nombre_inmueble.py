"""Nombre del inmueble para los entregables: el negocio que está HOY, no el que registró la ficha.

El complemento de la ficha del BCI trae a veces el nombre del negocio que había cuando se dio
de alta el lote, y el CSV del cliente lo exportaba tal cual. Medido en VAZ049 (16-sep-2026): de
36 nombres comparables contra lo que ven hoy el censo 2022, Overture y Google, 10 ya no estaban
—«ESTACIONAMENTO AEROPARKING» donde hoy está la locadora Loca10, «GULA'S RESTAURANTE» donde está
el Concórdia— y uno se exportaba aunque la propia ficha dijera «ASSADO NA BRASA (FECHADO)».

La regla: primero los nombres que una fuente ve hoy en el lote (hoteles, comercios, POIs y
`establecimientos_observados`), sin repetir el mismo negocio escrito de dos formas y sin las
descripciones que no son un nombre («RESTAURANTE», «LOCADORA DE AUTOMOVEIS»). El nombre de la
ficha se conserva sólo cuando no hay ningún nombre actual que lo contradiga: sin cobertura no se
puede saber si cambió (un edificio residencial, «RES. MIRANTE DO SOL», no figura en ninguna
fuente de negocios y no por eso es viejo).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Optional

# Palabras que describen el RUBRO y no identifican a nadie. Un nombre hecho sólo de estas
# («OFICINA DE AUTOMOVEIS», el censo anota así cuando el negocio no tiene cartel) no sirve de
# nombre del inmueble.
_RUBRO = frozenset("""
DE DA DO DAS DOS E EM NA NO A O AS OS COM PARA
LTDA ME EPP EIRELI SA MEI
COMERCIO COMERCIAL SERVICOS SERVICO LOJA LOJAS STORE
RESTAURANTE LANCHONETE BAR PADARIA PIZZARIA CHURRASCARIA
OFICINA MECANICA AUTOMOVEIS AUTOMOVEL VEICULOS CARROS MOTOS AUTO PECAS
LOCADORA LOCACAO ALUGUEL HOTEL POUSADA MOTEL
ESCOLA COLEGIO CRECHE IGREJA TEMPLO
CLINICA CONSULTORIO ODONTOLOGIA ODONTOLOGICO MEDICO LABORATORIO FARMACIA DROGARIA
MERCADO SUPERMERCADO MERCEARIA ACOUGUE SALAO BELEZA CABELEIREIRO BARBEARIA
ESCRITORIO ADVOCACIA CONTABILIDADE DEPOSITO GARAGEM ESTACIONAMENTO GALPAO
POSTO COMBUSTIVEL ACADEMIA DISTRIBUIDORA EMPRESA INDUSTRIA
RESIDENCIA DOMICILIO CASA
RENT CAR PARK ESPACO LANCHES FESTA FESTAS GRILL MULTIMARCAS AUTOMOTIVO
""".split())

# Además del rubro, al COMPARAR dos nombres no cuentan los lugares: «Banco da Amazônia» y
# «Hotel Portal da Amazônia» no son el mismo negocio por compartir «Amazônia».
_LUGAR = frozenset("""
VARZEA GRANDE CUIABA MATO GROSSO MT VG BRASIL PANTANAL AEROPORTO CENTRO NORTE SUL AMAZONIA
""".split())

# Marcas de que ahí no funciona nada: el nombre no se exporta.
_CERRADO = re.compile(r"\b(VAGO|VAGA|FECHAD[OA]|DESOCUPAD[OA]|ABANDONAD[OA]|DESATIVAD[OA]|"
                      r"INATIV[OA]|EM CONSTRUCAO|SEM USO)\b")


def normalizar(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", s)).strip()


def cerrado(nombre: Optional[str]) -> bool:
    return bool(_CERRADO.search(normalizar(nombre)))


def es_generico(nombre: Optional[str], rubro: frozenset[str] = frozenset()) -> bool:
    """True si el nombre sólo describe el rubro («RESTAURANTE», «LOCADORA DE AUTOMOVEIS»).

    `rubro` suma vocabulario al fijo: `EstablecimientosObservados` le pasa las palabras que se
    repiten en 10 o más negocios del censo del município, y ahí una palabra mal escrita
    («OFIFINA», «TRANSPIRTADORA») cuenta como la bien escrita.
    """
    toks = [t for t in normalizar(nombre).split() if not t.isdigit()]
    for t in toks:
        if t in _RUBRO or t in rubro:
            continue
        if len(t) >= 6 and any(g[0] == t[0] and abs(len(g) - len(t)) <= 2
                               and SequenceMatcher(None, t, g).ratio() >= 0.85 for g in rubro):
            continue
        return False
    return True


def mismo_negocio(a: Optional[str], b: Optional[str]) -> bool:
    """¿Dos nombres nombran al mismo negocio? «Loca10 Locadora» y «LOCA10 LOCADORA DE VEICULOS»
    sí; «Banco da Amazônia» y «Agência Sicoob» no. Compara las palabras que identifican (ni rubro
    ni lugar) y, para los nombres cortos («BR Estacionamento»), el texto entero."""
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta = {t for t in na.split() if len(t) >= 4 and t not in _RUBRO and t not in _LUGAR}
    tb = {t for t in nb.split() if len(t) >= 4 and t not in _RUBRO and t not in _LUGAR}
    if ta & tb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.85


def componer_nombre(actuales: Iterable[Optional[str]],
                    respaldo: Iterable[Optional[str]] = ()) -> str:
    """Nombre del inmueble a exportar.

    `actuales` son los nombres que alguna fuente ve hoy en el lote, en orden de preferencia (la
    primera grafía de cada negocio es la que queda). `respaldo` son nombres de REGISTRO —el
    complemento de la ficha del BCI, el titular del grupo de lotes— que no dicen qué funciona
    hoy: entran sólo si no hay ningún nombre actual. Si lo hay y es el mismo negocio ya está
    representado; si es otro, el de registro es viejo.
    """
    def depurar(nombres: Iterable[Optional[str]]) -> list[str]:
        quedan: list[str] = []
        for n in nombres:
            n = (n or "").strip()
            if (not n or cerrado(n) or es_generico(n)
                    or any(mismo_negocio(n, q) for q in quedan)):
                continue
            quedan.append(n)
        return quedan

    return " | ".join(depurar(actuales) or depurar(respaldo))
