#!/usr/bin/env Rscript
# Worker de geocoding offline para direcciones brasileñas con {geocodebr} (IPEA, sobre el
# CNEFE del IBGE). Lo invoca por subproceso `geocodebr_fetcher.py`.
#
# Uso:  Rscript geocodebr_geocode.R <input.csv> <output.csv>
#   input.csv  columnas: id,logradouro,numero,bairro,municipio,estado,cep  (todas texto)
#   output.csv columnas: id,lat,lon,precisao,desvio_metros,cod_setor,endereco_encontrado
#
# Notas:
#  - El CEP debe ir como texto (NA lógico rompe el parser de geocodebr) → colClasses="character".
#  - El bairro se mapea al campo `localidade` de geocodebr (no existe `bairro`).
#  - ⚠ `resultado_completo` va en FALSE. Con TRUE el paquete arma un SELECT con columnas que la
#    release de datos instalada NO tiene y **toda** consulta muere con
#    `Binder Error: Table "output_db" does not have a column named "empate"`. No es un caso
#    borde: crashea siempre, con estado o sin estado (verificado 12-sep-2026 en las cuatro
#    combinaciones). Como el worker está escrito para degradar con gracia, el geocoding
#    gratuito se caía **en silencio** y todo terminaba en Nominatim/Mapbox/Google, que se paga.
#    Con FALSE vienen igual `precisao`, `tipo_resultado`, `desvio_metros` y
#    `endereco_encontrado` —todo lo que usa el fetcher para descartar los geocodes gruesos—;
#    lo único que falta es `cod_setor`, que no se guarda en ninguna tabla.
#    Si algún día hace falta el setor censitário, hay que actualizar la release de datos
#    (`geocodebr::download_cnefe()`) y recién ahí volver a TRUE, comprobándolo.
suppressMessages(library(geocodebr))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2L) stop("uso: geocodebr_geocode.R <input.csv> <output.csv>")
inp <- args[[1]]; outp <- args[[2]]

df <- read.csv(inp, colClasses = "character", encoding = "UTF-8", na.strings = character(0))

campos <- geocodebr::definir_campos(
  logradouro = "logradouro", numero = "numero", localidade = "bairro",
  municipio  = "municipio",  estado = "estado",  cep = "cep"
)

res <- geocodebr::geocode(df, campos_endereco = campos,
                          resultado_completo = FALSE, verboso = FALSE)
res <- as.data.frame(res)

# `cod_setor` ya no viene (ver la nota de arriba): el relleno con NA lo deja vacío y el
# lector en Python lo convierte en None, que es lo que ya hacía cuando el campo faltaba.
cols <- c("id", "lat", "lon", "precisao", "desvio_metros", "cod_setor", "endereco_encontrado")
for (cc in cols) if (!cc %in% names(res)) res[[cc]] <- NA
write.csv(res[, cols], outp, row.names = FALSE, na = "")
