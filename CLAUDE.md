# Scraper GIS — Instrucciones para Claude Code

> El producto se llama **Scraper GIS**. El paquete Python, la DB, el container y el
> servicio siguen llamándose `scrapitero` a propósito: son identidad técnica, no marca.

> **Al inicio de cada sesión:** leer `ARCHITECTURE.md` para entender el sistema completo sin re-explorar código.

## Regla fundamental
**Nunca ejecutes curl, wget, ni proceses datos geoespaciales directamente.**
Siempre delegá en los agentes RPC. Sos el orquestador, no el ejecutor.

## Cómo ejecutar agentes

Patrón desde el VPS (Python 3.12):
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
PYTHONPATH=src python -m scrapitero.rpc.<nombre_agente> <<< '<JSON_INPUT>'
```

Patrón desde el container Hermes (Python 3.13):
```bash
PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src python3 -m scrapitero.rpc.<nombre_agente> <<< '<JSON_INPUT>'
```

**No uses `echo '<JSON>' | python ...`.** El escaneo de seguridad bloquea el patrón
"pipe a un intérprete" (`echo | python`) por considerarlo posible ejecución de contenido
sin inspección, y queda esperando aprobación. El agente lee el JSON por stdin igual, así
que pasalo sin pipe: con herestring `<<< '<JSON>'` (como arriba) o, para JSON largo,
escribilo a un archivo y redirigí `python -m scrapitero.rpc.<agente> < input.json`.

## Catálogo completo de agentes

### Diagnóstico y estado
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| CoverageReporter | `coverage_reporter` | **Siempre primero.** Ver estado actual: setores, parcelas, footprints, direcciones |
| SurveysStatus | `surveys_status` | Listar todos los surveys activos/recientes con conteos |
| SurveyStepUpdate | `survey_step_update` | Registrar progreso de un paso del pipeline en la DB |

### Fuentes de parcelas — Brasil
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| VGPipelineRunner | `vg_pipeline_runner` | **VG: PREFERIDO.** Happy path compilado: SmartGIS→BCI(parse inline)→Parser→Agrupador→**ShoppingFetcher**(OSM)→**ParcelaCategoria**→CountryFetcher→**HotelFetcher**→**HotelHabitacionesLLM**→**FootprintFetcher**→**AlturaFetcher** en UNA llamada determinista. Las dos últimas son las **capas de revisión de edificios** y van al final porque necesitan las parcelas ya cargadas con dirección/áreas del BCI para poder contrastar: la huella 2D sola no distingue una casa de una torre con la misma huella, así que la **altura** es la que delata al edificio y a la construcción no declarada. Ambas opcionales/best-effort (Open Buildings es gratis; Solar tiene 10.000 llamadas/mes gratis y el paso es resumible). Los pasos de shoppings/categorías/hoteles son **opcionales/best-effort** (no tumban el relevamiento si fallan); usan fuentes gratis (sin Google). El survey **no se marca `completed`** hasta terminar TODOS los pasos. Registra pasos en `surveys.notes`, honra stop. `parcial:true` ⇒ re-invocar (continúa). Los agentes de abajo quedan para correr pasos sueltos/debug |
| SmartGISFetcher | `smartgis_fetcher` | **VG: SIEMPRE primero.** Parcelas Várzea Grande: inscripción+geometría desde SmartGIS. Su `LOTE_ENDERECO` trae el logradouro **sin el tipo de vía** ("DA FEB"), así que **no pisa** `calle`/`barrio`/`codigo_postal` si ya vinieron del BCI (`AVENIDA - DA FEB`, el que llena `NOME_TIPO_LOGR` del CSV de operadora) ni si el operador los corrigió a mano — ver **precedencia de fuentes** más abajo |
| VGBCIFetcher | `varzea_bci_fetcher` | VG: Después de SmartGIS. Descarga PDFs BCI (reutiliza existentes en `pdf_downloads/`) **y parsea cada uno apenas baja** (`parse_inline`=true: uso/UF/dirección a DB de a uno). Presupuesto de tiempo (`max_runtime_s`=840): frena con gracia antes del timeout de Hermes (~900s) y devuelve `parcial:true` + `pdfs_pendientes` — re-ejecutar continúa donde quedó (NO es error) |
| BCIParser | `bci_parser` | VG: Después de VGBCIFetcher, como **red de seguridad** (idempotente): re-parsea PDFs con inline fallido o preexistentes sin parsear. Extrae uso/UF/dirección de PDFs sin LLM. Como se re-corre sobre regiones **ya terminadas**, respeta lo que aportaron los pasos posteriores: no pisa `uf_*`/`uso_*` cuando la fuente es `manual`/`cadastur`/`google`/`shopping_min` (el BCI cuenta unidades del inmueble, no habitaciones de hotel: sin la guarda un re-parseo tiraba Filinto Müller 62 de 146 UF a 1) ni la dirección `manual` — ver **precedencia de fuentes** más abajo |
| ONRLotesFetcher | `onr_lotes_fetcher` | Lotes urbanos Brasil (ciudades con cobertura ONR) |
| ONRSigefFetcher | `onr_sigef_fetcher` | Predios rurales Brasil (SIGEF/INCRA, todo el país) |
| ONRCartoIdentify | `onr_carto_identify` | Identificar cartório responsable de un punto (CNS/nombre) |
| IBGECensusFetcher | `ibge_census_fetcher` | Cuando `setores == 0`. Descarga setores censitários IBGE 2022 |
| IBGELogradourosFetcher | `ibge_logradouros_fetcher` | Antes de AddressResolver en Brasil. Geocoding gratis por interpolación |

### Fuentes de parcelas — Argentina
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| ARBACartoFetcher | `arba_carto_fetcher` | **PBA: SIEMPRE primero.** Requiere JSESSIONID. Si falla login → Telegram al usuario. Si no hay parcelas en DB las baja de IDERA por **filtro espacial** (polígono de la zona) o por nomenclatura si se pasa completa. |
| ARBACadastralFetcher | `arba_cadastral_fetcher` | PBA alternativo: WFS público de IDERA, sin autenticación. **Filtra por el polígono de la zona (`zone_geojson`) por default** — no requiere nomenclatura catastral; pasarla (partido/circ/secc/manzana) es opcional para bajar una manzana puntual. |
| SaltaCatastroFetcher | `salta_catastro_fetcher` | **Salta: SIEMPRE primero.** WFS público sin autenticación. Capital → IDEMSA (~125k parcelas). Interior → IDESA provincial. Selección automática por centroide de zona. |
| SaltaZonificacionFetcher | `salta_zonificacion_fetcher` | Después de SaltaCatastroFetcher. Clasifica uso_principal por CPUA 2019 (residencial/comercial/mixto/industrial/equipamiento/vacante). Cubre ciudad de Salta Capital. |
| SaltaRegistroFetcher | `salta_registro_fetcher` | Después de SaltaCatastroFetcher. Registro SIGSA público (toda la provincia). TIPO: RURAL→vacante, CLUB DE CAMPO→residencial, URBANO→defer a CPUA. Única señal de uso para el interior. |
| SaltaRentasFetcher | `salta_rentas_fetcher` | Después de SaltaZonificacionFetcher. DGRM rentas (Capital, vía Playwright por reCAPTCHA). valorEdificado≈0 → vacante. Detecta baldíos por parcela; corrige al CPUA. Lento + Telegram. |

### Enriquecimiento y resolución
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| OSMBuildingFetcher | `osm_building_fetcher` | Footprints OSM (cualquier país) cuando `footprints == 0`. Captura tags (tipo/pisos/viviendas) y **vincula cada edificio a su parcela** (`parcela_id`). Insumo de UnidadesEstimator |
| UnidadesEstimator | `unidades_estimator` | Estima **cantidad de unidades de vivienda/comercio** (`uf_vivienda`/`uf_comercio`) por parcela. OSM tags first + proxy geométrico + fallback por uso. Último paso de uso/UF. **NO** usar en Brasil (BCIParser ya da UF exacto) |
| AddressResolver | `address_resolver` | Cuando falta `calle` OR `numero` en parcelas. Cualquier país. Idioma automático. Brasil: IBGE gratis primero, Google Maps fallback. ARG: directo a Google (`es-AR`) |
| GooglePlacesFetcher | `google_places_fetcher` | Comercios de Google Maps (cualquier país). **Después de UnidadesEstimator.** Baja POIs comerciales por teselas adaptativas, los vincula a parcela (`ST_Contains`) y aporta el **conteo real de `uf_comercio`** (cada comercio = +1 UF, `uf_fuente='google'`) + señal de uso (parcela con comercio → comercial/mixto, `uso_fuente='google'`). Caro (~USD 0,032/req): tope `max_requests` + Telegram |
| HotelFetcher | `hotel_fetcher` | **Hoteles del relevamiento (multi-fuente, `fuentes=['cadastur','receita','osm','google']` por default).** **Cadastur** (oficial Brasil: nombre, CNPJ, **habitaciones/UHs**, leitos, tipo, situação; **lee primero la tabla local `cadastur_hospedagem`** que carga [[project_cadastur_local]] (`CadasturLocalFetcher`) y solo cae al portal CKAN si esa ciudad no está cacheada) + **Receita** (universo CNPJ de hospedagem + **situação cadastral**, dedupe por CNPJ con Cadastur, **mucha más cobertura**, sin habitaciones; coordenadas gratis vía `GeocodebrFetcher`/CNEFE, fallback a nominatim/Google para las que falten) + **OSM** (`tourism=hotel…`, gratis y siempre arriba; `rooms`/`stars` cuando están tageados) + **Google Places** (`lodging`, **descubrimiento PAGO** ~USD 0,032/req, hasta 80 teselas adaptativas; aporta ubicación + abierto/cerrado vía `businessStatus`, **no** trae habitaciones/UHs). El botón "🏨 Hoteles" de la web corre con este default (incluye Google) — para evitar el costo, invocar el agente con `fuentes=['cadastur','osm']`. Geocodifica los sin coords (Cadastur/Receita) con **geocodebr primero** (CNEFE/IBGE, gratis, en lote vía `geocode_forward`; tope de desvío `geocodebr_max_desvio_m`=300 m), y solo lo que no ubique cae a **Nominatim→Mapbox→Google** (Mapbox pago barato si hay `MAPBOX_TOKEN`); recorta a la zona, dedupe entre fuentes y **vincula a parcela** (`ST_Contains`). Enriquece **abierto/cerrado** con el `business_status` de Google (de `comercios`): `cerrado_def` = situação Inativo/Cancelado (Cadastur) **o** Receita `BAIXADA`/`NULA` **o** `CLOSED_PERMANENTLY` (Google). Para hoteles **abiertos**, suma sus **habitaciones como `uf_comercio`** (`uf_fuente='cadastur'`), los cerrados no cuentan. **Habitaciones — prioridad:** exacto de Cadastur (UHs) > OSM (`rooms`) > **manual** (asistencia humana, `hotel_habitaciones_manual` por CNPJ, mig. 030) > **IA Gemini** (`HotelHabitacionesLLM`) > **estimación por área del BCI** (`area_m2_construida ÷ m2_por_habitacion`, default 35) — la estimación BCI está **DESACTIVADA por default** (`estimar_habitaciones=False`; daba disparates) y tiene tope/guarda de área. El origen queda en `habitaciones_fuente` (`cadastur`/`osm` exacto, `manual`, `llm`, `bci_proxy` estimado); la UI marca **"(IA)"** / **"≈ N (estimado)"**. Dedupe cross-fuente sin CNPJ por **dirección normalizada o nombre + proximidad escalonada** (Google no trae CNPJ): nombre FUERTE (igual/núcleo distintivo idéntico) permite radio amplio (`merge_dist_fuerte_m`=2500 m — las coords de Cadastur/Receita son la dirección fiscal geocodificada, lejos del pin real), nombre débil exige ≤`merge_dist_m`=200 m; compartir UNA palabra ya NO matchea (bug "Amazon"). **CNPJs distintos ya no separa** (re-registro del mismo hotel): fusiona si misma dirección o mismo nombre fuerte ≤200 m. **Dedupe cross-run**: las filas ya en DB de fuentes que no corren (p.ej. Google paga de una corrida anterior) entran como semillas y se fusionan en vez de duplicarse. En el merge, la **coordenada y dirección física del pin Google/OSM pisan la geocodificada** (la dirección fiscal de Receita confundía a la asistencia). El enriquecimiento de `business_status` por comercios cercanos (60 m) ahora exige además **nombre similar**. El `cerrado_def` de la fusión **sigue a la fuente ganadora** (ya no es un OR acumulativo: si quedó cerrado por un falso positivo viejo de Google, Cadastur/Receita más nuevo y confiable lo puede reabrir). El `DELETE` previo al reinsert y el `INSERT` (con `ON CONFLICT` sobre `uq_hoteles_region_cnpj`) respetan el mismo scope de survey que las semillas, para no robarle hoteles a otro survey de la región ni romper la corrida si dos surveys comparten un CNPJ. El `uf_comercio` que aporta un hotel a su parcela se **resetea** cuando el hotel cerró o se reubicó a otra parcela entre corridas (si no, quedaba pegado con el valor viejo, y un hotel reubicado terminaba contado en dos parcelas a la vez). Capta **teléfono** (Receita) para la asistencia humana. Cuando Cadastur vuelva, el dato exacto pisa la estimación al re-correr. Escribe `hoteles` (mig. 025-026), idempotente (borra+reinserta por fuente; conserva `fuente='demo'`). Si una fuente falla (Cadastur 502) sigue con las otras; solo falla si **todas** caen. Un **cron de Hermes** (`hermes cron`, job `cadastur-watch`, cada 4 h, script `scripts/cadastur_watch_hermes.py`) chequea el portal CKAN y avisa por Telegram en cada **transición** (caído→disponible y disponible→caído, con el código HTTP: 502 backend caído, 403/451 bloqueo por IP, timeout); deja historial en `/opt/data/cadastur_watch.log`. Ver memoria [[project_hermes_cron]]. |
| ReceitaCNPJFetcher | `receita_cnpj_fetcher` | **Universo de hospedagem de Brasil (CNPJ — dados abertos da Receita Federal).** Carga `receita_estabelecimentos_hospedagem` (mig. 027) con TODOS los establecimientos CNAE **5510-8** (hotéis/apart/motéis) + **5590-6** (albergues/campings/pensões/outros): razão social, nome fantasia, endereço, **situação cadastral** (señal **gratis** de abierto/cerrado) — **no** trae habitaciones (eso sigue siendo Cadastur). Complementa a Cadastur (mucha más cobertura), mergea por CNPJ. Descarga+filtra+carga, idempotente (TRUNCATE+reload por dump mensual). Fuente: **share público de Nextcloud en `arquivos.receitafederal.gov.br`** (Receita migró los dados abertos del viejo `dadosabertos.rfb.gov.br`/SERPRO, que bloquea por **ASN de datacenter** — ni un VPS en Brasil lo atraviesa). El Nextcloud es **global ⇒ SIN proxy**: baja por **WebDAV** con el token del share (`RECEITA_SHARE_TOKEN`, default seteado); `RECEITA_PROXY` quedó **opcional** (override). Guarda+reusa los `.zip` en `RECEITA_DUMP_DIR` (`/opt/scrapitero/receita_dump/<AAAA-MM>/`, reuso por tamaño → **re-filtrar otros rubros sin re-descargar**), con **reintentos** ante cortes. El período se resuelve por **PROPFIND** del listado; el layout/token pueden cambiar (validar con `Municipios.zip`, ~KB). **Ya está wired en HotelFetcher como `fuente='receita'`**; las coordenadas las pone gratis `GeocodebrFetcher`. Un **cron de Hermes mensual** refresca la base. Ver memoria [[project_receita_cnpj]]. |
| CadasturLocalFetcher | `cadastur_local` | **Cache local de Cadastur (hoteles con UH/leitos) por município.** Descarga TODOS los trimestres parseables del package CKAN `meios-de-hospedagem` (CSV `...cadasturpj.csv` 2006→Q3 2021 + **`.xlsx` 2022→2026 parseados con stdlib**, zipfile+xml, sin openpyxl; los `.xls` BIFF 2022-24 no se parsean), filtra por `municipios`/`uf`, **consolida por CNPJ** (registro del trimestre más reciente + UH/leitos coalescidos del más reciente que los traiga) y carga `cadastur_hospedagem` (mig. 039, upsert por CNPJ, idempotente). Guarda los crudos en `CADASTUR_DUMP_DIR` (`/opt/scrapitero/cadastur_dump/`, reuso). `HotelFetcher` lee esta tabla **primero** (portal solo si falta la ciudad). El portal es intermitente (502) → así un relevamiento futuro no depende de él. VG: **39 hoteles, 38 con UH**. Ojo: el CSV cadasturpj llama **`Localidade`** al município (no `Município`). Ver memoria [[project_cadastur_local]]. |
| GeocodebrFetcher | `geocodebr_fetcher` | **Geocoding offline y GRATIS de direcciones brasileñas** (paquete R **{geocodebr}** de IPEA sobre el **CNEFE del IBGE**; requiere R+`geocodebr` en el host). Geocoder **batch** (un subproceso `Rscript` carga el CNEFE una vez): geocodifica en lote las filas sin coordenadas de `receita_estabelecimentos_hospedagem` y escribe `lat/lng/geocode_source` de vuelta (idempotente/resumible: solo `lat IS NULL AND geocode_source IS NULL`). Cada match trae **`precisao`** (numero/numero_aproximado/logradouro/cep/localidade/municipio) + **`desvio_metros`**; solo escribe coords con desvío ≤ `max_desvio_m` (default 300 m) y **sella** las gruesas (`geocode_source='g:…'`, lat NULL) para no reprocesarlas (candidatas a fallback pago). Capa **gratuita previa a Google** para Brasil. **Es la PRIMERA opción de TODO geocoding forward** (dirección→coordenada) vía el helper compartido `agents/geocode_forward.py` (`geocodebr_lote()`): lo usan **BaselineGeocoder** (relevamiento anterior) y **HotelFetcher** (hoteles Cadastur/Receita sin coords) antes de Nominatim/Google. geocodebr es **batch** por el arranque del CNEFE (~4 s) → se geocodifica en lote, NO por dirección en un loop. Ver memoria [[project_geocodebr]]. |
| ReceitaEstabFetcher | `receita_estab_fetcher` | **Universo CNPJ amplio clasificado por categoría** (escuelas/hospitales/comercios, no solo hospedagem). Re-escanea los `.zip` del dump de Receita **ya guardados** (sin re-descargar) y carga `receita_estabelecimentos` (mig. 028) con los establecimientos cuyo CNAE mapea a la taxonomía R/C/E del cliente (`agents/receita_categorias.py`: BAR, RESTAURANTE, ESCOLA, HOSPITAL, SHOPPING, SUPERMERCADO, etc.). Trae `natureza_juridica` (público/particular). Filtra por **UF** (`ufs=['MT']`) para no cargar millones nacionales. Idempotente por UF. Se geocodifica con `GeocodebrFetcher` (`tabla='receita_estabelecimentos'`) y se aterriza con `parcela_categoria`. Ver memoria [[project_receita_cnpj]]. |
| ParcelaCategoria | `parcela_categoria` | **Aterriza los establecimientos CNPJ + POIs sobre las parcelas relevadas.** Para una región: ubica los establecimientos geocodificados de `receita_estabelecimentos` **union** los POIs de `establecimientos_poi` (shoppings de OSM/Google) dentro de cada parcela (`ST_Contains`, acotado al bbox de las parcelas) y **sella** `parcelas.categoria_uso` (R/C/E) + `descripcion_uso` (BAR, ESCOLA, HOSPITAL PÚBLICO, SHOPPING…, mig. 029). Una parcela con varios lista todas; la categoría es la de mayor peso (E>C>R). Idempotente (resetea los sellos `receita_cnae` de la región antes de re-aplicar). Insumo del **tipo de edificación unificado** de la web. |
| IncidenciasReporter | `incidencias_reporter` | **Junta en un solo lugar los casos que sólo un humano puede resolver** y los carga en `incidencias` (mig. 046), que alimenta la página **`/incidencias/{survey_id}`**. Antes cada señal moría en un log, un aviso de Telegram o una página de un solo tipo — **la asistencia de hoteles quedó absorbida** como el tipo `hotel_sin_habitaciones`. Tipos: **`geocoding_dudoso`** (dirección del relevamiento anterior mal ubicada — **el más urgente**: con esos puntos se define la zona, así que un error ahí hace que se releven las parcelas equivocadas. Dos señales por geometría: `parcela_ajena` = la coordenada cae dentro de una parcela de otra calle · `lejos_de_su_calle` = está a más de `lejos_calle_m` (150 m) de **toda** parcela de su propia calle. **No** marca caer *entre* parcelas: `catastro_interp` interpola entre anclas y aterrizar en la vía es normal — con el criterio ingenuo salían 163 casos en VG de los cuales 144 eran eso; midiendo distancia a su calle quedan **44** reales. Excluye las fuentes exactas `catastro`/`g:numero`) · **`numero_faltante`** (el catastro no trae el número de puerta y `NumeroEstimator` **se negó a estimarlo** —calle con numeración incoherente o sin anclas—: en VG 26 casos, casi todos en `CLOVIS HUGNEY` 0,42 · `JOAO LIBANIO` 0,41 · `MAL RONDON` 0,55 · `SÃO BERNARDO` 0,56. La tarjeta trae los **vecinos con número** como referencia y el operador carga la altura mirando el frente en Street View; queda en **`parcela_direccion_manual`** (mig. 052, clave `(region_id, cca_code)` para **sobrevivir al re-scrape** — el `parcela_id` cambia entre surveys, la inscrição no) y se escribe en `parcelas.numero` con `direccion_source='manual'`) · **`hotel_sin_habitaciones`** (hotel abierto sin UHs en ninguna fuente) · **`altura_sin_declarar`** (el catastro no declara construcción y el satélite ve un edificio — el más valioso) · **`altura_mas_alta`** (satélite ve más pisos que el proxy del catastro) · **`uf_imposible`** (la UF declarada no cabe en el volumen visible: `(huella × pisos) / uf_vivienda < m2_por_uf_min`, default 25 m²/UF; detecta errores de carga del **baseline Y del BCI** — el umbral vive en el reporter, no en `altura_fetcher`, para no tocar el criterio de una capa ya corrida). **Idempotente preservando el trabajo humano**: upsert por `(survey_id, tipo, clave)` con **clave natural** estable entre corridas (`<tipo>:<parcela_id>`; para hoteles `hotel:<cnpj|nombre_norm>` — el `hotel_id` NO sirve, `HotelFetcher` borra+reinserta). El `ON CONFLICT` refresca el contenido del caso pero **nunca** pisa `estado`/`resolucion`/`nota`/`autor`, así re-correr 🏨 o 📏 no reabre lo resuelto; las pendientes cuya condición ya no se cumple pasan a **`obsoleta`** (y si reaparecen se reabren). Botón **"🧾 Incidencias"** (`POST /api/surveys/{sid}/incidencias/generar`) + banner en la tarjeta del survey con el desglose por tipo. Ver memoria [[project_incidencias]]. |
| AlturaFetcher | `altura_fetcher` | **Altura satelital del edificio por parcela → capa de REVISIÓN 📏 (no toca `parcelas` ni el relevamiento).** Responde lo que ninguna fuente del pipeline da: **el BCI NO trae cantidad de pisos** (verificado en los PDFs: `PISO CERAMICA` es el material, `PAVIMENTAÇÃO` el de la calle, `NIVEL 1,00` un coeficiente) y el footprint 2D no distingue una casa de un edificio con la misma huella. Fuente: **Google Solar API** `buildingInsights:findClosest` → `planeHeightAtCenterMeters` (que es **elevación msnm, NO altura**) menos el terreno de **Elevation API** (pedido en **lote**, hasta 250 coords/request ⇒ costo despreciable). Pisos = `floor(altura / metros_por_piso)` (3 m default) — **floor, no round**: `techo_msnm` es la cumbrera, así que una casa de 1 planta mide 4-5 m y con `round` toda casa pasaba a "2 pisos" (4 de 5 falsas marcas en la primera prueba). Marca dos tipos de **discrepancia** (siempre en el sentido "hay MÁS de lo declarado"; el inverso suele ser el anexo que devuelve `findClosest` o ruido de SRTM): **`sin_declarar`** = el catastro no declara construcción pero el satélite ve un edificio con huella ≥ `huella_min_m2` (20 m²) — **el caso más valioso**; **`mas_alto`** = ve ≥ `delta_pisos` más que el proxy `ceil(area_constr/(0,6·terreno))`. **GUARDA DE PERTENENCIA (mig. 047, imprescindible):** `findClosest` devuelve el edificio **más cercano al punto**, no el de la parcela — en un lote **vacío** mide *siempre* la construcción del vecino, y como `sin_declarar` se dispara justo cuando no hay construcción declarada, el sesgo se concentra ahí (medido: **56-69%** de los `sin_declarar` eran falsos; caso «DA LIBERDADE 144», BCI vacante y Street View vacío, medía la casa de DA LIBERDADE 350 a 20,3 m). Por eso se persiste el `center` del edificio (`edificio_lat/lng`), `dentro_parcela` (`ST_Contains`), `edificio_dist_m` y `footprints_dentro`, y `sin_declarar` exige **el edificio dentro de la parcela Y ≥1 footprint de Open Buildings dentro** (doble fuente satelital; la 2ª guarda se omite si el survey no corrió footprints, para no anular la capa). `mas_alto` es mucho más robusto (6-9%) y solo se descarta si `dentro_parcela=false`. Regla general: una API que devuelve "lo más cercano" no puede usarse como "lo que hay acá" sin validar pertenencia geométrica. Escribe `parcela_altura` (mig. 045, upsert por parcela), **idempotente y resumible** (`solo_faltantes`: un corte por cuota se reanuda sin re-pagar). Costo: free tier **10.000 buildingInsights/mes**; tope `max_requests` + avisos Telegram. **Throttle obligatorio** (`throttle_s`=0,7): Solar limita por minuto y sin esperar corta a mitad con 429 (reintenta 20/40/60 s; si nada se procesó devuelve `ok=false` para que el error sea visible). Persiste **`imagery_year`** y la UI lo muestra **a propósito**: en VG la imagen es **2014 en el 89%** de los puntos (2025 en 38, 2024 en 1) ⇒ el dato NO es "estado actual". Botón **"📏 Altura (revisión)"** (`POST /api/surveys/{sid}/altura`) + capa toggleable (`GET .../altura`; rojo=sin declarar, ámbar=más alto, gris=coincide). Resultado VG: 346 medidas, **72 discrepancias** (40 sin declarar, 32 más alto). Ver memoria [[project_fuentes_altura_edificios]]. |
| FootprintFetcher | `footprint_fetcher` | **Capa de REVISIÓN visual de footprints de edificios — NO toca `parcelas`/`edificios` ni el relevamiento.** Objetivo: comparar contra lo que dice el catastro (BCI en Brasil, exacto pero puede quedar desactualizado si hubo una construcción no declarada) un footprint independiente derivado de imagen satelital. Prioridad de fuente por bbox: **1) Google Open Buildings** — vía el mirror de **VIDA** en FlatGeobuf (`source.coop/vida/google-microsoft-open-buildings/flageobuf/by_country/country_iso=<ISO3>/<ISO3>.fgb`, republica el dataset de Google partido por país con columna `bf_source` que permite filtrar solo `'google'`), consultado con **DuckDB** (`httpfs`+`spatial`, ya dependencias del proyecto; requiere `s3_region='us-west-2'` + `s3_url_style='path'` — el estilo virtual-host rompe TLS por los puntos del bucket) usando el **índice espacial de FlatGeobuf** (no descarga el país completo, ~5-15 s por bbox). País (ISO-3) autodetectado del centroide del bbox con `geo.detect_country` — genérico para cualquier región del mundo, no hardcodeado a Brasil. **2) OSM (fallback)** si el archivo no existe para ese país o no hay resultados en el bbox — reusa los helpers de `osm_building_fetcher.py` (Overpass). Escribe `footprints_revision` (mig. 044, tabla separada de `edificios` para no contaminar `UnidadesEstimator` en Salta/PBA) idempotente por survey (borra+reinserta) y vincula a parcela por `ST_Contains` **filtrando por `region_id`** (necesario: varias regiones de VG se superponen geográficamente). **No es paso de `VGPipelineRunner`** — queda standalone a propósito, para revisar antes de decidir si hace falta actuar. Botón **"🏗️ Footprints (revisión)"** (in-process, `POST /api/surveys/{sid}/footprints`) + capa toggleable del mapa (`GET .../footprints` → GeoJSON, solo footprints vinculados a parcela; popup compara área/confidence del footprint vs. `uf_vivienda`/tipo de edificación del catastro). Ver memoria [[project_footprint_fetcher]]. |
| CountryFetcher | `country_fetcher` | **Barrios cerrados / condomínios (loteamentos fechados) desde OSM.** No salen del catastro/CNPJ (son un ÁREA que contiene muchas parcelas, no un POI). Una consulta Overpass por áreas con `residential=gated` o **nombre** de condomínio/loteamento fechado dentro de la zona (`COALESCE(subzona, zone_geojson)`); construye los polígonos de los **anillos cerrados** (una vía abierta "Rua Condomínio X" no forma polígono → se descarta, así mantiene precisión) y **estampa `parcelas.es_country`** (mig. 042) de las parcelas cuyo centroide cae adentro (shapely). Idempotente por survey/región (resetea el flag antes), best-effort (si Overpass cae no rompe, re-ejecutable). Alimenta la **capa 🏘 Country** del mapa. Botón "🏘 Country" (in-process, `POST /api/surveys/{sid}/country`) + paso del `VGPipelineRunner` tras `parcela_categoria`. |
| ShoppingFetcher | `shopping_fetcher` | **Shoppings reales (no salen del CNPJ).** Receita NO identifica shopping centers (el CNAE 6822 es "administração de propriedade imobiliária" = todas las inmobiliarias → remapeado a IMOBILIÁRIA). Los trae de **OSM `shop=mall`** (gratis) + **Google Places `shopping_mall`** (pago). Recorta a la zona, dedupe por **nombre + proximidad** (`merge_dist_m`=150 m; sin nombre en alguna fuente —típico de un nodo OSM sin tag `name`— cae a sola distancia), carga `establecimientos_poi` (mig. 031) con categoria='E'/descripcion='SHOPPING'. **Dedupe cross-run**: las filas ya en DB de fuentes que no corren hoy (p.ej. Google de una corrida anterior) entran como semillas y se fusionan en vez de duplicarse — antes correr OSM y Google por separado (el patrón recomendado para el paso gratis del pipeline VG) duplicaba el mismo shopping. `ParcelaCategoria` los aterriza junto a los CNPJ, con **tolerancia de borde** (`ST_DWithin` 5 m como fallback de `ST_Contains`) para no perder un shopping cuyo punto cae unos metros afuera del polígono. Idempotente por región. **Paso del pipeline VG** (solo OSM gratis; para Google correrlo aparte con `fuentes=['google']`). **Ningún fuente gratuita da la cantidad de locales de un shopping real** (Receita no distingue, OSM/Google tampoco) — `ParcelaCategoria` le pone un **piso de UF=1** (`uf_fuente='shopping_min'`) a la parcela sellada SHOPPING para que no quede en 0, pero NO es el conteo real (un mall de 40 locales sigue mostrando 1). Sin solución gratuita conocida; requeriría una fuente paga o carga manual (candidato a un flujo de asistencia humana como el de habitaciones de hotel). |
| HotelHabitacionesLLM | `hotel_habitaciones_llm` | **Completa con IA las habitaciones faltantes de hoteles.** Para cada hotel **abierto sin habitaciones** (Cadastur caído / sin dato), le pregunta a **Gemini con grounding de Google Search** ("¿cuántas habitaciones tiene el hotel X de Várzea Grande?") y registra el número en `hoteles.habitaciones` con `habitaciones_fuente='llm'` **solo si lo encuentra** (si no, lo deja NULL; los desconocidos van a asistencia humana). Rellena solo huecos (Cadastur/OSM/manual mandan). **Pago** (1 llamada con búsqueda por hotel) → tope `max_hoteles` (default 40) + throttle. Lee `GEMINI_API_KEY`/`GOOGLE_API_KEY` (Hermes la tiene). Es **paso del pipeline VG** (tras hoteles) y **botón "🛏️ Habitaciones IA"** de la web. En el mapa el número sale marcado **"(IA)"**. |
| NumeroEstimator | `numero_estimator` | **Estima el NÚMERO DE PUERTA de las parcelas que el catastro dejó sin altura.** El BCI de VG trae la dirección (código de logradouro, calle, CEP, bairro) pero en el **12,8%** de las parcelas el campo NÚMERO viene literal **`0`** (típico de `TIPO IMÓVEL: Territorial`, lote não construído) o **en blanco** — verificado contra los PDFs, **no es un fallo del parser**. Sin altura esas parcelas no aparean por dirección contra el relevamiento anterior y salen con `NUMERO` vacío en el CSV de operadora. Método (el inverso de `baseline_interp`: ahí es número→posición, acá **posición→número**): **eje de la calle** de OSM (`_osm_geometrias` + `_stitch`, una consulta Overpass cacheada en `calle_geometria`; respaldo **eje sintético por PCA** si OSM no tiene la vía) → proyecta las parcelas **con** número como **anclas** → interpola. Tres guardas que salieron de medir: (1) **`_sin_absurdos`** descarta anclas disparatadas por MAD (VG tiene una parcela cargada con el número **51000** entre vecinas de 2 cifras; como ancla mandaba a una vecina real de 58 a estimar 2271); (2) modelo **local** (`_estimar`): ancla **más cercana** + pendiente Theil-Sen de su entorno, contrastado con la interpolación entre las anclas que rodean — si discrepan manda el local, porque su error queda acotado por la distancia al ancla (las avenidas reinician la numeración al cambiar de bairro); (3) **coherencia por calle** = fracción de anclas que respeta el orden espacial: **por debajo de 0,6 NO se estima** y la calle se reporta en `calles_descartadas`. **Paridad por vereda** (signo del producto cruzado contra el eje) y sin colisiones con números ya usados. Validado por **leave-one-out** sobre las 469 parcelas CON número de VG: en calles coherentes **mediana 7 · p90 71 · ≤20 en el 80%**, y sobre targets cuyo número real es a su vez coherente con sus vecinos **mediana 6 · p90 21 · ≤20 en el 90%**. Los peores casos del LOO son **todos** parcelas cuyo número declarado contradice a sus linderos → el error es del catastro, no del método. Escribe **sólo** `parcelas.numero_estimado` / `numero_estimado_metodo` (`eje_osm`/`eje_pca`, sufijo `_extrap`) / `numero_estimado_confianza` (mig. 050): **`parcelas.numero` nunca se toca**. Idempotente por survey/región. Las calles donde **se niega a estimar** quedan en `calles_descartadas` y sus parcelas van al panel de incidencias como **`numero_faltante`**, para carga manual; las correcciones humanas (`parcela_direccion_manual`, mig. 052) se **re-aplican** al arrancar —así un re-scrape del BCI no las pisa— y, como quedan en `parcelas.numero`, entran solas como **anclas**: cada número que carga el operador mejora la estimación de sus vecinas. Botón **"🔢 Nº estimado"** (`POST /api/surveys/{sid}/numeros`); el popup del mapa lo muestra **"≈ N est."** en ámbar con tooltip del método, y el CSV en las columnas **Número estimado / método / confianza**. **Toda dirección sale con número** (regla del cliente, 2026-07-28): cuando el municipio no lo declaró, la dirección de los DOS CSV usa el estimado si su confianza llega a `_NUMERO_CONF_MIN` (0,4 — el corte deja afuera justo las extrapolaciones más allá del último ancla, que es donde el estimador mide peor); por debajo, la parcela va al panel como `numero_faltante`. `parcelas.numero` **sigue sin tocarse**: la elección de cuál sale es del export, y el crudo queda visible en `DSC_LOGRADOURO_NO` + las columnas de método y confianza, así se audita cuál se usó. En VG: 493 con número del municipio · **39 con estimado** · 33 a incidencias (suman las 565). |
| UsoClassifier | `uso_classifier` | Clasificar `uso_principal` (residencial/comercial/mixto) por parcela |
| EstablecimientoAgrupador | `establecimiento_agrupador` | **Después de uso/UF.** Agrupa parcelas que son UN solo establecimiento (fábrica/colegio/iglesia/galpón). La UF de la entidad = la del **miembro más desarrollado** (mín. 1), no la suma: una fábrica sobre 6 lotes de 1 UF → 1; pero una parcela con `uf_comercio=5` NO se colapsa (la entidad hereda esas 5). Regla: mismo `propietario_documento` real (CNPJ priorizado, sin sentinelas) + parcelas **contiguas** (componente conexa, `ST_DWithin`) + **uso no enteramente residencial**. CPF: solo agrupa sus parcelas con actividad (comercial/industrial/mixto/equipamiento), nunca sus viviendas; CNPJ: agrupa todo el bloque contiguo (incl. vivienda/baldío del predio). Escribe `establecimientos` + estampa `parcelas.establecimiento_id`. Idempotente por survey |

### Estimaciones adicionales (opcional — fuera del relevamiento principal)
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| DasymetricPopulation | `dasymetric_population` | **Habitantes por manzana** (desagregación dasimétrica). Estimación **secundaria** (menos exacta), se guarda y muestra aparte con su fecha. Reparte `setores_censitarios.pop_total` entre parcelas por peso de ocupación (uf_vivienda → volumen → área) y agrega por manzana catastral. Genérico para cualquier país con censo + parcelas. Necesita: censo que cubra la zona + fuente con parser de manzana (`manzana_catastral.py`). Correr **después** de uso/UF para mejor reparto. NO toca `parcelas`; escribe en `manzanas_habitantes`. |

### Creación de zonas
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| GeoJSONZoneFetcher | `geojson_zone_fetcher` | Crear región+survey desde archivo GeoJSON de polígonos. **País autodetectado** del centroide (cualquier país); `country_code` es override opcional |
| ZonaFetcher | `zona_fetcher` | Crear zona desde coordenada central + radio en metros. **País autodetectado** del centro |

**Genericidad / multi-país:** el sistema debe poder relevar **cualquier región del mundo**.
- El **país se autodetecta** (reverse-geocoding) al crear la zona, tanto en la web como en
  los agentes de creación — no se hardcodea ni se pide a mano (la web igual deja forzarlo).
- En **Brasil además se autodetecta y guarda el `municipio_codigo` IBGE** del centroide al
  crear la zona (`GeoJSONZoneFetcher`/`ZonaFetcher` → `geo.detect_municipio_br`). Es clave:
  IBGECensusFetcher/IBGELogradourosFetcher lo necesitan, y si la región lo tiene en NULL el
  orquestador puede adivinar un código inválido (rompe esos pasos). Si está en NULL, completarlo.
- Las utilidades geográficas comunes están en `src/scrapitero/agents/geo.py`:
  `area_m2`/`area_km2` (proyectan al **huso UTM correcto según la posición**, válido en
  todo el planeta — no usar husos fijos como 21S/20S), `utm_epsg`, `detect_country` (ISO-3),
  `country_iso2` y `detect_municipio_br` (código IBGE de un punto en Brasil). Cualquier
  cálculo de área nuevo debe usar `geo`, no un EPSG fijo.
- Las **fuentes** sí son por-zona (abaco=VG, IDEMSA/IDESA=Salta, ARBA=PBA): se agregan con
  el patrón **registry por fuente/región** (como `manzana_catastral.py`), y el orquestador
  elige la fuente según país/región.

### Reportes y exportación
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| RelevamientoReporter | `relevamiento_reporter` | Reporte completo: dirección, UF, nomenclatura por parcela |
| RelevamientoCSV | `relevamiento_csv` | Exportar a CSV compatible Google Sheets |
| ComparativaReporter | `comparativa_reporter` | Comparar un survey contra un relevamiento ANTERIOR: otro survey de la región (match por `cca_code` + dirección) o un baseline importado (CSV externo del cliente, match por dirección normalizada exacta + fuzzy). Clasifica cada dirección en nueva/cambio/igual/desaparecida + ΔUF + Δhabitantes estimado. On-the-fly, no persiste |
| CatastroGeocoder — reverse | (módulo, no RPC) | **coordenada → dirección oficial** (`reverse` / `reverse_lote` en `catastro_geocoder.py`). Es el sentido **más confiable**: no hay texto que interpretar, es `ST_Contains` puro — si el punto cae dentro del lote, la dirección de ese lote es la del punto, y la puso el municipio. En lote resuelve todos los puntos en **una sola query** (índice GiST). Respeta `FUENTES_AUTORITATIVAS` (no usa direcciones que salieron de geocodificar). **Enchufado en dos lugares:** (1) **`HotelFetcher`** — tras vincular el hotel a su parcela, su dirección catastral **pisa** la que traía la fuente, porque Cadastur/Receita dan la dirección **fiscal**, que puede estar a cientos de metros (caso Amazon Aeroporto: fiscal «Ponce de Arruda 50» vs física «Filinto Müller 62», 909 m — verificado: el reverse del pin devuelve `AVENIDA - FILINTO MULLER 62`, cca 102829); (2) **`AddressResolver`** como **paso 0** (`usar_catastro`, antes de IBGE y de Google) — antes le preguntaba a Google, pagando, la dirección de un punto cuya dirección el municipio ya publicó; `direccion_source='catastro'` con confianza 0,99. |
| CatastroGeocoder | (módulo, no RPC) | **El geocoder MÁS preciso: el catastro.** Invierte el problema — en vez de `dirección → API → coordenada → ¿en qué parcela cayó?` (que acumula error: la API devuelve el centro de la calle y el punto cae en una parcela cualquiera), matchea la dirección contra la **dirección oficial de la parcela** y usa el centroide de ESA parcela: `dirección → parcela`. Medido en VG: el error de "cae en una parcela de otra calle" pasó de **19,3% → 4,9%** (el match exacto da **2,6%**, y lo que queda son en buena parte esquinas —una parcela de esquina se cataloga por una sola de sus dos calles—). Resuelve el **86%** de las direcciones sin pegarle a ninguna API. Dos niveles: **`catastro`** (match exacto calle+número → devuelve también el `parcela_id`, sin necesidad de join espacial) y **`catastro_interp`** (la calle existe pero ese número no → interpola entre los números **reales** del catastro, mejor que sobre el eje OSM porque las anclas son direcciones oficiales). Matchea con `direccion_norm.nucleo_calle` (tolerante: el CSV del cliente y el catastro rotulan distinto la misma vía) + `limpiar_calle_anotacion`. **Solo usa direcciones de origen autoritativo** (`FUENTES_AUTORITATIVAS`: `bci_pdf`, `smartgis_vg`, catastros de Salta/PBA…) — quedan afuera a propósito las que vinieron de geocodificar (`google_maps`/`mapbox`/`nominatim`) y del `baseline`, porque usarlas para validar un geocoder sería **circular**. Genérico: funciona en cualquier región cuyas parcelas tengan dirección + geometría. Lo usa `BaselineGeocoder` como **paso 0a**; `baseline_interp` lo respeta (`_INTOCABLES`) y lo usa como ancla. |
| BaselineGeocoder | `baseline_geocoder` | Geocodifica (dirección→coordenada) las direcciones de un **baseline** (relevamiento anterior del cliente, CSV sin coordenadas). Necesario para **graficar el relevamiento anterior en el mapa** al crear una *actualización* y dibujar encima el polígono de la nueva zona. **Paso 0a: `CatastroGeocoder`** (ver fila de arriba — lo más preciso y gratis, resuelve ~86% y baja el error de parcela de 19,3% a 4,9%). **Saneo de la entrada** antes de consultar cualquier API (medido: sin esto el 25% de las consultas iba **sin número** y el 16,5% llevaba el loteamento pegado al nombre de la vía —`AV DA FEB(RES ALAMEDA)`—, y el geocoder devolvía el centro de la calle): `limpiar_calle_anotacion` saca el paréntesis y lo usa como bairro, y `_limpio` descarta sentinelas (`none`/`null`/`s/d`). **Google: solo se aceptan `ROOFTOP` y `RANGE_INTERPOLATED`** — `GEOMETRIC_CENTER` (centro de la vía) y `APPROXIMATE` (centro del barrio) **se descartan** en vez de guardarse como si fueran la dirección (eran el **65%** de lo que Google resolvía en VG, y caían en parcelas arbitrarias); `partial_match` resta confianza. Después del catastro: **geocodebr** en Brasil (CNEFE/IBGE, gratis, en lote vía `geocode_forward`, Paso 0) → **Nominatim forward gratis** (sesgo por país, ~1 req/s) → **Mapbox** (capa **paga barata**, Geocoding v6 forward; requiere `MAPBOX_TOKEN`/`MAPBOX_ACCESS_TOKEN`; `usar_mapbox=True` por default, con **tope `mapbox_max_requests`=1000** para no pasar el tramo gratis — al alcanzarlo avisa por Telegram y el resto cae a Google) → **Google Geocoding fallback** (el más caro, red final). Escribe `baseline_direcciones.lat/lng/geocode_source/geocode_confidence` (migración 019). **Caché de geocoding** (`geocode_cache`, migración 021): antes de pegarle a la API reusa coordenadas ya resueltas por **dirección normalizada + país** (`<iso2>|<calle_norm>|<numero_norm>`) — ahorra costo de Google en direcciones repetidas (varias unidades del mismo edificio), re-runs y futuras actualizaciones de la misma zona; también deduplica dentro del mismo run. Idempotente/resumible (solo filas sin `lat`), throttle + Telegram. Lo lanza la Web UI en background; también por RPC |
| GeocodeCheck | `geocode_check` | **Control de calidad del geocoding de un baseline (cross-check de 2 fuentes gratis).** Para cada dirección con coordenada guardada, la cruza contra una **segunda fuente independiente y gratis** (`geocodebr`/CNEFE) y, si difieren más de `threshold_m` (default **50 m**), la marca para **revisar**. **Solo lectura/reporte** (NO toca la DB): devuelve conteos (`comparadas`/`coinciden`/`revisar`/`no_verificable`/`sin_segunda_fuente`) + el detalle ordenado por distancia, con ambas coordenadas y la fuente guardada (Google/Nominatim) para ver cuál falla más. **Brasil-only** (geocodebr cubre Brasil; fuera no hay 2ª fuente gratis). Solo marca cuando geocodebr resolvió a **nivel de número** (`solo_numero=True`); más grueso → "no verificable". Necesita la **UF** (estado vacío crashea geocodebr): la toma del `municipio_codigo` de la región o, si está en NULL, del **centroide** de las coords guardadas. Por RPC; candidato a botón en la web |
| BaselineConsistenciaCheck | `baseline_consistencia_check` | **QA del geocoding de un baseline por consistencia de NÚMERO** (complementa la guarda de `baseline_interp` para calles con pocas direcciones). En una misma calle, dos direcciones con **números cercanos** (Δ ≤ `delta_num`=30) tienen que estar **cerca** en el espacio (casas contiguas son adyacentes en cualquier calle, sin importar la densidad de numeración); si están **lejos** (> `dist_m`=400 m), una está mal geocodificada. **Solo lectura/reporte** (NO toca la DB): lista los pares sospechosos con la fuente de cada uno y la distancia, marcando como `sospechoso` el de la fuente **menos confiable** (`g:numero`=geocodebr exacto manda). Excluye las `ciudad` (aproximadas). Bajo ruido: no marca extensiones legítimas de avenidas (números lejanos), solo "números cercanos lejos en el espacio". Por RPC; candidato a botón en la web. Ver [[project_scope_calles]] / guarda de `baseline_interp`. |
| BaselineInterp | `baseline_interp` | **Ubica las direcciones NO-exactas del relevamiento anterior interpolando el número sobre el EJE REAL de la calle (OSM) — método PRIMARIO.** El problema de fondo: el geocoder por-dirección (Mapbox/geocodebr) **apila y desordena** (en VG, 12/21 calles con ≥4 dirs estaban fuera de secuencia). Como una calle es una línea y las alturas crecen monótonas sobre ella, interpolar sobre la geometría OSM da posiciones **secuenciales por construcción**. Orden por confiabilidad: (1) **geocodebr `g:numero` exacto** se respeta y sirve de **ancla**; (2) **eje OSM (primario para el resto)**: `_osm_geometrias` baja **UNA** consulta Overpass de todas las vías con nombre del bbox (sin regex → barata) y matchea local por núcleo fuzzy ≥0.82, **cacheada por ciudad/calle en `calle_geometria` (mig. 041, incl. negativo)** → re-runs y otras zonas de la ciudad la reusan (run ~4 s en caliente vs ~260 s en frío); `_stitch` cose los tramos en un eje continuo y **deduplica carriles de avenidas de doble mano** (sin esto la cadena vuelve sobre sí misma y altura baja/alta caen juntas — era el bug de Arthur Bernardes); `_colocar_en_eje` desambigua el homónimo por **consenso de anclas** (g:numero peso 3, geocodebr 2, mapbox 1), orienta el eje y mapea número→arclength (piecewise por anclas exactas o proporcional); fuente `osm_interp`; (3) **Mapbox por número SOLO para calles que OSM no tiene**; (4) **Gemini** canoniza el nombre no matcheado (`calle_canonica` mig. 038) y reintenta OSM/Mapbox; (5) lo que nada ubica → punto aproximado de **geocodebr/CNEFE** (apilado, ubicación oficial) o **centro de la ciudad, marcado** `ciudad`. Respeta las exactas `g:numero`; no pisa un `osm_interp`/`interp` previo bueno si Overpass flakea. **Corre solo** como pase final de **BaselineGeocoder** y por RPC. Resultado VG: **21/21 calles secuenciales** (era 9/21), Arthur Bernardes 13↔251 868 m (era 30 m). Ver [[project_geocoding_osm_arbitro]]. Mirrors Overpass: overpass-api.de/kumi primero, osm.ch/private.coffee al final (200-vacío / timeout), `_try_mirrors` rechaza el 200-vacío sin `remark`. |

## Flujo para Várzea Grande (Brasil)

```
1. GeoJSONZoneFetcher  → crear región con zone_geojson (via Web UI o RPC)
2. VGPipelineRunner    → TODO el resto en una llamada determinista:
   SmartGIS → BCI (parseo inline por PDF) → Parser (red de seguridad) → Agrupador
   → marca completed. parcial:true ⇒ re-invocar con el mismo input (continúa).
3. CoverageReporter    → verificar estado
→ Desde Web UI: botón "▶ Iniciar" delega en Hermes, que invoca `vg-pipeline-runner`
  (skill preferida para BRA/VG; los pasos sueltos quedan como fallback/debug).
```

Pasos individuales (fallback/debug — el runner los ejecuta en este orden):
SmartGISFetcher → VGBCIFetcher → BCIParser → EstablecimientoAgrupador.

**Regla VG:** SmartGISFetcher SIEMPRE primero. El `CODIGO_IMOVEL_AGRUPADO` de SmartGIS = `cca_code` en DB = número para descargar BCI en `vg.abaco.com.br`. La zona se respeta automáticamente desde `regions.zone_geojson`.

**Regla `pdf_dir` (VGBCIFetcher ↔ BCIParser):** ambos deben usar el **MISMO `pdf_dir` absoluto**
(default `/opt/scrapitero/pdf_downloads`, o setear `SCRAPITERO_PDF_DIR` que ambos respetan).
Si el fetcher escribe en una carpeta (p.ej. el CWD del container Hermes con un `pdf_dir`
relativo) y el parser lee otra, BCIParser reporta "PDFs faltantes" aunque ya estén descargados.
BCIParser ahora detecta este desajuste y avisa en qué carpeta SÍ están — no hace falta re-descargar.

**Carpeta por ciudad (reuso entre relevamientos):** los PDFs NO se guardan en `pdf_dir/` plano
sino en una **subcarpeta por ciudad**: `pdf_dir/<ciudad>/reporte_*.pdf`. Tanto VGBCIFetcher
(escribe) como BCIParser (lee) resuelven la subcarpeta automáticamente desde el `region_id`
con `resolve_city_pdf_dir()` — no hay que pasar nada extra. La ciudad se deriva del
`municipio_codigo` de la región (`5108402`→`varzea-grande`); como `vg.abaco.com.br` es exclusivo
de Várzea Grande, el default es `varzea-grande` aunque la región no tenga `municipio_codigo`.
Así, al relevar una zona nueva de una ciudad ya relevada, los PDFs de parcelas compartidas se
**reutilizan** sin re-descargar. Para soportar otra ciudad de ábaco, agregar su código a
`_MUNICIPIO_SLUG` en `varzea_bci_fetcher.py`.

## Flujo para Salta (Argentina)

```
1. GeoJSONZoneFetcher           → crear región con zone_geojson (Web UI o RPC)
2. SaltaCatastroFetcher         → parcelas con geometría desde WFS público (capital o interior)
3. OSMBuildingFetcher           → footprints + tags OSM, vinculados a parcela (insumo de UF)
4. AddressResolver              → completar direcciones (directo Google Maps, es-AR)
5. SaltaRegistroFetcher         → TIPO provincial (rural/club de campo → uso)
6. SaltaZonificacionFetcher     → clasificar uso_principal urbano por CPUA 2019 (Capital)
7. SaltaRentasFetcher           → corregir baldíos por valorEdificado (Capital)
8. UnidadesEstimator            → estimar uf_vivienda/uf_comercio por parcela (último de uso/UF)
9. GooglePlacesFetcher          → comercios reales: uf_comercio exacto por conteo (opcional, pago)
10. RelevamientoCSV             → exportar resultado
```

**GooglePlacesFetcher (comercios de Google):** corre **después** de UnidadesEstimator.
Cada comercio que cae dentro de una parcela suma **+1 a `uf_comercio`** (sin agrupar; un
shopping de 20 locales = 20 UF). Es la **fuente autoritativa** de `uf_comercio` (pisa el
proxy geométrico, `uf_fuente='google'`) y señal de uso (parcela con comercio → comercial,
o mixto si ya era residencial, `uso_fuente='google'`). Busca por **teselas adaptativas**
(no por parcela) con tope `max_requests` y avisos de costo por Telegram — Places es caro
(~USD 0,032/req vs USD 0,005 del geocoding). Es **opcional/pago**: agregalo cuando el
conteo de comercios justifique el gasto.

**Objetivo de UF en Salta:** lo que importa es la **cantidad de unidades de vivienda y
de comercio** por parcela, no el conteo de edificios. El conteo exacto de UF no existe
gratis (ver memoria `project_salta_fuentes_uso_uf` y `project_salta_estimacion_uf`).
`OSMBuildingFetcher` + `UnidadesEstimator` lo **estiman**: tags OSM (`building:flats`,
`building:levels`, tipo) cuando existen, proxy geométrico (área×pisos/tamaño_típico)
**solo para edificios en altura** (`building:levels ≥ 2` o `apartments`), y fallback al
mínimo por uso para parcelas sin edificios OSM (común en el interior). Un edificio de
**1 sola planta** sin tag multi-unidad cuenta como **1 UF** (no se subdivide la huella;
evita sobrestimar — antes una casa/local grande de 2300 m² daba 30 viviendas).
**Toda la lógica está documentada en `docs/ESTIMACION_UF.md`.**

**`mixto` = vivienda O comercio (excluyente):** cada UF de una parcela mixto es vivienda
**o** comercio, nunca ambas a la vez. Cada edificio se asigna a una sola categoría por su
tag; los no tipados y el fallback sin edificios → vivienda por defecto. NO se cuentan
1 vivienda + 1 comercio en mixto.

**UF mínimas por uso (Salta):** al clasificar se computan `unidades_funcionales_estimadas`
**y `uf_vivienda`**:
**residencial → SIEMPRE al menos 1 UF de vivienda** (una vivienda mínima por parcela; se
setea tanto `unidades_funcionales_estimadas` como `uf_vivienda` con `GREATEST(…,1)`, sin
pisar un conteo real mayor); vacante/baldío → 0 UF (terreno vacío, sin unidad). Lo aplican
SaltaZonificacionFetcher (uso urbano CPUA) y SaltaRegistroFetcher (CLUB DE CAMPO → residencial);
SaltaRentasFetcher corrige a 0 cuando detecta baldío por `valorEdificado`.

**Uso/UF exactos por parcela:** no hay fuente gratuita con cobertura completa. Ver
memoria `project_salta_fuentes_uso_uf`. Lo gratuito: uso por zona (CPUA) + TIPO
provincial (registro SIGSA) + baldío/edificado por parcela (rentas DGRM).
**Número de UF/PH: ninguna fuente gratuita lo da** — el catastro modela cada UF como
clave independiente; el agrupamiento solo está en la cédula paga de inmuebles.gov.ar.

**Regla Salta:** SaltaCatastroFetcher detecta automáticamente la fuente según el centroide de la zona:
- Ciudad de Salta Capital → IDEMSA (geocloud.municipalidadsalta.gob.ar), ~125k parcelas, EPSG:4326, sin auth
- Interior provincial → IDESA (geoportal.idesa.gob.ar), cobertura provincial, puede ser más lento

---

## Flujo para Buenos Aires Province (Argentina)

```
1. ARBACartoFetcher    → parcelas con geometría + UF/cocheras (requiere JSESSIONID vigente)
   └─ Si falla login  → notificar al usuario por Telegram y detener
2. OSMBuildingFetcher  → footprints de edificios
3. AddressResolver     → completar direcciones faltantes
4. UsoClassifier       → clasificar uso_principal (PASO ESTÁNDAR, no opcional)
5. RelevamientoCSV     → exportar resultado
```

**Regla PBA — uso_principal SIEMPRE se clasifica:** PBA no tiene fuente nativa de uso
(a diferencia de Brasil=BCI y Salta=CPUA/SIGSA). La única señal es `UsoClassifier`, que
combina la **UF de ARBA** (`uf_vivienda`/`uf_comercio` que llena ARBACartoFetcher desde las
subparcelas de carto.arba.gov.ar) + **Google Places** (comercios alrededor). Por eso:
- `UsoClassifier` es **paso estándar** del flujo PBA (no opcional) — si no se corre, todas
  las parcelas quedan `uso_principal = NULL` ("sin clasificar").
- **Depende de ARBACartoFetcher:** si las parcelas entraron solo por IDERA (`arba_idera`,
  geometría sin UF), `UsoClassifier` no tiene UF y cae a Google Places / `sin_datos`. Para
  uso útil, correr ARBACartoFetcher (con JSESSIONID) **antes**.
- Requiere `GOOGLE_MAPS_API_KEY`. Opcional: `GooglePlacesFetcher` para conteo real de comercios.

**Regla PBA — la zona (GeoJSON) maneja la descarga de parcelas:** como todo relevamiento
parte de un GeoJSON, **no hace falta la nomenclatura catastral** para entrar a ARBA. Tanto
ARBACartoFetcher como ARBACadastralFetcher, si no encuentran parcelas en DB, las bajan de
IDERA por **filtro espacial**: bbox del polígono de la zona (`regions.zone_geojson`) vía el
parámetro WFS `bbox=...,EPSG:4326` (que reproyecta desde el CRS nativo Gauss-Krüger del
layer) + **recorte exacto al polígono con shapely**. La nomenclatura (partido/circ/secc/
manzana) es **opcional**: pasarla completa filtra por prefijo CCA (una manzana puntual).
No se usa CQL `INTERSECTS` porque GeoServer interpreta el WKT en el CRS nativo (metros), no
en lat/lon. Para esto la región debe tener `zone_geojson` (creada con GeoJSONZoneFetcher).

## Lo que NO debés hacer
- ❌ `curl https://geoftp.ibge.gov.br/...`
- ❌ `wget ...`
- ❌ Procesar shapefiles directamente
- ❌ Insertar filas en la DB manualmente
- ❌ Instalar paquetes (`pip install`, `uv install`)

## Notificaciones (Telegram + actividad web)

**La audiencia es un operador técnico**, no un usuario final. El operador puede destrabar
el problema (dar una credencial, levantar una fuente caída, reiniciar un servicio) **solo
si el mensaje dice qué falló exactamente**. Por eso, en Telegram y en el cuadro de
actividad de la web:

- **Éxito / progreso:** mensajes cortos con los números clave.
- **Error o problema: SIEMPRE el detalle concreto de la causa.** Prohibido el genérico
  ("hubo un problema" / "reintentando…" sin más). Incluir, textual:
  - el campo `error` del output del agente (copiado tal cual),
  - **qué agente/paso** falló (nombrarlo: SmartGIS, OSM/Overpass, ARBA Carto, SaltaRentas…),
  - la causa técnica exacta: código HTTP + host/URL, credencial/sesión faltante (p.ej.
    `JSESSIONID` vencido), reCAPTCHA que no cargó, `ModuleNotFoundError`, timeout del WFS…,
  - **qué se necesita para resolverlo**, si se sabe.

**Lado código (los logs nacen en el agente, no en las skills):** el decorador
`agent_run` (en `src/scrapitero/agents/_run.py`) envuelve el `run()` de **todos** los
agentes y, ante un fallo (`ok=False`), **sella el campo `error` con el slug de la skill**
(`osm_building_fetcher` → `osm-building-fetcher`) y emite un `logger.error` con `[skill]
detalle`. Por eso el `error` que devuelve cualquier agente **ya incluye qué skill falló +
la causa**; el orquestador sólo tiene que relayarlo tal cual (no reescribir ni resumir).
El cuadro de actividad de la web muestra `surveys.notes.pasos[paso].error`, que ya viene
sellado. Si agregás un agente nuevo, ponele `@agent_run` sobre su `run()`.
Al tocar mensajería, aplicar el cambio en `CLAUDE.md` **y** en las skills `relevar-zona` /
`relevar-region` en el mismo turno (ver memoria `feedback_cambios_hermes`).

## Web UI

Dashboard para gestionar relevamientos. Corre en `http://localhost:8765` (público en
`http://2.25.141.45:8765`, `WEB_BASE_URL` para los links de Telegram). Credenciales de
**Telegram** (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_HOME_CHANNEL`/`TELEGRAM_ALLOWED_USERS`) **también
en el `.env` del host** (no solo en Hermes): el botón 🏨 corre in-process en la web, y sin esas
vars sus avisos por Telegram se descartan en silencio.

**Capas de ítems críticos (mapa):** el control de capas del mapa tiene un overlay toggleable por
cada tipo de propiedad crítico — **🏨 Hoteles · 🏢 Edificios · 🧱 PH · 🛍 Shopping · 🏘 Country** —
para aislar visualmente cada uno (anillo de color propio sobre el marcador base). Una parcela puede
caer en varias (un edificio de deptos es Edificio Y PH). El endpoint `GET /api/surveys/{sid}/parcelas`
devuelve `items[]` por parcela (`_items_criticos` en `web/app.py`): **hotel**=hotel abierto vinculado ·
**edificio**=`uf_vivienda>1` (APARTAMENTO) · **ph**=`>1` unidad en `parcela_unidades` (propiedad
horizontal) · **shopping**=`descripcion_uso` SHOPPING · **country**=`parcelas.es_country` (lo marca
`CountryFetcher` desde OSM). La capa solo aparece si hay ≥1 parcela de ese ítem, con su conteo. Ver
memoria [[project_capas_criticas]].

**Tipo de edificación unificado (taxonomía del cliente):** cada parcela muestra **un solo
label** de la lista fija del cliente (RESIDÊNCIA/APARTAMENTO/LOTE VAZIO/HOTEL/BAR/ESCOLA/
HOSPITAL/SHOPPING/COMÉRCIO EM GERAL/MIXTO/INDÚSTRIA…). Lo calcula `_tipo_edificacion` (en
`web/app.py`) por prioridad: **hotel** vinculado→HOTEL/MOTEL/FLAT/PENSÃO; **establecimiento
CNPJ** (`descripcion_uso`, de `ParcelaCategoria`)→esa descripción; si no, del **catastro/BCI**:
vacante/sin construir→LOTE VAZIO, residencial→RESIDÊNCIA (uf_vivienda=1)/APARTAMENTO (>1),
industrial→INDÚSTRIA, comercial y mixto→COMÉRCIO EM GERAL (la taxonomía del cliente, solo
Brasil, no tiene 'MIXTO'; lista completa en `docs/TIPOS_PROPIEDAD.md`). Se muestra en el popup de la
parcela (línea 🏢) y en el CSV (columna **"Tipo de edificación"**). Para que entren los tipos
CNPJ específicos hay que correr `parcela_categoria` sobre la región.

**Habitaciones de hoteles — IA + asistencia humana:** cuando ninguna fuente trae las
habitaciones (Cadastur 502, estimación BCI desactivada por default), hay dos caminos:
(1) **IA** — botón **"🛏️ Habitaciones IA"** / paso del pipeline (`HotelHabitacionesLLM`): Gemini
busca en la web y completa las que encuentra (marcadas **"(IA)"** en el popup).
(2) **Asistencia humana** — al terminar 🏨, si quedan hoteles **abiertos sin habitaciones**,
HotelFetcher manda un **🆘 aviso por Telegram con un link** al **reporte de incidencias**
(`/incidencias/{survey}?tipo=hotel_sin_habitaciones`; la vieja `/asistencia-hoteles/{survey}`
**redirige** ahí, así los avisos ya enviados siguen funcionando): ficha del hotel
(teléfono/dirección/situação/fuente) + carga manual del número (queda
`habitaciones_fuente='manual'`, persiste por CNPJ a re-cortes). Los que la IA no encuentra
(nombres basura de Google, etc.) caen acá. El popup del hotel muestra dirección completa.

**Reporte de incidencias (`/incidencias/{survey_id}`) — TODO lo que necesita un humano:** una
sola página donde el operador resuelve los casos que ninguna fuente puede cerrar sola. Los carga
`IncidenciasReporter` (botón **"🧾 Incidencias"** o `POST /api/surveys/{sid}/incidencias/generar`)
en la tabla `incidencias` (mig. 046). Layout igual a la ex-asistencia de hoteles (sidebar +
Leaflet, puntos del relevamiento anterior como contexto) más **chips de filtro por tipo** y
selector de estado; el link de Telegram llega con `?tipo=` preseleccionado.
**Mapa con satélite y salida a Google Maps** (estas incidencias se resuelven *mirando* la
construcción): capas base **Esri satélite (default) · OSM · Google satélite/mapa** — las de
Google se agregan solo si hay `GOOGLE_MAPS_API_KEY` (vía `/api/config` + Leaflet.GoogleMutant,
el mismo mecanismo que `addGoogleLayers` del dashboard) y `maxZoom` 22 con zoom digital sobre el
tile nativo. Botón **"🌍 Abrir en Google Maps"** sobre el mapa: abre la **misma vista** (centro +
zoom actuales) en satélite (`/maps/@lat,lng,Nz/data=!3m1!1e3`), y cada tarjeta tiene su propio
**"🌍 Ver en Google Maps"** para ir directo a ese caso (Street View del edificio). Cada tarjeta muestra
el caso y **sólo las acciones de su tipo**, y todas ofrecen **"✖ No es un problema"** (cierra con
nota, sin tocar datos). Las acciones escriben en las **tablas de override durables** para que un
re-scrape no pierda la corrección: `habitaciones` → `hoteles` + `hotel_habitaciones_manual` ·
`no_es_hotel` → helper `_descartar_hotel` (4 tablas, el mismo que usa el botón 🚫) ·
`tipo_edificacion` → `parcela_tipo_manual` · `uf` → `parcelas` (`uf_fuente='manual'`) +
**`parcela_uf_manual`** (mig. 046) · **`direccion`** → `parcelas` (`direccion_source='manual'`) +
**`parcela_direccion_manual`** (mig. 052).

**Editor ÚNICO de la ubicación (✏️ Editar ubicación) — un solo botón de guardar:** cada tarjeta
tiene **un** formulario con todo lo editable y **un** botón «💾 Guardar cambios» al final, en vez
de un mini-form por variable: **calle · número · complemento · bairro · CEP · tipo de edificación ·
UF vivienda · UF comercio · coordenada · habitaciones** (estas últimas sólo si el caso trae hotel).
Viene precargado con los valores actuales (`GET /api/parcelas/{parcela_id}`, que devuelve también
`lat`/`lng`/`ubicacion_source`) y con el origen de cada dato al pie. Los campos que se dejan igual
no se mandan y no se tocan. Un caso de altura o de UF casi siempre destapa además que la dirección
está mal rotulada, y antes no había forma de arreglarla sin entrar a la base.
**La coordenada se marca clickeando en el mapa**: el botón «📍 Marcar en el mapa» entra en modo
selección (cursor de cruz) y el click sobre el satélite planta el punto y llena lat/lng; el pin
queda arrastrable para afinar. Escribir lat/lng a mano queda como ajuste fino, no como entrada
principal — nadie tipea coordenadas mirando una foto aérea. **Gotcha de Leaflet:** el click que
cae sobre una capa interactiva **no** se propaga al mapa, y el mapa está lleno de marcadores
(incidencias + puntos del relevamiento anterior), que son justo los que uno quiere reubicar ⇒ el
modo engancha el mismo handler en cada capa mientras dura y lo desengancha al salir. Va a **tres destinos**
según qué es la ubicación del caso, en ese orden: **hotel** (`_mover_hotel`: mueve el pin,
**re-vincula la parcela** por `ST_Contains` y persiste `hotel_ubicacion_manual`, mig. 049) →
**parcela** (`centroid_lat/lng` + `ubicacion_source='manual'` + `parcela_ubicacion_manual`,
mig. 053; **no toca `geometry`**, que es el polígono del catastro) → **dirección del relevamiento
anterior** (`baseline_direcciones.lat/lng` con `geocode_source='manual'`). Eso le da acción por
primera vez a **`geocoding_dudoso`**, que mostraba el problema —el punto está a cientos de metros
de su calle— y no ofrecía forma de arreglarlo. Ojo: 16 de 20 `hotel_sin_habitaciones` **no tienen
parcela**, así que ese camino también acepta habitaciones + coordenada.
**Etiqueta y UF sin parcela:** las tarjetas que apuntan al relevamiento anterior editan la
etiqueta y las UF **de esa dirección** (`baseline_direcciones`, precargadas con
`GET /api/baseline-direcciones/{id}`). La etiqueta va a la columna propia `tipo_edificacion`
(mig. 054) y **no** a `uso`: `uso` es la clasificación funcional (`residencial`/`comercial`/
`mixto`) que consumen `_agregar_por_direccion`, la comparativa y el CSV, así que se **deriva** de
las UF resultantes con la misma regla de la importación. La corrección se ve sola en el mapa —
`/api/baselines/{id}/puntos` calcula `uf_total` de `uf_vivienda+uf_comercio`.
Cada concepto se guarda en **su** tabla de override (dirección → `parcela_direccion_manual`, tipo →
`parcela_tipo_manual`, UF → `parcela_uf_manual`, coordenada → la de arriba según el objeto,
habitaciones → `hotel_habitaciones_manual`), todo en **una sola transacción**: el "guardar" es uno
solo también del lado de los datos. Las acciones que **no** son edición de campos siguen aparte,
porque son decisiones que borran o cierran un registro: **🔒 Está cerrado · 🚫 No es hotel ·
✖ No es un problema**. La corrección de dirección va a **`parcelas` directamente** con
`direccion_source='manual'`: tiene que llegar al CSV, al CSV Operadora y al apareo contra el
relevamiento anterior — si quedara sólo en una columna secundaria, el trabajo del operador no
llegaría al entregable. `NumeroEstimator` **re-aplica** `parcela_direccion_manual` al
arrancar, así un re-scrape del BCI no la pisa (clave `(region_id, cca_code)`: la inscrição es
estable entre relevamientos, el `parcela_id` no); por el mismo motivo **SmartGIS no pisa el
centroide** cuando `ubicacion_source='manual'`. Endpoints: `GET /api/surveys/{sid}/incidencias?estado=&tipo=`
(lista + `resumen` por tipo/estado), `POST .../incidencias/generar`,
`POST /api/incidencias/{id}/resolver` (`{accion, valor, nota}`; la acción del editor único es
`ubicacion` — `direccion` se acepta como alias histórico). En los tipos de altura la tarjeta
**muestra el año de la imagen satelital** con una advertencia — en VG el 89% es de 2014, así que el
dato no refleja obra posterior. Resultado VG: **139 incidencias** (55 sin declarar, 45 más alto,
28 UF imposible, 11 hoteles).

**Falsos hoteles → reclasificar a comercio (botón "🚫 No es hotel"):** Google Places a veces
devuelve un comercio con `primaryType=lodging` (dato erróneo; p.ej. "Casa Cortina", tienda de
cortinas — la Receita lo confirma comércio CNAE 4759, y no trae CNPJ para cruzarlo automático).
En el reporte de incidencias, el link **"🚫 No es hotel"** de cada tarjeta abre un selector con la
**taxonomía del cliente** (`GET /api/tipos-edificacion`, constante `TIPOS_EDIFICACION` en
`web/app.py`, espejo de `docs/TIPOS_PROPIEDAD.md`); al elegir la etiqueta (típ. `COMÉRCIO EM
GERAL`) el `POST /api/hoteles/{id}/no-es-hotel`: (1) **borra el hotel** (sale de la asistencia,
no cuenta como hotel); (2) lo guarda en **`hotel_descartado`** (mig. 043) con etiqueta +
coordenada → HotelFetcher lo **filtra** en la próxima corrida (`_cargar_descartados`/
`_esta_descartado`: por CNPJ o nombre+proximidad ≤200 m, no reaparece) y el mapa lo **dibuja
como comercio en su coordenada real** (marcador 🏪 color comercio vía
`GET /api/surveys/{sid}/comercios-marcados` + `loadComerciosMarcados`), **sin** necesitar una
parcela; (3) si tenía parcela, sella `parcela_tipo_manual` (override de máxima prioridad del
"Tipo de edificación", gana a hotel/CNPJ/catastro); (4) corrige el `rubro` `lodging`→`comercio`
del comercio homónimo cercano. Requisito del cliente: "ningún caso es ignorable" — el ex-hotel
queda **visible como comercio en su coordenada**, no solo borrado.

**Botón 🏨 Hoteles:** tilde **"Google (pago)"** para correr con/sin la fuente paga. En la
leyenda del mapa, las líneas de hotel de la taxonomía del cliente (HOTEL/MOTEL/FLAT/PENSÃO)
muestran, además del conteo de parcelas, el **total de habitaciones** de esos hoteles
(abiertos, con dato) — `loadHoteles` suma por tipo (`hotelTipoLabel`, espejo de
`_hotel_tipo_label`) y lo guarda en `map._hotelHabByTipo`, que lee el render de la leyenda.
HotelFetcher tiene un **buffer de borde** (40 m) para no perder hoteles pegados al límite del
polígono.

**Número de puerta estimado (🔢) — toda dirección sale con número:** las parcelas que el catastro
dejó sin altura (`numero` en `0` o vacío) reciben un número **inferido** por `NumeroEstimator`
(ver su fila arriba). Regla del cliente (2026-07-28): *"todas las direcciones tienen que tener
número; si la parcela viene por catastro —que es lo más seguro que tenemos— usá un estimado, y lo
que no se pueda o sea poco confiable, cargalo como incidencia"*. Entonces:
- **La dirección de los dos CSV usa el estimado** cuando el municipio no declaró el número y la
  confianza llega a **`_NUMERO_CONF_MIN` = 0,4** (constante en `web/app.py`, espejada en
  `IncidenciasInput.numero_conf_min`; **si se cambia una hay que cambiar la otra**, o quedan
  parcelas sin número en el CSV y sin incidencia que las reclame). El corte en 0,4 deja afuera
  justo las **extrapolaciones** más allá del último ancla, que es donde el estimador mide peor.
- **`parcelas.numero` sigue sin tocarse**: la elección de cuál sale es del export. El crudo del
  municipio queda visible en `DSC_LOGRADOURO_NO` y el inferido en sus columnas propias
  (**Número estimado / método / confianza**), así se audita cuál se usó en cada fila. En el mapa
  se sigue marcando **"≈ N est."**.
- **Lo que no llega al piso de confianza va al panel** como `numero_faltante`. La tarjeta aclara
  que **la ubicación NO está en duda** —inscrição, polígono, calle y CEP vienen del catastro— y
  que lo único que falta es el rótulo; si hay una estimación floja, la muestra como punto de
  partida con su confianza. Sin esa aclaración el operador sale a verificar una ubicación que ya
  es dato del municipio.
- Reparto en VG: **493** con número del municipio · **39** con estimado · **33** a incidencias.

**CSV del relevamiento:** export único **⬇ CSV** (todas las parcelas del survey; se quitó el
"CSV Consolidado" y el botón "🔁 Sub-zona"). Columnas extra: **`DSC_NOME_DO_IMOVEL`**
(nombre del comercio/hotel) y **`DSC_LOGRADOURO_NO`** (número de la dirección).

**Comentarios del cliente (sugerencias/correcciones sobre direcciones relevadas):** el
cliente (y el operador) puede dejar comentarios sobre **una parcela relevada** — uno de los
círculos de color del mapa. Se crean y se leen en el **mismo popup de detalle de la
parcela** (botón "💬 Comentar" dentro del popup → texto). Se guardan en la tabla
`comentarios_cliente` (migración 015: `parcela_id` obligatorio en la práctica, POINT 4326
en el centroide de la parcela, texto, `autor_rol`, `estado` pendiente/resuelto). Es la
**única escritura permitida al rol cliente** (excepción explícita en el middleware de
auth); el backend valida que la parcela pertenezca al survey. Las parcelas comentadas
muestran un pin 💬 (ámbar=pendiente, verde=resuelto) que al clickearlo abre el popup de la
parcela. Cada comentario nuevo dispara un **aviso por Telegram al operador** (región,
dirección de la parcela, coordenadas y texto). El operador gestiona desde la misma sección
del popup: ✔ resolver / ↩ reabrir / 🗑 eliminar. Endpoints:
`GET|POST /api/surveys/{id}/comentarios` (POST con `parcela_id`+`texto`),
`POST /api/comentarios/{id}/estado`, `DELETE /api/comentarios/{id}`.

**Visibilidad en vista cliente (tilde "👁 Cliente"):** cada tarjeta de la lista del
operador tiene un tilde que controla si ese relevamiento se muestra en la vista
cliente (raíz `/`). `surveys.visible_cliente` (migración 014, default `true`); el rol
cliente solo recibe los visibles (filtro server-side en `GET /api/surveys`; toggle:
`POST /api/surveys/{id}/visibilidad`, solo operador).

**Habitantes por manzana (opción adicional):** en el detalle de cada relevamiento hay una
sección aparte **"👥 Habitantes por manzana"** con un botón **"▶ Estimar habitantes"** que
corre `DasymetricPopulation` in-process y muestra una tabla por manzana (Habitantes ≈ ·
rango · UF Viv · UF Com · Parcelas) con total y **fecha de estimación**. Es **secundaria**
al relevamiento (menos exacta), claramente marcada como tal. Endpoints:
`POST /api/surveys/{id}/dasimetrico` (correr) y `GET /api/surveys/{id}/manzanas` (leer).

**Formato del CSV (web export):** la primera columna es la **Dirección completa**
(calle + número + complemento); siguen **Unidad**, **Código Unidad**, **Uso**,
**UF Vivienda**, **UF Comercio**, y de ahí en adelante el resto de la información de la
parcela. Sin dirección → `(sin dirección)`.

**Expansión por unidad (Brasil/BCI):** para parcelas con **más de una unidad** (edificios
/ lotes con varias casas conjugadas) el CSV **no pone el conteo de UF en una fila**, sino
**una fila por unidad** repitiendo la dirección completa (con su complemento) e
identificándola con **Unidad** = `Unidade N` (1..N del BCI) + **Código Unidad** (código de
unidad único del BCI). Cada fila expandida lleva su propio uso, UF (1 en vivienda o
comercio según el uso de esa unidad), área construida y suma al total de la parcela. Las
parcelas de una sola unidad (incluidos los departamentos reales, que en el catastro de VG
son cada uno su propia inscrição con su complemento `ED/BLOCO/APTO`) quedan como **una fila
normal** (columnas Unidad/Código vacías). Las unidades se guardan en `parcela_unidades`
(migración 020) que llena `BCIParser` — sólo persiste parcelas con >1 unidad; **NO** cambia
`parcelas` (la web/KPIs/mapa siguen mostrando el conteo). La expansión es **sólo de este
CSV** (no del CSV Operadora ni del consolidado).

**CSV Operadora (solo Brasil):** botón verde Brasil "⬇ CSV Operadora" en el detalle de
cada relevamiento (vistas cliente y operador), visible solo si `country_code='BRA'`.
Endpoint `GET /api/surveys/{id}/export/csv-operadora`. Layout de base de logradouros de
operadora: `COD_OPERADORA` (=858 fijo), `NOME_LOCALIDADE`, `UF`, `BAIRRO`,
`BAIRRO_ABREVIADO` (vacío), `NOME_TIPO_LOGR`/`NOME_TITULO`/`PREPOSICAO`/
`NOME_OFICIAL_LOGR` (descomposición heurística de `calle` —
`agents/logradouro_br.py`), `NOME_LOGR_ABREV` (vacío), `CEP`, `NUMERO`,
`CEP_UNICO` (='N' fijo), `CODIGO_LOGRADOURO` (código municipal del logradouro que
BCIParser extrae del PDF — migración 016; vacío para parcelas parseadas antes),
`COD_LOG_PARA` (vacío), `BASE` (vacío, sin valor definido aún). **Una fila por
dirección completa única** (calle+número+CEP+bairro deduplicados — varias parcelas con
la misma dirección colapsan en un registro), ordenado por calle y número; las parcelas
sin calle se excluyen.

**Comparativa con relevamiento anterior (📊):** cada relevamiento tiene en su detalle un
selector **"Comparar con…"** que ofrece (a) los surveys anteriores de la misma región —
incluidos los archivados — y (b) los **baselines importados** (el CSV del relevamiento
anterior del cliente, subido con **columnas de nombre fijo** —ver abajo—; Excel debe guardarse
como CSV antes — no hay openpyxl). El resultado muestra KPIs de delta (UF viv/com antes→ahora,
nuevas/cambiaron/desaparecidas, Δhabitantes estimado por hab/domicilio del censo), pinta
el mapa por estado (verde=nueva, ámbar=cambió, gris=igual; desaparecidas en tabla) y
exporta **CSV Comparativa**. Matching: entre surveys va por `cca_code` (exacto) con
dirección como fallback; contra baseline va por dirección normalizada
(`agents/direccion_norm.py`: tipos de vía/títulos canonicalizados ES+PT, complementos
recortados) exacta + fuzzy difflib (≥0.78, misma altura). Endpoints:
`GET /api/surveys/{id}/comparativa/opciones`, `GET …/comparativa?contra_tipo&contra_id`,
`GET …/export/csv-comparativa`, `POST …/baselines/preview`, `POST …/baselines`,
`DELETE /api/baselines/{id}`. Agente: `comparativa_reporter` (también por RPC).

**Crear como "actualización" (sobre el CSV anterior geocodificado):** el formulario "Nueva
zona" tiene un toggle **⦿ Nueva zona / ◯ Actualización de un relevamiento anterior**. En
modo *actualización* el operador sube el **CSV del relevamiento anterior** (sin coordenadas).
**Ya NO hay mapeo manual de columnas:** el CSV debe traer las columnas con el **nombre exacto
de la operadora** (case/acento-insensible), y el backend las resuelve solo (`_resolver_columnas`
+ `_BASELINE_COLUMNAS`/`_BASELINE_OBLIGATORIAS` en `web/app.py`). Si falta una **obligatoria**,
el import se **rechaza** nombrándola (no crea región ni baseline). Contrato de columnas:
- **Obligatorias:** `DSC_ENDERECO_COMPLETO` (dirección), `DSC_CIDADE` (ciudad), `COD_UF`
  (estado/UF — acepta sigla `MT`, código IBGE `51` o nombre `Mato Grosso` vía `sigla_uf`),
  `NUM_CEP` (CEP — señal de **máxima precisión** en Brasil, va a geocodebr + texto de
  Nominatim/Mapbox/Google), `DSC_TIPO_IMOVEL` (tipo de inmueble → deriva UF viv/com).
- **Opcionales:** `DSC_BAIRRO`, `DSC_STATUS_CONTRATO`, `COD_NODE`.

La **ciudad sale siempre de `DSC_CIDADE`** (ya no hay campo manual): `_aplicar_ciudad_baseline`
rellena las filas vacías con la predominante y rechaza si no hay ninguna. Esa ciudad detecta el
país. El `mapeo_d` interno resuelto se sigue guardando en `baselines.mapeo` (lo usa la
re-exportación del CSV Operadora). El **mismo contrato de columnas** rige el import de baseline
para comparar (`POST …/baselines`). El wizard muestra el contrato y valida ✓/✗ por columna antes
de habilitar el botón. Al confirmar, el backend crea la región + persiste el baseline y
lanza `BaselineGeocoder` en background para **geocodificar cada dirección**
(agregando la ciudad de su fila a cada consulta). **Mientras geocodifica, el wizard muestra
un log en vivo con fecha/hora de cada paso** (panel "🕓 Detalle en vivo"): los logs del
geocoder se taggean al `baseline_id` (thread-local `_thread_job_id`) y el front los pollea
en `GET /api/baselines/{id}/activity?since=` además de la barra de progreso — así el operador
ve qué fuente está usando (geocodebr paso 0, progreso cada 50, interpolación, "listo"), si
quedó trabado o el detalle textual de un error. Cuando termina, el wizard **grafica los puntos en un mapa** — el relevamiento anterior se
dibuja como **cuadrados grises con la cantidad de UF en número** — y el operador **dibuja
in-app** (Leaflet-Geoman) el polígono de la **nueva zona** (sector) sobre ellos, mientras
sigue viendo todo el relevamiento viejo. Ese polígono queda como `regions.zone_geojson` (la
nueva zona a relevar) y se crea el survey en `stopped`, listo para ▶ Iniciar — se comporta
como cualquier relevamiento normal.

**Definir la zona por CALLE + RANGO DE ALTURAS (modo alternativo a dibujar):** tras
geocodificar, el wizard ofrece dos modos: "⬚ Dibujar zona" (el de arriba) y **"🛣️ Por calle +
rango (autodetectado)"**. En el segundo, `GET /api/baselines/{id}/calles`
(`_detectar_calles_baseline`) agrupa el relevamiento anterior por calle normalizada
(`agents/direccion_norm.normalizar_calle`) y saca el **rango de numeración** (min/max), con el
**tope extendido** (+20%, mín +50) para captar obra nueva. El operador edita esa tabla
(rangos, excluir/agregar calles) y **previsualiza** la zona (`POST …/calles/preview`) →
`POST …/crear-survey-calles` construye el **polígono de descarga** (`_poligono_de_calles`).
**Garantiza, por CADA dirección, su CUADRA**: un bloque de **100 m de largo × 40 m de ancho**
(±50 m sobre el eje real de la calle, ±20 m a cada lado) orientado según el eje OSM; la unión
da el corredor (calles densas → segmento continuo; dirección aislada → igual su cuadra).
Reusa el **mismo eje OSM cacheado** que el geocoding (`baseline_interp._osm_geometrias` +
`_stitch`: cosido y sin carriles duplicados; caché `calle_geometria` mig. 041 por ciudad/calle,
clave = `calle_norm` recomputado) → consistente y sin re-consultar Overpass si la ciudad ya está
cacheada. Suma un **blob de garantía** de 20 m por punto (si el geocode cae a >20 m del eje no
queda afuera). SmartGIS baja por **intersección**, así el corredor fino agarra las parcelas de
las dos veredas. Queda como `regions.zone_geojson` y guarda la lista en
**`surveys.scope_calles`** (JSONB, migración 040). **La zona autogenerada es EDITABLE**: el
wizard la muestra en un mapa con Geoman (mover vértices, sumar/quitar polígonos) y `crear-survey-calles`
acepta `zone_geojson` (el polígono ajustado) como override. El relevamiento baja por ese polígono y,
**ya con las direcciones (BCI)**, el agente **`ScopeCallesFilter`** (rpc `scope_calles_filter`,
paso del `VGPipelineRunner` tras BCIParser) **borra las parcelas cuya calle no esté en el scope
o cuyo número quede fuera del rango** (conserva las de calle NULL). Así el alcance es **estricto
por calle+altura, no por polígono dibujado**. Ver memoria [[project_scope_calles]].

**Comparación viejo/nuevo sobre el mapa:** el survey
queda vinculado al baseline (`surveys.baseline_id`, migración 024); el **mapa del survey
grafica el relevamiento anterior por debajo** de las parcelas nuevas (pane z-index 350 < 400)
como **marcadores blancos con borde violeta**: un marcador por ubicación (agrupado por
coordenada). Punto con UNA dirección → cuadrado con su UF; punto con VARIAS direcciones
(el geocoding gratuito colapsa números de la misma calle, o varias unidades de una dirección)
→ círculo con la **cantidad de direcciones**; al clickear, la(s) **dirección(es)
completa(s)** (`direccion_raw`) con su UF. Las columnas del CSV anterior son fijas (ver el
contrato arriba: `DSC_ENDERECO_COMPLETO`/`DSC_CIDADE`/`COD_UF`/`NUM_CEP`/`DSC_TIPO_IMOVEL`).
**La UF se deriva del tipo de inmueble (`DSC_TIPO_IMOVEL`) agregando por dirección**
(`_agregar_por_direccion`): cada entrada residencial cuenta 1 vivienda, el resto
(comercial/otros) 1 comercio — así una dirección con N unidades suma su UF. Endpoint de los puntos:
`GET /api/baselines/{id}/puntos`. El CSV anterior queda
**auto-vinculado como baseline** de la región, así "Comparar con…" lo ofrece sin reimportar.
El país se autodetecta geocodificando unas pocas direcciones (necesario para sesgar el
geocoding); en Brasil se completa `municipio_codigo` del centroide del polígono. Endpoints:
`POST /api/actualizaciones/preview`, `POST /api/actualizaciones/preparar`,
`GET /api/baselines/{id}/geocoding` (poll de progreso),
`POST /api/baselines/{id}/crear-survey`.

**Relevamientos parciales (🔁 Sub-zona):** para re-relevar SOLO una parte de una región
ya relevada, el botón "🔁 Sub-zona" del detalle (operador) sube un polígono GeoJSON y
crea un **survey nuevo sobre la misma región** con `surveys.subzona_geojson` (migración
018; `POST /api/surveys/{id}/parcial`, valida que la sub-zona toque la zona de la
región). **Todos los fetchers que filtran por zona (SmartGIS, VGBCI, SaltaCatastro,
ARBA carto/cadastral, GooglePlaces) prefieren la subzona del survey** vía
`COALESCE(s.subzona_geojson, r.zone_geojson)` — no hay que pasar nada extra, solo el
`survey_id`. Los PDFs BCI compartidos se reutilizan. El survey grande queda intacto y
después se comparan con la comparativa. La tarjeta del parcial lleva badge "sub-zona"
y su mapa muestra el polígono de la sub-zona.

**CSV Consolidado (por región):** botón "⬇ CSV Consolidado" en el detalle
(`GET /api/regions/{region_id}/export/csv-consolidado`): combina TODOS los surveys de
la región (completos, parciales y archivados) tomando **el dato más reciente de cada
parcela** (identidad: `cca_code` → clave de dirección → id). Los surveys nunca se
pisan; la consolidación es una vista de lectura. Columna "Relevado el" con la fecha
del survey que aportó cada fila.

**Los relevamientos NUNCA se borran — se archivan (📦):** requisito del cliente: el
relevamiento anterior siempre debe quedar para comparar. El botón de la tarjeta archiva
(`surveys.archivado`, migración 017; `POST /api/surveys/{id}/archivar`): sale de la lista
(sección plegable "📦 Archivados" del operador) pero sigue en la DB y en el selector de
comparativa. El `DELETE /api/surveys/{id}` físico solo se permite sobre surveys YA
archivados (doble paso, doble confirmación en la UI). El cliente nunca ve archivados.

**UF exacta vs estimada:** la web siempre muestra la cantidad de UF de vivienda y comercio.
Cuando la UF es **estimada** (cualquier `parcelas.uf_fuente` ≠ `bci`) la marca con badge
`est.` y prefijo `≈` en los KPIs, y el popup de cada parcela detalla el origen
(`exacto (BCI)` / `estimado (OSM/proxy/por uso)`). **Al pasar el cursor sobre el KPI de UF
o sobre la línea de origen del popup, un tooltip explica cómo se estimó.** El CSV incluye
la columna **UF Fuente**.
`bci`=exacto (BCIParser, Brasil); `osm`/`proxy`/`uso`=estimado (UnidadesEstimator).

**Establecimientos (1 entidad sobre N parcelas):** cuando una fábrica/colegio/iglesia/galpón
ocupa varias parcelas catastrales, `EstablecimientoAgrupador` las agrupa en la tabla
`establecimientos` y el conteo de UF de la web/CSV/reporter cuenta el establecimiento por la
UF de su **parcela más desarrollada** (no la suma de sus miembros). Así una fábrica sobre 6
lotes de 1 UF cuenta 1, pero una parcela con varias UF reales (`uf_comercio=5`) NO se colapsa.
Las parcelas miembro conservan sus datos y quedan vinculadas por `parcelas.establecimiento_id`;
en el mapa el popup las marca como "parte de establecimiento". El CSV trae las columnas
**Establecimiento (tipo)** y **(nombre)**.

**Origen de los datos (data lineage):** el relevamiento final deja registrado de dónde
salió cada dato, en 4 columnas de origen por parcela (todas exportadas en el CSV de la web):
- `fuente_parcela` (col **Fuente**) — quién aportó la parcela/geometría (smartgis_vg, arba_carto,
  salta_idemsa/idesa, sigef_onr, catastro…).
- `direccion_source` (col **Fuente dirección**) — origen de la dirección (bci_pdf, google_geocode,
  osm, nominatim, ibge, catastro).
- `uf_fuente` (col **UF Fuente**) — origen del conteo de UF (bci/osm/proxy/uso/google).
  `google`=GooglePlacesFetcher (conteo real de comercios; autoritativo para `uf_comercio`).
  `shopping_min`=piso mínimo de `ParcelaCategoria` (ver más abajo, ningún fuente gratuita
  da el conteo real de locales de un shopping).
- `uso_fuente` (col **Uso Fuente**, migración 009) — qué agente clasificó `uso_principal`:
  `bci`=BCIParser, `cpua`=SaltaZonificacionFetcher, `sigsa`=SaltaRegistroFetcher,
  `rentas`=SaltaRentasFetcher, `clasificador`=UsoClassifier, `google`=GooglePlacesFetcher
  (parcela con comercio → comercial/mixto). NULL = sin determinar (datos previos
  a la migración).

**Precedencia de fuentes (obligatorio al escribir `parcelas`):** varias etapas escriben las
MISMAS columnas y **corren más de una vez** (SmartGIS es resumible, BCIParser es la red de
seguridad y se re-corre sobre regiones ya terminadas). Sin guarda explícita gana la última
corrida aunque su dato sea peor, y el `*_fuente` queda **mintiendo** — porque el agente
actualiza el valor pero no siempre el sello. Reglas:
- `COALESCE(:nuevo, viejo)` **NO alcanza**: sólo protege contra el NULL, no contra el dato peor.
  Si tu agente puede correr después de otro que escribe esa columna, poné
  `CASE WHEN <sello> IN (…) THEN <viejo> ELSE … END`.
- **`manual` nunca se pisa** (corrección del operador desde el panel de incidencias: es lo único
  irreconstruible). Para `uf_*` tampoco `cadastur`/`google`/`shopping_min`, que son posteriores
  al BCI y más específicos para su campo.
- El que cambia un valor tiene que actualizar **su sello en la misma sentencia**.
- Para detectar un pisado silencioso, buscar filas donde el sello dice una fuente y el texto
  tiene la huella de otra (erratas, espaciado, formato). Ver memoria
  `project_precedencia_fuentes_parcela`.

**Levantar el servidor:**
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
uvicorn scrapitero.web.app:app --host 0.0.0.0 --port 8765
```

## Stack
- Python 3.12, venv en `/opt/scrapitero/.venv`
- PostgreSQL+PostGIS en Docker (`scrapitero_db`), accesible en `localhost:5432` desde el host
- Hermes Agent en Docker (orquestador de producción), Python 3.13
- Web: FastAPI + uvicorn en puerto 8765
- Repo: github.com/Meter0r0/Scrapitero
