# Tipos de propiedad — taxonomía del cliente (solo Brasil)

> Lista **fija y autoritativa** de etiquetas que puede llevar una parcela en el campo
> **"Tipo de edificación"** (popup del mapa y columna del CSV de la web). Cada parcela
> muestra **un solo** label. **Aplica solo a Brasil.**
>
> Esta es la fuente de verdad provista por el cliente. El código que la calcula vive en:
> - **Base catastral / BCI** → `_tipo_edificacion()` en `src/scrapitero/web/app.py`
> - **Hoteles** → `_hotel_tipo_label()` en `src/scrapitero/web/app.py`
> - **Establecimientos del CNPJ (Receita)** → `_CNAE_MAP` en
>   `src/scrapitero/agents/receita_categorias.py`
> - **Shopping** → `ShoppingFetcher` (no sale del CNPJ; OSM `shop=mall` + Google)
>
> Si agregás o cambiás una etiqueta, tocá el código y actualizá esta tabla en el mismo turno.

## Categorías
**R** = Residencial · **C** = Comercial · **E** = Especial.

## Lista completa

### R — Residencial
- RESIDÊNCIA
- APARTAMENTO
- PENSÃO

### C — Comercial
- AGÊNCIA DE AUTOMOVEIS
- BAR
- BUFFET
- CASA NOTURNA
- COMÉRCIO EM GERAL
- ESCRITÓRIO DE SERVICOS
- IMOBILIÁRIA
- INDÚSTRIA
- INSTITUIÇÃO FINANCEIRA
- LANCHONETE
- OFICINA
- PADARIA
- RESTAURANTE

### E — Especial
- ASSOCIAÇÃO / SINDICATO
- CLÍNICA PARTICULAR
- CLÍNICA PUBLICA
- CONSULTÓRIO PARTICULAR
- CONSULTÓRIO PÚBLICO
- CRECHE
- ESCOLA
- ESCOLA PARTICULAR
- ESCOLA PÚBLICA
- ESCOLA PÚBLICA ESTADUAL
- ESCOLA PÚBLICA MUNICIPAL
- ESTACIONAMENTO
- FLAT
- HOSPITAL PARTICULAR
- HOSPITAL PÚBLICO
- HOTEL
- INSTITUICAO ESPORTIVA
- MÉDICO / HOSPITALAR
- MOTEL
- ÓRGÃO PÚBLICO
- POSTO DE GASOLINA
- SERVICOS
- SHOPPING
- SUPERMERCADO
- UNIVERSIDADE/FACULDADE
- LOTE VAZIO

### Fuera de Brasil — `TIPOS_EDIFICACION_EXTRA` (NO son del cliente)

Etiquetas que **no** están en la lista de arriba y que `GET /api/tipos-edificacion` ofrece
**sólo si el survey no es de Brasil**. La lista del cliente es contrato: meterle un ítem se lo
mete también en el desplegable y en el CSV de Várzea Grande, donde nadie lo pidió. Pero un
relevamiento argentino tiene usos que esa lista no cubre, y forzarlos a la etiqueta más
parecida miente. El país lo decide el `survey_id`, **no el `?lang=`**: mirar VG en español no
habilita etiquetas nuevas.

| Etiqueta (canónica, PT) | Cat. | ES | Por qué |
|---|---|---|---|
| PRAÇA | E | PLAZA | Una plaza pública no es `LOTE VAZIO` (nadie la va a construir, no tiene dueño privado) ni `ESTACIONAMENTO`. Caso: la parcela de 8.022 m² de Malvinas que Google bautizó «Calle Juan» —la misma donde caía el Burger King del shopping— es la plaza del complejo. «PLAZA» cubre también la plazoleta: es la misma cosa a otra escala. |

## Prioridad de asignación

`_tipo_edificacion` resuelve el label por este orden (gana el primero que aplica):

0. **Etiqueta MANUAL del operador** (`parcela_tipo_manual`, mig. 043) → gana a todo. La setea
   el flujo "no es hotel" de la asistencia (ver abajo) cuando el ex-hotel tenía una parcela.
1. **Hotel vinculado** a la parcela → HOTEL / MOTEL / FLAT / PENSÃO
2. **Establecimiento CNPJ** (`descripcion_uso`, de `ParcelaCategoria`) → esa descripción
3. **Uso del catastro/BCI** → LOTE VAZIO / RESIDÊNCIA / APARTAMENTO / INDÚSTRIA /
   COMÉRCIO EM GERAL

(Para que entren los tipos específicos del CNPJ hay que correr `parcela_categoria` sobre la
región; si no, la parcela cae al uso del catastro.)

## Etiquetado manual desde la asistencia de hoteles ("no es hotel")

Google Places a veces devuelve un comercio con `primaryType=lodging` (dato erróneo de Google;
p.ej. "Casa Cortina", una tienda de cortinas). Ese falso hotel queda en `hoteles` y atascado en
la asistencia. Desde `/asistencia-hoteles/{survey}`, el botón **"🚫 No es hotel"** abre un
selector con **esta misma lista** de etiquetas; al elegir una (típicamente `COMÉRCIO EM GERAL`)
el backend (`POST /api/hoteles/{id}/no-es-hotel`):
1. lo **borra de `hoteles`** (sale de la asistencia, no cuenta como hotel),
2. lo registra en **`hotel_descartado`** (mig. 043) con la etiqueta + coordenada real →
   HotelFetcher lo **filtra** en la próxima corrida (no reaparece) y el mapa lo **dibuja como
   comercio en su coordenada** (`GET /api/surveys/{sid}/comercios-marcados`, marcador 🏪 color
   comercio), **sin** depender de que caiga en una parcela,
3. si tenía parcela vinculada, además sella `parcela_tipo_manual` (prioridad 0 de arriba),
4. corrige el `rubro` del comercio homónimo cercano de `lodging` → `comercio`.

La lista de etiquetas se sirve en `GET /api/tipos-edificacion` (constante `TIPOS_EDIFICACION`
en `web/app.py`, espejo de este documento).

---

## Estado de la conciliación con el código

1. **MIXTO** ✅ resuelto. `_tipo_edificacion` ahora mapea `uso = mixto` →
   **COMÉRCIO EM GERAL** (decisión del cliente; `MIXTO` no existe en la taxonomía).
2. **CONSULTÓRIO PÚBLICO / PARTICULAR** ✅ resuelto. El CNAE **8630-5/03** (atenção
   ambulatorial *restrita a consultas*) mapea a **CONSULTÓRIO** y se refina a
   PÚBLICO/PARTICULAR por `natureza_juridica`; el resto del 8630 sigue en CLÍNICA.
3. **SERVICOS (E)** ⚠️ pendiente. El código solo genera `ESCRITÓRIO DE SERVICOS` (C)
   para los CNAEs de servicios profesionales (69–82). No hay forma confiable de routear
   un CNAE a `SERVICOS` (E) genérico sin que el cliente indique **qué actividades** caen
   ahí. Queda como etiqueta válida pero sin asignación automática.
4. **LOTE VAZIO = E** ✅ documental. El catálogo lo clasifica como Especial. En el código
   `categoria_uso` (R/C/E) solo se setea para establecimientos del CNPJ; los tipos de base
   catastral no consumen la categoría, así que no requiere cambio.
