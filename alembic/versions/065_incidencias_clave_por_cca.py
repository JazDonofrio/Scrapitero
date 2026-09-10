"""065 — la incidencia de una parcela se identifica por su inscrição, no por el `parcela_id`

`IncidenciasReporter` es idempotente por `(survey_id, tipo, clave)` y la clave de un caso de
parcela era `<tipo>:<parcela_id>`. El `parcela_id` es un UUID **que se regenera cuando la
parcela se vuelve a bajar del catastro**: cada re-scrape huerfanaba lo que el operador ya
había cerrado y el mismo caso reaparecía como incidencia nueva.

Medido el 9-sep-2026, después de re-parsear el BCI en las 18 regiones de Várzea Grande:
**118 incidencias de parcela apuntaban a parcelas que ya no existen en su survey**, todas en
VAZ049 —que se creó portando el relevamiento, lo que regeneró los `parcela_id`—, y **5 de
ellas estaban resueltas** por el operador (4 `altura_sin_declarar` y 1 `uf_sin_declarar`). Las
había cerrado y volvían a la lista, que es justamente lo que la clave natural venía a evitar.

(La primera medición dio 35 resueltas y estaba mal: contaba también las `geocoding_dudoso`,
cuya clave es el id de una fila de `baseline_direcciones` y por eso nunca aparece en
`parcelas`. Verificado: sus 29 resueltas apuntan a direcciones que existen. Al medir orfandad
de claves hay que separar por tipo, porque cada uno apunta a una tabla distinta.)

Es el mismo problema que `parcela_direccion_manual` (mig. 052) resolvió clavando por
`(region_id, cca_code)`: **la inscrição la asigna el municipio y sobrevive al re-scrape**.

Esta migración traduce las claves existentes de `<tipo>:<uuid>` a `<tipo>:cca:<inscrição>`.
No cambia ningún estado ni ninguna nota: sólo re-apunta la fila para que el próximo reporte
la reconozca como el mismo caso.

Dos guardas, las dos medidas antes de escribir esto:

* **La inscrição repetida no sirve de clave.** Hay catastros que reparten una misma inscrição
  en varios polígonos (en PBA, José León Suárez 1397 y 1466 comparten CCA a 70 m; en
  `zona-salta-centro-t1` hay una en nueve parcelas). Esas filas se quedan con `parcela_id`, o
  dos casos distintos colapsarían en uno. Hoy no hay ninguna incidencia en esa situación,
  pero la guarda va igual porque el agente la aplica.
* **Colisiones**: 76 grupos quedaban con dos filas para el mismo caso. Se conserva una sola,
  por valor de lo que tiene adentro — `resuelta` > `pendiente` > `obsoleta`, y a igual estado
  la más reciente. De los 76, 72 son `(obsoleta, pendiente)` —se queda la vigente— y **4 son
  `(pendiente, resuelta)`, donde se conserva la decisión del operador**: si el caso sigue
  vigente, el reporte siguiente le refresca el texto sin reabrirlo, y si dejó de estarlo pasa
  a `obsoleta` como cualquier otro.

Las parcelas sin inscrição (las que no salen del catastro: `shopping_poi`, baseline) se
quedan con `parcela_id`, que es lo único que las identifica.

Revision ID: 065
Revises: 064
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op

revision: str = "065"
down_revision: Union[str, None] = "064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Los tipos cuya clave es una parcela. Los demás ya usan una clave estable por su cuenta:
# hoteles por CNPJ/nombre, POIs por `place_id`, geocoding por el id de la dirección.
_TIPOS = ("altura_sin_declarar", "altura_mas_alta", "numero_faltante",
          "uf_imposible", "uf_sin_declarar")

# Candidatas: incidencia cuya clave apunta a una parcela localizable CON inscrição, y cuya
# inscrição identifica a una sola parcela dentro del survey de la incidencia.
# `strpos` y no `LIKE '%...%'`: psycopg busca placeholders en todo el texto de la sentencia
# —comentarios SQL incluidos— y aborta al ver un `%` que no sea `%s`/`%b`/`%t`.
_CANDIDATAS = """
    CREATE TEMP TABLE _reclave AS
    SELECT i.incidencia_id,
           i.survey_id,
           i.tipo,
           i.estado,
           i.resuelta_at,
           i.actualizada_at,
           i.tipo || ':cca:' || p.cca_code AS clave_nueva
    FROM incidencias i
    JOIN parcelas p ON p.parcela_id::text = split_part(i.clave, ':', 2)
    WHERE i.tipo = ANY(:tipos)
      AND p.cca_code IS NOT NULL AND p.cca_code <> ''
      AND strpos(i.clave, ':cca:') = 0   -- no re-clavar lo ya migrado
      AND (SELECT count(*) FROM parcelas p2
            WHERE p2.survey_id = i.survey_id AND p2.cca_code = p.cca_code) <= 1
"""

# De cada grupo que colisiona se conserva la fila con más trabajo humano adentro.
_BORRAR_COLISIONES = """
    DELETE FROM incidencias
    WHERE incidencia_id IN (
        SELECT incidencia_id FROM (
            SELECT incidencia_id,
                   row_number() OVER (
                       PARTITION BY survey_id, tipo, clave_nueva
                       ORDER BY CASE estado WHEN 'resuelta' THEN 0
                                            WHEN 'pendiente' THEN 1
                                            ELSE 2 END,
                                COALESCE(resuelta_at, actualizada_at) DESC NULLS LAST
                   ) AS rn
            FROM _reclave
        ) r WHERE r.rn > 1
    )
"""

_APLICAR = """
    UPDATE incidencias i
       SET clave = r.clave_nueva
      FROM _reclave r
     WHERE r.incidencia_id = i.incidencia_id
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CANDIDATAS.replace(":tipos", "'{%s}'" % ",".join(_TIPOS)))
    conn.exec_driver_sql(_BORRAR_COLISIONES)
    # Las filas borradas siguen en la temp table: sacarlas antes del UPDATE.
    conn.exec_driver_sql("""
        DELETE FROM _reclave r
         WHERE NOT EXISTS (SELECT 1 FROM incidencias i
                            WHERE i.incidencia_id = r.incidencia_id)
    """)
    conn.exec_driver_sql(_APLICAR)
    conn.exec_driver_sql("DROP TABLE _reclave")


def downgrade() -> None:
    """No se vuelve atrás.

    La traducción no es reversible fila por fila: el `parcela_id` que tenía cada clave era
    justamente el que dejó de existir, y las filas que colisionaban se fusionaron. Volver
    atrás significaría inventar los UUID viejos, que es peor que quedarse acá — y la clave
    nueva la entiende igual el reporte anterior, que simplemente la trataría como un caso
    nuevo.
    """
    pass
