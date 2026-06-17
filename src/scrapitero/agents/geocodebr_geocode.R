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
#  - `resultado_completo=TRUE` agrega `precisao`, `desvio_metros` y `cod_setor` (setor censitário).
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
                          resultado_completo = TRUE, verboso = FALSE)
res <- as.data.frame(res)

cols <- c("id", "lat", "lon", "precisao", "desvio_metros", "cod_setor", "endereco_encontrado")
for (cc in cols) if (!cc %in% names(res)) res[[cc]] <- NA
write.csv(res[, cols], outp, row.names = FALSE, na = "")
