/* ════════════════════════════════════════════════════════════════════════════
   AI Mapping — idioma de la interfaz (español por default, portugués opcional)
   ════════════════════════════════════════════════════════════════════════════

   El cliente del relevamiento es brasilero: la UI tiene que poder leerse en
   portugués sin dejar de estar disponible en español.

   ── LA CONVENCIÓN (importante) ──────────────────────────────────────────────
   **La clave de traducción ES el texto en español.** No hay claves simbólicas
   tipo `form.nueva.titulo`: el diccionario mapea la frase española directamente
   a la portuguesa.

       t('Parcelas')  →  'Lotes'   (en pt)  ·  'Parcelas'  (en es)

   Tres consecuencias que conviene tener presentes:

   1. **Si falta la traducción, sale el español.** Nunca `undefined`, nunca
      vacío, nunca una clave cruda en pantalla. Por eso se puede traducir de a
      lotes: el estado intermedio es "portugués parcial", jamás "roto".

   2. **Si cambiás una frase en index.html/app.py, actualizá la clave acá.**
      Es el modo de falla número uno de esta convención: la traducción deja de
      matchear y desaparece EN SILENCIO. `scripts/i18n_audit.py` lo detecta
      (chequeo de claves huérfanas) — corrélo al tocar texto de la UI.

   3. **El español del código sigue siendo legible.** No hay que abrir el
      diccionario para saber qué dice un botón.

   ── ESCAPING: se escapa la VARIABLE, nunca la traducción ────────────────────
   Varias traducciones contienen HTML a propósito (los párrafos de ayuda con
   <strong>/<br>). Envolverlas en escHtml() las convertiría en tags literales
   visibles. La regla es al revés:

       ✓  t('Se relevaron {n} parcelas', {n: escHtml(dato)})
       ✗  escHtml(t('Se relevaron {n} parcelas', {n: dato}))

   Y dentro de un atributo HTML va `tAttr`, no `t`: una traducción con comillas
   dobles rompería el atributo y con él todo el template literal.

       ✓  `title="${tAttr('Archivar el relevamiento')}"`
       ✗  `title="${t('Archivar el relevamiento')}"`

   ── CARGA ───────────────────────────────────────────────────────────────────
   Script clásico, no módulo: los módulos son diferidos por definición y el JS
   de index.html es inline y corre al parsear, así que `t` tiene que existir
   antes. Se carga en el <head> y define globales.

   Quien lo incluya debe poner además el fallback de `typeof t !== 'function'`
   (ver index.html): si este archivo no carga, el primer `t()` de nivel de
   módulo tiraría ReferenceError al parsear y mataría la app entera.
   ════════════════════════════════════════════════════════════════════════════ */

/* ── Idioma activo ──────────────────────────────────────────────────────────
   Se resuelve al cargar, antes del primer pintado — mismo patrón que el tema
   claro/oscuro. SIN autodetección de navegador ni de país: el default es
   español y la elección del usuario manda (decisión de producto).

   Se sella en `documentElement.lang` y no en un `data-*` porque es el atributo
   semánticamente correcto (ya existe en el HTML) y porque así el CSS puede
   pintar el toggle con `:root[lang="pt"]` sin una línea de JS, igual que
   `:root[data-theme="dark"]` pinta el sol/luna. */
var LANG = 'es';
try {
  var _stored = localStorage.getItem('aim-lang');
  if (_stored === 'pt' || _stored === 'es') LANG = _stored;
} catch (e) {}
document.documentElement.lang = LANG;

/* Locale para toLocaleString/toLocaleDateString (separadores y nombres de mes).
   OJO: no es lo mismo que LANG en todos lados — hay valores monetarios en R$
   que van fijos en 'pt-BR' aunque la UI esté en español (ver index.html, el
   popup de valor venal). */
var I18N_LOCALE = LANG === 'pt' ? 'pt-BR' : 'es-AR';

/* Normaliza espacios para que reflowear un párrafo en el HTML no rompa la
   clave. Se aplica a AMBOS lados: al texto leído del DOM y al índice de claves
   del diccionario. */
function _i18nNorm(s) {
  return String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
}

/* Índice normalizado del diccionario, construido una sola vez. Permite que una
   clave escrita en varias líneas en este archivo matchee un nodo del DOM
   indentado de otra forma. */
var _I18N_NORM = null;
function _i18nIndex() {
  if (_I18N_NORM) return _I18N_NORM;
  _I18N_NORM = Object.create(null);
  for (var k in I18N_PT) {
    if (Object.prototype.hasOwnProperty.call(I18N_PT, k)) _I18N_NORM[_i18nNorm(k)] = I18N_PT[k];
  }
  return _I18N_NORM;
}

/* ── t(es, vars?) — traduce texto ───────────────────────────────────────────
   `vars` interpola placeholders con nombre: t('Error: {msg}', {msg: e}).

   Con nombre y no posicional (%s/{0}) porque el portugués reordena la frase, y
   así se puede traducir sin tocar el call site. `{}` no colisiona con el `${}`
   de los template literals, así que las claves siguen legibles adentro de uno.

   La clave TIENE que ser un literal estático: `t(\`Error: ${x}\`)` genera una
   clave distinta por cada valor de x — no matchea nunca y además es imposible
   de auditar. Por eso existe la interpolación de este lado.

   Detalles deliberados:
   · hasOwnProperty y no `I18N_PT[es] ||` — con `||` una traducción vacía cae al
     español en silencio, y claves como 'constructor' o 'toString' devolverían
     basura del prototipo (t() recibe entrada no controlada en t(data.error)).
   · un placeholder sin valor se deja visible ({msg}) en vez de volverse
     "undefined": el error se ve de inmediato en pantalla. */
function t(es, vars) {
  var dict = LANG === 'pt' ? I18N_PT : null;
  var out = es;
  if (dict && Object.prototype.hasOwnProperty.call(dict, es)) {
    out = dict[es];
  } else if (dict) {
    var norm = _i18nIndex()[_i18nNorm(es)];
    if (norm !== undefined) out = norm;
  }
  out = String(out == null ? '' : out);
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m;
    });
  }
  return out;
}

/* ── tAttr(es, vars?) — traduce para meter en un atributo HTML ──────────────
   Igual que t() pero escapa &, < y " — sin esto, la primera traducción que
   contenga una comilla doble rompe el atributo y con él el markup entero del
   template literal, con un fallo visual difícil de rastrear. */
function tAttr(es, vars) {
  return t(es, vars).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

/* ── Walker del markup estático ─────────────────────────────────────────────
   Contrato de atributos:

     data-i18n              → traduce textContent. El elemento NO puede tener
                              hijos-elemento (envolvé el texto en un <span>).
     data-i18n-html         → traduce innerHTML. Para los párrafos de ayuda que
                              llevan <strong>/<u>/<br> adentro.
     data-i18n-attr="a,b"   → traduce esos atributos (vía tAttr). Combinable.

   Es **idempotente y bidireccional**: la primera pasada guarda el original en
   un WeakMap y todas las búsquedas parten de ahí, así que se puede llamar N
   veces y alternar es↔pt sin degradar. Lo necesita el login, que cambia de
   idioma en vivo (no puede recargar: es la respuesta a un POST).

   WeakMap y no dataset: no ensucia el HTML inspeccionable y guarda HTML crudo
   sin pelear con el escaping.

   Clave faltante ⇒ el nodo NO se toca. Nunca lo vacía. */
var _i18nSrc = new WeakMap();

function i18nApply(root) {
  var scope = root || document;
  var nodes = scope.querySelectorAll('[data-i18n],[data-i18n-html],[data-i18n-attr]');
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var src = _i18nSrc.get(el);
    if (!src) {
      src = { text: el.textContent, html: el.innerHTML, attrs: {} };
      var lista = (el.getAttribute('data-i18n-attr') || '').split(',');
      for (var j = 0; j < lista.length; j++) {
        var a = lista[j].trim();
        if (a) src.attrs[a] = el.getAttribute(a);
      }
      _i18nSrc.set(el, src);
    }
    if (el.hasAttribute('data-i18n')) {
      var txt = t(_i18nNorm(src.text));
      if (txt !== undefined) el.textContent = txt;
    }
    if (el.hasAttribute('data-i18n-html')) {
      var htm = t(_i18nNorm(src.html));
      if (htm !== undefined) el.innerHTML = htm;
    }
    for (var attr in src.attrs) {
      if (!Object.prototype.hasOwnProperty.call(src.attrs, attr)) continue;
      if (src.attrs[attr] == null) continue;
      el.setAttribute(attr, t(_i18nNorm(src.attrs[attr])));
    }
  }
}

/* ── i18nSetLang(lang) — persiste y re-aplica ───────────────────────────────
   NO recarga: quien llame decide si hace falta (el dashboard sí recarga porque
   el 96% de su texto se genera en funciones de render; el login no puede). */
function i18nSetLang(lang) {
  if (lang !== 'es' && lang !== 'pt') return;
  LANG = lang;
  I18N_LOCALE = LANG === 'pt' ? 'pt-BR' : 'es-AR';
  try { localStorage.setItem('aim-lang', LANG); } catch (e) {}
  document.documentElement.lang = LANG;
  i18nApply(document);
}

/* ════════════════════════════════════════════════════════════════════════════
   DICCIONARIO ES → PT
   ════════════════════════════════════════════════════════════════════════════
   Las secciones espejan el orden de index.html para poder ubicarse rápido.

   NO se traduce (vocabulario contractual del cliente brasilero, ya en
   portugués): la taxonomía de TIPOS_EDIFICACION (RESIDÊNCIA, APARTAMENTO,
   LOTE VAZIO, COMÉRCIO EM GERAL…), los tipos de hotel de hotelTipoLabel
   (HOTEL/MOTEL/FLAT/PENSÃO) y las columnas DSC_ y COD_ del CSV de operadora
   (escritas sin comodín a propósito: un `*` seguido de `/` cerraría este comentario).
   Tampoco los datos del relevamiento (nombres de calle, propietarios, etc.).
   ════════════════════════════════════════════════════════════════════════════ */
var I18N_PT = {

  /* ── Estado del relevamiento (statusLabel) ──────────────────────────────── */
  'En curso': 'Em andamento',
  'Deteniendo…': 'Parando…',
  'Detenido': 'Parado',
  'Parcial': 'Parcial',
  'Completado': 'Concluído',
  'Error': 'Erro',

  /* ── UF: cómo se estimó (tooltips del KPI y del popup de parcela) ───────── */
  'UF estimada (no es un conteo exacto). Se calcula por parcela: 1) conteo real de OSM (building:flats) si existe; 2) si no, proxy geométrico = área del footprint × pisos ÷ tamaño típico (vivienda 80 m², comercio 50 m²), solo para edificios en altura; 3) si la parcela no tiene edificios en OSM, mínimo según su uso. Pasá el cursor sobre cada parcela del mapa para ver el método usado.':
    'UF estimada (não é uma contagem exata). Calcula-se por lote: 1) contagem real do OSM (building:flats) se existir; 2) senão, proxy geométrico = área da pegada × pavimentos ÷ tamanho típico (moradia 80 m², comércio 50 m²), apenas para edifícios em altura; 3) se o lote não tem edifícios no OSM, o mínimo conforme seu uso. Passe o cursor sobre cada lote do mapa para ver o método usado.',
  'Conteo exacto extraído del PDF BCI oficial (Brasil).':
    'Contagem exata extraída do PDF do BCI oficial (Brasil).',
  'Conteo real declarado en OpenStreetMap (building:flats / addr:units).':
    'Contagem real declarada no OpenStreetMap (building:flats / addr:units).',
  'Estimado por geometría: área del footprint × pisos ÷ tamaño típico (vivienda 80 m², comercio 50 m²). Solo se subdivide la huella en edificios de 2+ pisos o tipo apartments; un edificio de 1 planta cuenta como 1 unidad.':
    'Estimado por geometria: área da pegada × pavimentos ÷ tamanho típico (moradia 80 m², comércio 50 m²). A pegada só é subdividida em edifícios de 2+ pavimentos ou do tipo apartments; um edifício térreo conta como 1 unidade.',
  'Estimado por uso: la parcela no tiene edificios en OpenStreetMap, se asignó el mínimo según su uso (residencial→1 vivienda, comercial→1 comercio).':
    'Estimado por uso: o lote não tem edifícios no OpenStreetMap, atribuiu-se o mínimo conforme seu uso (residencial→1 moradia, comercial→1 comércio).',
  'Conteo real de comercios de Google Maps dentro de la parcela: cada comercio = 1 unidad de comercio (uf_comercio). El uf_vivienda mantiene su estimación previa.':
    'Contagem real de comércios do Google Maps dentro do lote: cada comércio = 1 unidade de comércio (uf_comercio). O uf_vivienda mantém sua estimativa anterior.',

  /* ── Pasos del pipeline (etiquetas cortas de la barra) ──────────────────────
     Las primeras son nombres propios de la fuente y salen igual en los dos idiomas;
     van listadas igual para que el chequeo (C) de scripts/i18n_audit.py pueda exigir
     que TODO texto envuelto en t() tenga una decisión tomada, y no confunda "es igual
     a propósito" con "me olvidé de traducirlo". */
  'OSM': 'OSM',
  'SmartGIS': 'SmartGIS',
  'BCI PDF': 'BCI PDF',
  'Parser': 'Parser',
  'Shoppings': 'Shoppings',
  'Footprints': 'Footprints',
  'Categorías': 'Categorias',
  'Hoteles': 'Hotéis',
  'Habitaciones IA': 'Quartos IA',
  'Altura': 'Altura',

  /* ── Comparativa contra el relevamiento anterior (ESTADO_LABEL) ─────────── */
  'Nueva': 'Nova',
  'Cambió': 'Mudou',
  'Sin cambio': 'Sem alteração',
  'Desaparecida': 'Desapareceu',
  'No comparable': 'Não comparável',

  /* ── Incidencias (INCIDENCIA_LABEL) ─────────────────────────────────────── */
  '🏨 hoteles sin habitaciones': '🏨 hotéis sem quartos',
  '👯 hoteles duplicados (cuentan dos veces)': '👯 hotéis duplicados (contam duas vezes)',
  '🏗 construcción no declarada': '🏗 construção não declarada',
  '🏢 más alto de lo declarado': '🏢 mais alto do que o declarado',
  '⚠ UF que no cabe en lo construido': '⚠ UF que não cabe no construído',
  '📍 ubicación dudosa del relevamiento anterior': '📍 localização duvidosa do levantamento anterior',

  /* ── Categorías de uso del mapa (CAT_LABEL) ─────────────────────────────── */
  'Vivienda': 'Moradia',
  'Edificio de viviendas': 'Edifício residencial',
  'Comercio': 'Comércio',
  'Edificio de comercios': 'Edifício comercial',
  'Mixto': 'Misto',
  'Vacante': 'Vazio',
  'Industrial': 'Industrial',
  'Equipamiento': 'Equipamento',

  /* ── Capas de ítems críticos (ITEMS_CRITICOS) ───────────────────────────── */
  '🏨 Hoteles': '🏨 Hotéis',
  '🏢 Edificios': '🏢 Edifícios',
  '🧱 PH': '🧱 PH',
  '🛍 Shopping': '🛍 Shopping',
  '🏘 Country': '🏘 Condomínio',

  /* ── Capa de altura (ALTURA_MOTIVO_LABEL) ───────────────────────────────── */
  'catastro sin construcción declarada': 'cadastro sem construção declarada',
  'más pisos que lo declarado': 'mais pavimentos do que o declarado',

  /* ── Pasos del pipeline en el detalle (STEP_LABELS) ─────────────────────── */
  '🔍 Evaluando estado inicial': '🔍 Avaliando estado inicial',
  '🔍 Evaluando cobertura': '🔍 Avaliando cobertura',
  '🗺 SmartGIS: descargando parcelas': '🗺 SmartGIS: baixando lotes',
  '📄 BCI: descargando PDFs': '📄 BCI: baixando PDFs',
  '🔎 BCI Parser: extrayendo uso/UF/dirección': '🔎 BCI Parser: extraindo uso/UF/endereço',
  '🏙 ONR: lotes urbanos': '🏙 ONR: lotes urbanos',
  '🌾 ONR/SIGEF: predios rurales': '🌾 ONR/SIGEF: imóveis rurais',
  '🇧🇷 IBGE: setores censitários': '🇧🇷 IBGE: setores censitários',
  '🇧🇷 IBGE: geocoding por interpolación': '🇧🇷 IBGE: geocodificação por interpolação',
  '🗺 ARBA Carto: parcelas PBA': '🗺 ARBA Carto: lotes PBA',
  '🗺 ARBA WFS: parcelas PBA': '🗺 ARBA WFS: lotes PBA',
  '🗺 Catastro Salta: parcelas (WFS)': '🗺 Cadastro Salta: lotes (WFS)',
  '🏘 Zonificación CPUA: uso por zona': '🏘 Zoneamento CPUA: uso por zona',
  '📋 Registro SIGSA: tipo provincial': '📋 Registro SIGSA: tipo provincial',
  '🏗 Rentas DGRM: detectando baldíos': '🏗 Rentas DGRM: detectando terrenos vazios',
  '🏢 OSM: footprints de edificios': '🏢 OSM: pegadas de edifícios',
  '📫 Resolviendo direcciones': '📫 Resolvendo endereços',
  '🏷 Clasificando uso': '🏷 Classificando uso',
  '⬇ Exportando CSV': '⬇ Exportando CSV',
  '📊 Generando reporte': '📊 Gerando relatório',
  '✓ Pipeline completado': '✓ Pipeline concluído',
  '✗ Pipeline terminó con error': '✗ Pipeline terminou com erro',
  '⏹ Pipeline detenido manualmente': '⏹ Pipeline parado manualmente',
  '— Pipeline no definido para este país': '— Pipeline não definido para este país',

  /* ── Métricas del resumen de cada paso (METRIC_LABELS) ──────────────────── */
  'insertadas': 'inseridos',
  'actualizadas': 'atualizados',
  'consultadas': 'consultados',
  'procesadas': 'processados',
  'direcciones': 'endereços',
  'clasificadas': 'classificados',
  'parcelas': 'lotes',
  'baldíos': 'terrenos vazios',
  'edificados': 'edificados',
  'sin match': 'sem correspondência',
  'footprints': 'pegadas',
  'logradouros': 'logradouros',
  'setores': 'setores',
  'PDFs nuevos': 'PDFs novos',
  'PDFs ya existían': 'PDFs já existiam',
  'sin PDF': 'sem PDF',
  'lotes escaneados': 'lotes escaneados',
  'exportadas': 'exportados',
  'errores': 'erros',

  /* ── Header y chrome ────────────────────────────────────────────────────── */
  'AI Mapping — Relevamiento Inteligente': 'AI Mapping — Levantamento Inteligente',
  'DB conectada': 'BD conectado',
  '+ Nuevo relevamiento': '+ Novo levantamento',
  'Cambiar entre tema claro y oscuro': 'Alternar entre tema claro e escuro',
  'Cambiar tema': 'Alternar tema',
  'Cerrar sesión': 'Sair da sessão',
  'Salir': 'Sair',
  'Sin actividad reciente': 'Sem atividade recente',
  'Relevamientos': 'Levantamentos',
  'Cargando…': 'Carregando…',

  /* ── Formulario de nueva zona ───────────────────────────────────────────── */
  'Nueva zona de relevamiento': 'Nova zona de levantamento',
  '✕ Cerrar': '✕ Fechar',
  'Tipo de relevamiento': 'Tipo de levantamento',
  'Nueva zona': 'Nova zona',
  'Actualización de un relevamiento anterior': 'Atualização de um levantamento anterior',
  'Subí el CSV del relevamiento anterior (sin coordenadas): lo geocodificamos para ubicarlo en el mapa y dibujás encima el polígono de la nueva zona a relevar. El CSV queda como base de comparación de la región.':
    'Envie o CSV do levantamento anterior (sem coordenadas): nós o geocodificamos para posicioná-lo no mapa e você desenha por cima o polígono da nova zona a levantar. O CSV fica como base de comparação da região.',
  'Nombre del relevamiento': 'Nome do levantamento',
  'Ej: Barrio Centro, Villa Sur…': 'Ex.: Bairro Centro, Vila Sul…',
  'País': 'País',
  'Por defecto se detecta del GeoJSON; podés forzarlo':
    'Por padrão é detectado do GeoJSON; você pode forçá-lo',
  'Auto-detectar (del GeoJSON)': 'Detectar automaticamente (do GeoJSON)',
  'Área GeoJSON': 'Área GeoJSON',
  'Usá <a href="https://geojson.io" target="_blank" rel="noopener" style="color:hsl(var(--primary))">geojson.io</a> para dibujar el área y exportar el archivo. <br> <strong style="color:var(--c-warn)">⚠ En geojson.io usá siempre la herramienta de <u>Polígono</u> (ícono de pentágono), no la de Línea.</strong> Si usás la herramienta de línea el contorno no cierra correctamente y algunas parcelas del borde quedan excluidas del relevamiento.':
    'Use o <a href="https://geojson.io" target="_blank" rel="noopener" style="color:hsl(var(--primary))">geojson.io</a> para desenhar a área e exportar o arquivo. <br> <strong style="color:var(--c-warn)">⚠ No geojson.io use sempre a ferramenta de <u>Polígono</u> (ícone de pentágono), não a de Linha.</strong> Se usar a ferramenta de linha o contorno não fecha corretamente e alguns lotes da borda ficam de fora do levantamento.',
  'Cancelar': 'Cancelar',
  'Crear relevamiento': 'Criar levantamento',

  /* ── Wizard de actualización ────────────────────────────────────────────── */
  'CSV del relevamiento anterior': 'CSV do levantamento anterior',
  'Si es Excel, primero guardalo como CSV (Archivo → Guardar como → CSV). El CSV debe traer las columnas obligatorias con el nombre exacto de arriba (ya no se mapea a mano).':
    'Se for Excel, primeiro salve como CSV (Arquivo → Salvar como → CSV). O CSV deve trazer as colunas obrigatórias com o nome exato indicado acima (não se mapeia mais à mão).',
  '📍 Geocodificar y graficar': '📍 Geocodificar e plotar',
  '🕓 Detalle en vivo (fecha/hora de cada paso del geocoding)':
    '🕓 Detalhe ao vivo (data/hora de cada etapa da geocodificação)',
  '¿Cómo definís la zona del relevamiento nuevo?': 'Como você define a zona do novo levantamento?',
  '⬚ Dibujar zona en el mapa': '⬚ Desenhar zona no mapa',
  '🛣️ Por calle + rango de alturas (autodetectado)':
    '🛣️ Por rua + faixa de numeração (detectado automaticamente)',
  'Dibujá el polígono de la <strong>nueva zona</strong> con la herramienta ⬟ o ▭ (arriba a la derecha del mapa), sobre los puntos del relevamiento anterior. Podés mover los vértices para ajustarlo, y cambiar a <strong>satélite</strong> con el control de capas (arriba a la izquierda).':
    'Desenhe o polígono da <strong>nova zona</strong> com a ferramenta ⬟ ou ▭ (canto superior direito do mapa), sobre os pontos do levantamento anterior. Você pode mover os vértices para ajustá-lo e mudar para <strong>satélite</strong> no controle de camadas (canto superior esquerdo).',
  'Volver a encuadrar el mapa sobre el relevamiento anterior':
    'Reenquadrar o mapa sobre o levantamento anterior',
  '⤢ Ver todo': '⤢ Ver tudo',
  'Borrar el polígono dibujado y empezar de nuevo': 'Apagar o polígono desenhado e recomeçar',
  '🗑 Borrar zona': '🗑 Apagar zona',
  'Agrandar el mapa para dibujar más cómodo': 'Ampliar o mapa para desenhar com mais conforto',
  '⛶ Agrandar': '⛶ Ampliar',
  'Crear actualización': 'Criar atualização',
  'Calles + rango de alturas detectados del relevamiento anterior (el tope viene <strong>extendido</strong> para captar obra nueva). Editá rangos, destildá calles o agregá una. Se autogenera, por cada dirección, su <strong>cuadra</strong> (100 m de largo × 40 m de ancho, ±20 m del eje).':
    'Ruas + faixa de numeração detectadas do levantamento anterior (o limite superior vem <strong>estendido</strong> para captar obra nova). Edite as faixas, desmarque ruas ou acrescente uma. Gera-se automaticamente, para cada endereço, sua <strong>quadra</strong> (100 m de comprimento × 40 m de largura, ±20 m do eixo).',
  '＋ Agregar calle': '＋ Adicionar rua',
  'Previsualizar zona →': 'Pré-visualizar zona →',
  'Zona autogenerada (una cuadra por dirección). Podés <strong>ajustarla</strong> a las necesidades del relevamiento: arrastrá los vértices, sumá o quitá polígonos con las herramientas de arriba a la derecha. Cuando esté lista, creá el relevamiento.':
    'Zona gerada automaticamente (uma quadra por endereço). Você pode <strong>ajustá-la</strong> às necessidades do levantamento: arraste os vértices, acrescente ou remova polígonos com as ferramentas do canto superior direito. Quando estiver pronta, crie o levantamento.',
  '← Volver a calles': '← Voltar às ruas',

  /* ── Avisos y confirmaciones (alert / confirm) ──────────────────────────── */
  /* Los dos primeros cubren ~24 call sites: el mensaje de error del backend entra
     como {msg} y hoy pasa tal cual (sigue en español, está fuera de alcance). */
  'Error: {msg}': 'Erro: {msg}',
  'Error de red: {msg}': 'Erro de rede: {msg}',
  'desconocido': 'desconhecido',
  '¿Archivar "{nombre}"?\nSale de la lista pero queda guardado y disponible en "Comparar con…". Lo podés restaurar cuando quieras.':
    'Arquivar "{nombre}"?\nSai da lista, mas continua salvo e disponível em "Comparar com…". Você pode restaurá-lo quando quiser.',
  '¿Clonar "{nombre}"?\nSe crea un relevamiento NUEVO y VACÍO con la misma zona, en estado detenido, listo para ▶ Iniciar de cero. El original queda intacto.':
    'Clonar "{nombre}"?\nCria-se um levantamento NOVO e VAZIO com a mesma zona, parado, pronto para ▶ Iniciar do zero. O original fica intacto.',
  'Clon creado: "{nombre}". Buscalo en la lista y apretá ▶ Iniciar.':
    'Clone criado: "{nombre}". Procure-o na lista e clique em ▶ Iniciar.',
  '¿Eliminar DEFINITIVAMENTE el relevamiento "{nombre}"?\nSe borran todas las parcelas, edificios y datos asociados, y deja de estar disponible para comparativas.\nEsta acción no se puede deshacer.':
    'Excluir DEFINITIVAMENTE o levantamento "{nombre}"?\nTodos os lotes, edifícios e dados associados serão apagados, e ele deixa de estar disponível para comparações.\nEsta ação não pode ser desfeita.',
  'Última confirmación: borrar "{nombre}" para siempre.':
    'Última confirmação: apagar "{nombre}" para sempre.',
  'Elegí el relevamiento anterior en el selector.':
    'Escolha o levantamento anterior no seletor.',
  'Seleccioná primero en el selector el baseline importado (📄) que querés eliminar.':
    'Selecione primeiro no seletor a base importada (📄) que deseja excluir.',
  '¿Eliminar este baseline importado?\n(Se puede volver a importar desde el CSV cuando quieras.)':
    'Excluir esta base importada?\n(Você pode reimportá-la do CSV quando quiser.)',
  '¿Eliminar este comentario?': 'Excluir este comentário?',
  'No hay ubicación disponible para este relevamiento.':
    'Não há localização disponível para este levantamento.',
  'El polígono no abarca ninguna dirección del relevamiento anterior.\n\n¿Crear la actualización igual?':
    'O polígono não abrange nenhum endereço do levantamento anterior.\n\nCriar a atualização mesmo assim?',
  '¿Re-escanear el área?\nBuscará parcelas nuevas y actualizará las existentes.\nPuede tardar varios minutos.':
    'Reescanear a área?\nBuscará lotes novos e atualizará os existentes.\nPode levar vários minutos.',
  '¿Iniciar el pipeline completo? (SmartGIS → descarga BCIs)\nEsto puede tardar varios minutos.':
    'Iniciar o pipeline completo? (SmartGIS → baixa os BCIs)\nIsso pode levar vários minutos.',

  /* ── Estados transitorios de botón y mensajes de progreso ───────────────── */
  'Creando…': 'Criando…',
  'Guardando…': 'Salvando…',
  'Crear caso': 'Criar caso',
  'Guardar': 'Salvar',
  '✔ Creado': '✔ Criado',
  'Importar': 'Importar',
  '⟳ Preparando…': '⟳ Preparando…',
  '⟳ Creando…': '⟳ Criando…',
  '⟳ Creando relevamiento…': '⟳ Criando levantamento…',
  '⟳ Construyendo zona (OSM)…': '⟳ Construindo zona (OSM)…',
  '⟳ Comparando…': '⟳ Comparando…',
  '⟳ Importando…': '⟳ Importando…',
  '⟳ Estimando…': '⟳ Estimando…',
  '⟳ escaneando…': '⟳ escaneando…',
  '⟳ Buscando…': '⟳ Buscando…',
  '⟳ OSM…': '⟳ OSM…',
  '⟳ estimando…': '⟳ estimando…',
  '⟳ buscando…': '⟳ buscando…',
  '⟳ midiendo…': '⟳ medindo…',
  '⟳ IA…': '⟳ IA…',
  '⏹ Parando…': '⏹ Parando…',
  '⏹ Parar': '⏹ Parar',
  '↺ Escaneando…': '↺ Escaneando…',
  '▶ Iniciando…': '▶ Iniciando…',
  'Crear relevamiento parcial': 'Criar levantamento parcial',

  'Elegí el archivo GeoJSON del área.': 'Escolha o arquivo GeoJSON da área.',
  'Elegí el archivo GeoJSON de la sub-zona.': 'Escolha o arquivo GeoJSON da sub-zona.',
  'Elegí el archivo primero.': 'Escolha o arquivo primeiro.',
  'Elegí el CSV anterior.': 'Escolha o CSV anterior.',
  'Elegí al menos una calle.': 'Escolha ao menos uma rua.',
  'Poné un nombre al relevamiento.': 'Dê um nome ao levantamento.',
  'Leyendo archivo…': 'Lendo arquivo…',
  'Relevamiento creado. Descargando edificios OSM…':
    'Levantamento criado. Baixando edifícios do OSM…',
  'Creando la región y detectando el país…': 'Criando a região e detectando o país…',
  'Creando el relevamiento…': 'Criando o levantamento…',
  'Generando las cuadras sobre la geometría de OSM…':
    'Gerando as quadras sobre a geometria do OSM…',
  'La zona quedó vacía.': 'A zona ficou vazia.',
  'Sin zona dibujada todavía.': 'Nenhuma zona desenhada ainda.',
  'Dibujá el polígono de la nueva zona primero.': 'Desenhe primeiro o polígono da nova zona.',
  '✅ Relevamiento creado. Listo para iniciar.': '✅ Levantamento criado. Pronto para iniciar.',
  '✓ Relevamiento parcial creado — aparece en la lista, tocá ▶ Iniciar.':
    '✓ Levantamento parcial criado — aparece na lista, clique em ▶ Iniciar.',
  'Buscando incidencias…': 'Buscando ocorrências…',
  'Detectando barrios cerrados en OSM…': 'Detectando condomínios fechados no OSM…',
  'interpolando sobre el eje de cada calle…': 'interpolando sobre o eixo de cada rua…',
  'Google Open Buildings (fallback OSM)…': 'Google Open Buildings (alternativa OSM)…',
  'Google Solar + Elevation…': 'Google Solar + Elevation…',
  'Preguntando a la IA (Gemini + web)…': 'Perguntando à IA (Gemini + web)…',
  'Repartiendo población censal entre las manzanas…':
    'Distribuindo a população censitária entre as quadras…',

  '✓ Relevamiento creado sobre {n} calles. Iniciálo desde la lista.':
    '✓ Levantamento criado sobre {n} ruas. Inicie-o pela lista.',
  '✓ {n} direcciones importadas': '✓ {n} endereços importados',
  ' ({n} filas sin dirección descartadas)': ' ({n} linhas sem endereço descartadas)',
  '. Ya podés compararlo desde el selector.': '. Já pode compará-lo pelo seletor.',
  '{n} pendiente(s)': '{n} pendente(s)',
  ' · {n} nueva(s)': ' · {n} nova(s)',
  ' · {n} obsoleta(s)': ' · {n} obsoleta(s)',
  '{n} hotel(es) · {hab} hab.': '{n} hotel(éis) · {hab} quartos',
  ' · {n} cerrado(s)': ' · {n} fechado(s)',
  '{zona} en zona · {parcela} en parcela · {cerrados} cerrados · {uf} UF':
    '{zona} na zona · {parcela} no lote · {cerrados} fechados · {uf} UF',
  '{areas} área(s) · {n} parcelas': '{areas} área(s) · {n} lotes',
  '{n}/{total} estimadas en {calles} calles': '{n}/{total} estimados em {calles} ruas',
  ' · {n} sin estimar': ' · {n} sem estimativa',
  '{fuente} · {n} footprints · {en} en parcelas': '{fuente} · {n} pegadas · {en} em lotes',
  '{n} con altura · {disc} discrepancia(s)': '{n} com altura · {disc} discrepância(s)',
  ' ({n} sin declarar)': ' ({n} não declaradas)',
  ' · parcial, re-ejecutar': ' · parcial, reexecutar',
  '{n} resuelto(s) · {sin} sin dato · de {total}':
    '{n} resolvido(s) · {sin} sem dado · de {total}',

  /* ── Tarjeta del relevamiento (renderCard) ──────────────────────────────── */
  'Detener pipeline': 'Parar o pipeline',
  'Deteniendo': 'Parando',
  'Volver a escanear el área buscando parcelas nuevas': 'Reescanear a área buscando lotes novos',
  '↺ Re-escanear': '↺ Reescanear',
  'Iniciar pipeline completo': 'Iniciar o pipeline completo',
  '▶ Iniciar': '▶ Iniciar',
  'Detené el pipeline primero': 'Pare o pipeline primeiro',
  'Restaurar a la lista principal': 'Restaurar para a lista principal',
  'Eliminar DEFINITIVAMENTE (borra parcelas y datos; deja de estar disponible para comparar)':
    'Excluir DEFINITIVAMENTE (apaga lotes e dados; deixa de estar disponível para comparar)',
  "Archivar: lo saca de la lista pero queda guardado y disponible en 'Comparar con…'":
    "Arquivar: sai da lista, mas continua salvo e disponível em 'Comparar com…'",
  'Clonar: crea un relevamiento nuevo y vacío con la misma zona, para empezar de cero (el original queda intacto)':
    'Clonar: cria um levantamento novo e vazio com a mesma zona, para começar do zero (o original fica intacto)',
  '{n} parcelas': '{n} lotes',
  'Relevamiento parcial: cubre una sub-zona de la región':
    'Levantamento parcial: cobre uma sub-zona da região',
  'sub-zona': 'sub-zona',
  'Parcelas': 'Lotes',
  'UF Vivienda': 'UF Moradia',
  'UF Comercio': 'UF Comércio',
  'est.': 'est.',
  'Área relevada': 'Área levantada',
  'Tildado = este relevamiento se muestra en la vista cliente (raíz). Destildalo para ocultárselo al cliente.':
    'Marcado = este levantamento aparece na visão do cliente (raiz). Desmarque para ocultá-lo do cliente.',
  '👁 Cliente': '👁 Cliente',
  '👤 Operador': '👤 Operador',
  'Cambiar el estado manualmente (no corre ni detiene el pipeline)':
    'Alterar o estado manualmente (não roda nem para o pipeline)',
  'Abrir esta ubicación en Google Maps (con nombres de calles)':
    'Abrir esta localização no Google Maps (com nomes de ruas)',
  '📍 Google Maps': '📍 Google Maps',
  'Pantalla completa': 'Tela cheia',
  'Actualizar y centrar en la zona marcada': 'Atualizar e centralizar na zona marcada',
  '🔄 Actualizar': '🔄 Atualizar',
  'Actividad': 'Atividade',
  'Abrí el detalle para cargar el historial…': 'Abra o detalhe para carregar o histórico…',
  '👥 Habitantes por manzana': '👥 Habitantes por quadra',
  'Estimación adicional por desagregación dasimétrica — menos exacta que el relevamiento':
    'Estimativa adicional por desagregação dasimétrica — menos exata que o levantamento',
  'Repartir la población censal entre las manzanas (técnica dasimétrica). Requiere censo + parcelas con uso/UF.':
    'Distribuir a população censitária entre as quadras (técnica dasimétrica). Requer censo + lotes com uso/UF.',
  '▶ Estimar habitantes': '▶ Estimar habitantes',
  'Sin estimación todavía. Tocá "Estimar habitantes".':
    'Ainda sem estimativa. Clique em "Estimar habitantes".',
  '↻ Actualizar': '↻ Atualizar',
  // El nombre del formato («CSV Operadora») lo pone el backend desde el perfil del
  // cliente y NO se traduce, igual que la taxonomía y las columnas DSC_/COD_.
  'Descargar el CSV con el formato de entrega propio del cliente':
    'Baixar o CSV com o formato de entrega do próprio cliente',
  'Descargar el relevamiento en DXF para AutoCAD: parcelas, unidades de vivienda y comercio, dirección y tipo de edificación, cada cosa en su capa. AutoCAD lo abre nativo y permite guardarlo como DWG.':
    'Baixar o levantamento em DXF para AutoCAD: lotes, unidades de moradia e comércio, endereço e tipo de edificação, cada coisa em sua camada. O AutoCAD abre nativamente e permite salvar como DWG.',
  '⬇ DXF (AutoCAD)': '⬇ DXF (AutoCAD)',
  'Traer footprints de edificios (Google Open Buildings, fallback OSM) para revisar visualmente contra lo que dice el catastro — capa aparte, no toca el relevamiento.':
    'Trazer pegadas de edifícios (Google Open Buildings, alternativa OSM) para conferir visualmente contra o que diz o cadastro — camada à parte, não altera o levantamento.',
  '🏗️ Footprints (revisión)': '🏗️ Pegadas (revisão)',
  'Altura satelital de cada parcela (Google Solar − terreno) contrastada con los pisos que sugiere el catastro. Marca dónde hay más construido de lo declarado. Free tier 10k/mes.':
    'Altura por satélite de cada lote (Google Solar − terreno) confrontada com os pavimentos que o cadastro sugere. Marca onde há mais construído do que o declarado. Cota gratuita de 10 mil/mês.',
  '📏 Altura (revisión)': '📏 Altura (revisão)',
  'Estimar el número de puerta de las parcelas que el catastro dejó sin altura, interpolando sobre el eje de la calle entre los linderos con número real. No pisa el número del catastro: se guarda aparte y se muestra marcado «≈ N est.».':
    'Estimar o número do imóvel dos lotes que o cadastro deixou sem numeração, interpolando sobre o eixo da rua entre os vizinhos com número real. Não sobrescreve o número do cadastro: fica salvo à parte e aparece marcado «≈ N est.».',
  '🔢 Nº estimado': '🔢 Nº estimado',
  'Buscar los casos que necesitan resolución humana (hoteles sin habitaciones, construcción no declarada, UF imposible) y abrir el reporte para resolverlos.':
    'Buscar os casos que precisam de resolução humana (hotéis sem quartos, construção não declarada, UF impossível) e abrir o relatório para resolvê-los.',
  '🧾 Incidencias': '🧾 Ocorrências',
  'Abrir el reporte de incidencias del relevamiento': 'Abrir o relatório de ocorrências do levantamento',
  'Buscar hoteles (Cadastur + Receita + OSM; Google si está tildado): habitaciones y abierto/cerrado; suma las habitaciones como UF comercio':
    'Buscar hotéis (Cadastur + Receita + OSM; Google se estiver marcado): quartos e aberto/fechado; soma os quartos como UF comércio',
  '🏨 Hoteles': '🏨 Hotéis',
  'Incluir Google Places, la fuente PAGA (~USD 0,032/req). Sin tilde corre solo las fuentes gratuitas (Cadastur + Receita + OSM).':
    'Incluir o Google Places, a fonte PAGA (~USD 0,032/req). Sem marcar, roda apenas as fontes gratuitas (Cadastur + Receita + OSM).',
  'Google (pago)': 'Google (pago)',
  'Completar con IA (Gemini + búsqueda web) las habitaciones de los hoteles abiertos sin dato. Pago por hotel.':
    'Completar com IA (Gemini + busca na web) os quartos dos hotéis abertos sem dado. Pago por hotel.',
  '🛏️ Habitaciones IA': '🛏️ Quartos IA',
  'Detectar barrios cerrados / condomínios en OSM y marcar las parcelas de adentro (capa 🏘 Country del mapa).':
    'Detectar condomínios fechados no OSM e marcar os lotes de dentro (camada 🏘 Condomínio do mapa).',
  '🏘 Country': '🏘 Condomínio',

  /* ── Progreso del pipeline y resultado de la importación ────────────────── */
  'Avance del relevamiento': 'Progresso do levantamento',
  '✓ Completo': '✓ Completo',
  '✗ Error': '✗ Erro',
  'Sin pipeline': 'Sem pipeline',
  '⚠ Quedó <b>parcial</b>: {pasos}. Hay que <b>reanudar</b> para completarlo (reanudar acumula, no repite lo hecho).':
    '⚠ Ficou <b>parcial</b>: {pasos}. É preciso <b>retomar</b> para concluir (retomar acumula, não repete o que já foi feito).',
  '📋 Resultado de la importación': '📋 Resultado da importação',
  '{n} importadas': '{n} importados',
  '{n} ubicadas': '{n} localizados',
  ' ({n} del caché)': ' ({n} do cache)',
  '{n} sin ubicar': '{n} sem localizar',
  '{n} ubicaciones distintas en el mapa': '{n} localizações distintas no mapa',
  ' — {n} direcciones comparten coordenada (repetidas por unidad o geocoding a nivel calle); se abren en abanico para verlas todas':
    ' — {n} endereços compartilham a mesma coordenada (repetidos por unidade ou geocodificação em nível de rua); abrem-se em leque para vê-los todos',

  /* ── Comparativa con el relevamiento anterior ───────────────────────────── */
  'Todavía no hay un relevamiento anterior para comparar.':
    'Ainda não há um levantamento anterior para comparar.',
  '— Elegí el relevamiento anterior —': '— Escolha o levantamento anterior —',
  'Comparar': 'Comparar',
  'Subir el CSV del relevamiento anterior del cliente':
    'Enviar o CSV do levantamento anterior do cliente',
  '⬆ Importar CSV anterior': '⬆ Importar CSV anterior',
  'Eliminar el baseline importado seleccionado en el selector':
    'Excluir a base importada selecionada no seletor',
  'Cruzando direcciones…': 'Cruzando endereços…',
  'Match aproximado de dirección': 'Correspondência aproximada de endereço',
  'Δ UF Vivienda': 'Δ UF Moradia',
  'Δ UF Comercio': 'Δ UF Comércio',
  'direcciones': 'endereços',
  'Nuevas': 'Novas',
  '{n} sin cambio': '{n} sem alteração',
  'Cambiaron / Desap.': 'Mudaram / Desap.',
  '👥 Δ habitantes estimado:': '👥 Δ habitantes estimado:',
  '(ΔUF vivienda × {n} hab/domicilio del censo — estimación secundaria)':
    '(ΔUF moradia × {n} hab/domicílio do censo — estimativa secundária)',
  'Contra «{nombre}»': 'Contra «{nombre}»',
  ' del {fecha}': ' de {fecha}',
  'match por {metodo}': 'correspondência por {metodo}',
  'dirección normalizada': 'endereço normalizado',
  'inscripción catastral + dirección': 'inscrição cadastral + endereço',
  ' · {n} matches aproximados': ' · {n} correspondências aproximadas',
  ' · {n} parcelas sin dirección (no comparables)': ' · {n} lotes sem endereço (não comparáveis)',
  'Descargar la comparativa completa (incluye las sin cambio)':
    'Baixar a comparação completa (inclui as sem alteração)',
  '⬇ CSV Comparativa': '⬇ CSV Comparação',
  'Volver a pintar el mapa por tipología': 'Voltar a pintar o mapa por tipologia',
  '✕ Quitar del mapa': '✕ Remover do mapa',
  'Estado': 'Estado',
  'Dirección': 'Endereço',
  'Uso': 'Uso',
  'UF Viv': 'UF Mor',
  'UF Com': 'UF Com',
  'Mostrando 300 — el CSV trae todas.': 'Mostrando 300 — o CSV traz todos.',
  'Sin diferencias: todas las direcciones están igual que antes.':
    'Sem diferenças: todos os endereços estão iguais aos de antes.',

  /* ── Importar relevamiento anterior (baseline) ──────────────────────────── */
  '⬆ Importar relevamiento anterior (CSV)': '⬆ Importar levantamento anterior (CSV)',
  'Subí el CSV que tiene el cliente. Si es Excel, primero guardalo como CSV (Archivo → Guardar como → CSV). El CSV debe traer las columnas con el nombre exacto (ya no se mapea a mano).':
    'Envie o CSV que o cliente tem. Se for Excel, primeiro salve como CSV (Arquivo → Salvar como → CSV). O CSV deve trazer as colunas com o nome exato (não se mapeia mais à mão).',
  'Archivo CSV': 'Arquivo CSV',
  'Nombre del baseline': 'Nome da base',
  'Fecha del relevamiento original': 'Data do levantamento original',
  'Importar {n} filas': 'Importar {n} linhas',

  /* ── Hoteles y comercios reclasificados ─────────────────────────────────── */
  'reclasificado a mano (no es hotel)': 'reclassificado à mão (não é hotel)',
  '{n} habitaciones': '{n} quartos',
  'habitaciones s/d': 'quartos s/inf.',
  'Obtenida por IA (Gemini + búsqueda web) — conviene verificar':
    'Obtido por IA (Gemini + busca na web) — convém verificar',
  '(IA)': '(IA)',
  'Estimado por área de construcción del catastro (BCI); el dato exacto lo da Cadastur':
    'Estimado pela área construída do cadastro (BCI); o dado exato vem do Cadastur',
  '(estimado)': '(estimado)',

  /* ── Comentarios del cliente y marcado para revisar ─────────────────────── */
  '💬 Comentarios ({n})': '💬 Comentários ({n})',
  'Dejá una sugerencia o corrección sobre esta dirección':
    'Deixe uma sugestão ou correção sobre este endereço',
  '💬 Comentar': '💬 Comentar',
  'Sugerencia o corrección sobre esta dirección…': 'Sugestão ou correção sobre este endereço…',
  'Abrir un caso en el panel de incidencias para corregir esto a mano':
    'Abrir um caso no painel de ocorrências para corrigir isto à mão',
  '📌 Marcar para revisar': '📌 Marcar para revisar',
  'Qué está mal (ej.: es un edificio, no un comercio)…':
    'O que está errado (ex.: é um edifício, não um comércio)…',

  /* ── Popups del mapa (parcela y relevamiento anterior) ──────────────────── */
  '✓ Cliente vigente (conectado)': '✓ Cliente ativo (conectado)',
  '≈ Ubicación aproximada (centro de la ciudad — la calle no se pudo ubicar en ninguna fuente)':
    '≈ Localização aproximada (centro da cidade — a rua não pôde ser localizada em nenhuma fonte)',
  'Dirección normalizada (forma canónica que se usa para matchear)':
    'Endereço normalizado (forma canônica usada para correspondência)',
  '(sin dirección)': '(sem endereço)',
  'Relevamiento anterior': 'Levantamento anterior',
  'sin uso': 'sem uso',
  'UF': 'UF',
  '(viv {v} / com {c})': '(mor {v} / com {c})',
  '· UF {n}': '· UF {n}',
  '{n} direcciones en este punto': '{n} endereços neste ponto',
  'Relevamiento anterior · UF total': 'Levantamento anterior · UF total',
  'Tipo de edificación (taxonomía del cliente)': 'Tipo de edificação (taxonomia do cliente)',
  'Categoría/descripción derivada de los establecimientos (CNPJ Receita) que caen en la parcela':
    'Categoria/descrição derivada dos estabelecimentos (CNPJ Receita) que caem no lote',
  'Residencial': 'Residencial',
  'Comercial': 'Comercial',
  'Especial': 'Especial',
  'm² terreno': 'm² de terreno',
  'm² construida': 'm² construídos',
  '≈ {n} piso(s)': '≈ {n} pavimento(s)',
  'Insc:': 'Insc.:',
  '{n} comercio(s) (Google)': '{n} comércio(s) (Google)',
  'valor venal': 'valor venal',
  'alíq.': 'alíq.',
  'Año constr.:': 'Ano constr.:',
  'Co-resp.:': 'Corresp.:',
  'Parte de establecimiento': 'Parte de estabelecimento',
  '{n} parcelas = 1 establecimiento (UF contada una vez)':
    '{n} lotes = 1 estabelecimento (UF contada uma só vez)',
  'Relev. anterior': 'Lev. anterior',
  'nuevo': 'novo',
  '{a} dir → {b} parcelas · en esta zona': '{a} end. → {b} lotes · nesta zona',

  /* ── Listado, actividad y sub-zona ──────────────────────────────────────── */
  'Sin actividad registrada todavía.': 'Ainda sem atividade registrada.',
  'Sin actividad registrada para este relevamiento.':
    'Sem atividade registrada para este levantamento.',
  'No hay relevamientos aún': 'Ainda não há levantamentos',
  'Creá el primero con el botón de arriba.': 'Crie o primeiro com o botão acima.',
  'Todavía no hay relevamientos para mostrar.': 'Ainda não há levantamentos para mostrar.',
  'No hay relevamientos activos': 'Não há levantamentos ativos',
  '📦 Archivados ({n})': '📦 Arquivados ({n})',
  'Error cargando datos': 'Erro ao carregar os dados',
  '🔁 Relevar sub-zona': '🔁 Levantar sub-zona',
  'Subí el polígono (GeoJSON) de la parte que querés re-relevar. Se crea un relevamiento nuevo acotado a esa sub-zona — este queda intacto y después podés compararlos. Los PDFs ya descargados se reutilizan.':
    'Envie o polígono (GeoJSON) da parte que quer levantar de novo. Cria-se um levantamento novo restrito a essa sub-zona — este fica intacto e depois você pode compará-los. Os PDFs já baixados são reaproveitados.',
  'Polígono GeoJSON de la sub-zona': 'Polígono GeoJSON da sub-zona',

  /* ── Login (app.py, _login_page) ────────────────────────────────────────── */
  'AI Mapping — Acceso': 'AI Mapping — Acesso',
  'Contraseña incorrecta o sin permiso.': 'Senha incorreta ou sem permissão.',
  'Relevamiento inteligente': 'Levantamento inteligente',
  'Contraseña': 'Senha',
  'Ingresar': 'Entrar',
  'Plataforma de relevamiento geoespacial': 'Plataforma de levantamento geoespacial',

  /* ── Resumen de avance de cada paso del pipeline (stepSummary) ──────────── */
  '{n} de ~{total} lotes hallados': '{n} de ~{total} lotes encontrados',
  '{n} PDF ({nuevos} nuevos, {reusados} reusados)': '{n} PDF ({nuevos} novos, {reusados} reaproveitados)',
  ' · {n} pendientes': ' · {n} pendentes',
  ' · {n} fallidos': ' · {n} com falha',
  '{n} de {total} parseadas': '{n} de {total} analisados',
  ' · {n} con error': ' · {n} com erro',
  '{e} establecimientos · {p} parcelas': '{e} estabelecimentos · {p} lotes',
  '{n} encontrados': '{n} encontrados',
  '{n} categorizadas': '{n} categorizados',
  '{n} hoteles': '{n} hotéis',
  ' · {n} sin habitaciones': ' · {n} sem quartos',
  '{n} completadas por IA': '{n} completados por IA',
  ' · {n} sin resultado': ' · {n} sem resultado',
  '{n} huellas': '{n} pegadas',
  ' · {n} en parcelas': ' · {n} em lotes',
  '{n} medidas': '{n} medidos',
  ' · {n} discrepancias vs catastro': ' · {n} discrepâncias vs cadastro',

  /* ── Habitantes por manzana (dasimétrico) ───────────────────────────────── */
  '≈ {hab} habitantes en {mz} manzanas': '≈ {hab} habitantes em {mz} quadras',
  'Estimación dasimétrica al {fecha} · valores aproximados (banda ±30%)':
    'Estimativa dasimétrica em {fecha} · valores aproximados (faixa ±30%)',
  'Manzana': 'Quadra',
  'Habitantes': 'Habitantes',
  'Rango': 'Faixa',
  'UF Viv.': 'UF Mor.',
  'UF Com.': 'UF Com.',

  /* ── Cambio de idioma ───────────────────────────────────────────────────── */
  'Cambiar el idioma de la interfaz (español / portugués)':
    'Alterar o idioma da interface (espanhol / português)',
  'Cambiar idioma': 'Alterar idioma',
  'Cambiar el idioma recarga la página y se pierde lo que cargaste en el formulario. ¿Seguir igual?':
    'Alterar o idioma recarrega a página e você perde o que preencheu no formulário. Continuar mesmo assim?',
};
