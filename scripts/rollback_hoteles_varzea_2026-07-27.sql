-- =====================================================================================
-- ROLLBACK de las correcciones manuales de hoteles aplicadas el 2026-07-27
-- sobre la region `zona-varzea-grande-update` (survey d6e59b97, relevamiento del 14-jul).
--
-- Deja las filas exactamente como estaban antes de la sesion. Todos los valores originales
-- fueron capturados de la base ANTES de aplicar cada cambio.
--
-- Uso:
--   docker exec -i -e PGPASSWORD=... scrapitero_db psql -U scrap -d scrapitero \
--     -v ON_ERROR_STOP=1 < scripts/rollback_hoteles_varzea_2026-07-27.sql
--
-- Cambios que revierte (en orden inverso al que se aplicaron):
--   1. PORTAL DA AMAZONIA / Sao Bento (33316343000119) — cerrado por duplicado
--   2. PORTAL DA AMAZONIA / Couto 400 (01071359000112) — mudado y vinculado a parcela
--   3. HOTEL TAINA (14008321000147) — coordenada corregida 698 m
--   4. HOTEL SAN MARINO (07024384000121) — cerrado por SUSPENSA en Receita
--   5. REAL VILLES duplicado (07194645000151) — cerrado por BAIXADA + leitos al vigente
--   6. SANTOS DUMONT (00791459000150) — cerrado por ser el CNPJ anterior del Express
--      y HOTEL EXPRESS (05577851000115) — mudado y vinculado
--   7. REAL VILLES 710 (33474110000144) — 10 habitaciones manuales
--
-- OJO: la fila de `hotel_ubicacion_manual` del CNPJ 33474110000144 es ANTERIOR a esta
-- sesion (cargada el 2026-07-27 03:20) y NO se borra.
-- =====================================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ── 1. PORTAL DA AMAZONIA / Sao Bento ────────────────────────────────────────────────
UPDATE hoteles SET cerrado_def = false, nota = NULL
 WHERE region_id = 'zona-varzea-grande-update' AND cnpj = '33316343000119';

-- ── 2. PORTAL DA AMAZONIA / Couto Magalhaes 400 ──────────────────────────────────────
UPDATE hoteles
   SET location   = ST_SetSRID(ST_MakePoint(-56.1236324, -15.643005), 4326),
       direccion  = 'Couto Magalhães  Várzea Grande Centro-Norte Centro-Norte',
       parcela_id = NULL
 WHERE region_id = 'zona-varzea-grande-update' AND cnpj = '01071359000112';

-- ── 3. HOTEL TAINA ───────────────────────────────────────────────────────────────────
UPDATE hoteles
   SET location  = ST_SetSRID(ST_MakePoint(-56.123778, -15.643944), 4326),
       direccion = 'GOV  JOAO PONCE DE ARRUDA  Várzea Grande Centro-Norte'
 WHERE hotel_id = '87e404c0-c793-49bc-91bc-8de439ea9221';

-- ── 4. HOTEL SAN MARINO ──────────────────────────────────────────────────────────────
UPDATE hoteles SET cerrado_def = false, nota = NULL
 WHERE region_id = 'zona-varzea-grande-update' AND cnpj = '07024384000121';

-- ── 5. REAL VILLES duplicado (+ leitos que se le copiaron al vigente) ────────────────
UPDATE hoteles SET cerrado_def = false, nota = NULL
 WHERE hotel_id = 'a509bfdc-186b-424a-b033-c36f01aa8814';

UPDATE hoteles SET leitos = NULL
 WHERE hotel_id = 'bb7d6b26-92a6-4367-b944-f7e7b2a0682a';

-- ── 6. SANTOS DUMONT + HOTEL EXPRESS ─────────────────────────────────────────────────
UPDATE hoteles SET cerrado_def = false, nota = NULL
 WHERE hotel_id = '573af0a8-c1e1-42f8-ab1f-1ddfbf1af90a';

UPDATE hoteles
   SET location   = ST_SetSRID(ST_MakePoint(-56.123778, -15.643944), 4326),
       direccion  = 'Governador João Ponce de Arruda (Lot Centro) Várzea Grande Centro-Norte Centro-Norte',
       parcela_id = NULL
 WHERE hotel_id = '0a4e2dba-db4a-4965-a765-5da1d7e0a72a';

UPDATE incidencias SET estado='pendiente', resolucion=NULL, nota=NULL, autor=NULL,
       resuelta_at=NULL, actualizada_at=now()
 WHERE incidencia_id = 'da7b81fb-f9c2-40c9-8197-41172dae4e11';

-- ── 7. REAL VILLES 710 ───────────────────────────────────────────────────────────────
UPDATE hoteles SET habitaciones = NULL, habitaciones_fuente = NULL
 WHERE hotel_id = 'bb7d6b26-92a6-4367-b944-f7e7b2a0682a';

UPDATE incidencias SET estado='pendiente', resolucion=NULL, nota=NULL, autor=NULL,
       resuelta_at=NULL, actualizada_at=now()
 WHERE incidencia_id = 'e645929b-5496-4b46-aaad-10ea849f36e5';

-- ── Overrides durables creados en la sesion ──────────────────────────────────────────
DELETE FROM hotel_habitaciones_manual
 WHERE region_id = 'zona-varzea-grande-update' AND cnpj = '33474110000144';

DELETE FROM hotel_cerrado_manual
 WHERE region_id = 'zona-varzea-grande-update'
   AND cnpj IN ('00791459000150','07194645000151','07024384000121','33316343000119');

-- NO se borra 33474110000144: esa fila es anterior a la sesion.
DELETE FROM hotel_ubicacion_manual
 WHERE region_id = 'zona-varzea-grande-update'
   AND cnpj IN ('05577851000115','14008321000147','01071359000112');

COMMIT;
