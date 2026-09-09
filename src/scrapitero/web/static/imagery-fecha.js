/* Cartel con la FECHA de la imagen satelital que se está viendo, para cualquier mapa Leaflet
   de la app.
 *
 * Por qué existe: el operador decide mirando la foto —confirma una construcción, mueve un
 * punto, dibuja una zona— y hasta ahora no había forma de saber de cuándo era esa foto. La
 * contradicción medida el 6-sep-2026: `AlturaFetcher` mide sobre la imagery de Google Solar,
 * que es de **2014** en el 87% de Várzea Grande y **2013** en Hurlingham, mientras el basemap
 * de Esri que todos los mapas muestran por default trae **sep-2025** sobre VG y **ene-2026**
 * sobre el conurbano. La pantalla mostraba obra que la medición no podía ver.
 *
 * Vive en un archivo propio y no copiado en cada página porque hay TRES mapas satelitales
 * (el del relevamiento, el del asistente de actualización y el de incidencias) y una copia
 * por página es una copia que se actualiza en dos lugares y se olvida en el tercero.
 *
 * ⚠ El cartel está atado a la CAPA, no a la zona. De Google **no sabemos la fecha** (no la
 * publica por API) y lo dice; dejar la fecha de Esri fija mientras alguien mira el satélite
 * de Google sería peor que no poner nada, porque le da precisión falsa a la decisión.
 *
 * Por el mismo motivo vive acá `capasBase()`: las tres definiciones de las capas satelitales
 * estaban copiadas en los tres mapas, con sus `maxZoom`/`maxNativeZoom` a mano — y eso es
 * exactamente lo que hizo falta corregir en los tres a la vez (ver `MAX_NATIVE_ESRI`).
 */
(function (global) {
  'use strict';

  var MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun',
               'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

  // Hasta dónde deja acercar el mapa. Más allá del tile nativo Leaflet hace zoom digital
  // (agranda el último tile real), que es lo que permite mirar un techo de cerca.
  var MAX_ZOOM = 22;
  // ⚠ Último nivel en el que Esri tiene imagen DE VERDAD en las zonas que relevamos.
  // Estaba en 20 y por eso "desaparecía la imagen al hacer zoom" (reportado el 9-sep-2026):
  // pasando de z19 el mapa quedaba en blanco con los marcadores flotando. No es un fallo de
  // red y por eso no saltaba en la consola — Esri responde **HTTP 200 con un tile gris que
  // dice «Map data not yet available»**, así que Leaflet lo pinta como si fuera imagen.
  // Medido sobre el tile del centro de cada zona (desviación estándar de sus píxeles, que en
  // una foto aérea es alta y en el relleno gris es 5,4 idéntica en todas):
  //     Várzea Grande  z18 40,3 · z19 46,5 · z20 5,4 · z21 5,4
  //     Malvinas Arg.  z18 70,2 · z19 77,2 · z20 5,4 · z21 5,4
  //     Hurlingham     z18 43,6 · z19 49,4 · z20 5,4 · z21 5,4
  // Con 19 la imagen se ve siempre, borrosa al final del zoom pero presente, que es lo que el
  // operador necesita para confirmar una construcción. Si algún día se releva una ciudad con
  // cobertura más fina, esto es lo que hay que subir — y conviene medirlo igual, no suponerlo.
  var MAX_NATIVE_ESRI = 19;
  var MAX_NATIVE_OSM = 19;   // el último nivel que sirve openstreetmap.org

  var URL_ESRI_SAT = 'https://server.arcgisonline.com/ArcGIS/rest/services/'
                   + 'World_Imagery/MapServer/tile/{z}/{y}/{x}';
  var URL_ESRI_REF = 'https://server.arcgisonline.com/ArcGIS/rest/services/'
                   + 'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}';
  var URL_OSM = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';

  /** Las capas base que comparten los tres mapas de la app, ya configuradas. */
  function capasBase() {
    return {
      satelite: L.tileLayer(URL_ESRI_SAT, {
        attribution: 'Tiles © Esri', maxZoom: MAX_ZOOM, maxNativeZoom: MAX_NATIVE_ESRI }),
      etiquetas: L.tileLayer(URL_ESRI_REF, {
        maxZoom: MAX_ZOOM, maxNativeZoom: MAX_NATIVE_ESRI, opacity: 0.8 }),
      osm: L.tileLayer(URL_OSM, {
        attribution: '© OpenStreetMap', maxZoom: MAX_ZOOM, maxNativeZoom: MAX_NATIVE_OSM }),
    };
  }

  // Caché por página: la cobertura de Esri viene en bloques de varios km, así que dos mapas
  // de la misma ciudad comparten respuesta y no se le pega dos veces al servicio.
  var cache = {};

  function clave(lat, lng) {
    return Math.round(lat / 0.05) + ':' + Math.round(lng / 0.05);
  }

  /** Fecha de captura del basemap de Esri en ese punto. `null` si no se pudo. */
  function fechaDeImagen(lat, lng) {
    var k = clave(lat, lng);
    if (cache[k] !== undefined) return Promise.resolve(cache[k]);
    return fetch('/api/imagery-fecha?lat=' + lat + '&lng=' + lng)
      .then(function (r) { return r.json(); })
      .then(function (d) { cache[k] = (d && d.ok) ? d.imagery : null; return cache[k]; })
      .catch(function () { cache[k] = null; return null; });
  }

  function mesAnio(iso) {
    var m = /^(\d{4})-(\d{2})-/.exec(iso || '');
    return m ? MESES[+m[2] - 1] + '-' + m[1] : (iso || '');
  }

  // ── Capas de Google (opcionales) ───────────────────────────────────────────
  // Google es la capa ACTIVA por default desde el 9-sep-2026, por pedido del operador: su
  // imagen del conurbano y de Várzea Grande es más nueva y más nítida que la de Esri, que en
  // z20 ni siquiera existe (ver `MAX_NATIVE_ESRI`). Esri queda como opción del selector, y
  // es además el **fallback**: si no hay API key o el SDK no carga, el mapa se queda con la
  // capa de Esri que ya estaba puesta, sin quedarse sin fondo.
  //
  // ⚠ Dos cosas que se pagan por tener Google de default, dichas acá porque no se ven en el
  // código: (1) **el cartel de fecha deja de informar** — Google no publica la fecha de
  // captura por API, así que dice «fecha no publicada», y el pedido del cliente del
  // 6-sep-2026 era mostrar SIEMPRE de cuándo es la imagen; quien necesite saberlo tiene que
  // cambiar a Esri. (2) **se factura por carga de mapa**: antes el SDK sólo instanciaba un
  // mapa de Google si alguien elegía esa capa; ahora lo hace al abrir cualquier relevamiento.
  var NOMBRE_GOOGLE_SAT = '🛰 Google (satélite)';
  var NOMBRE_GOOGLE_MAPA = '🗺 Google (mapa)';
  var _cfg = null, _sdk = null;

  function _config() {
    if (!_cfg) _cfg = fetch('/api/config').then(function (r) { return r.json(); })
                       .catch(function () { return {}; });
    return _cfg;
  }

  function _script(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = src; s.async = true; s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  }

  function _cargarSdk(key) {
    if (!_sdk) {
      _sdk = _script('https://maps.googleapis.com/maps/api/js?key=' + key + '&v=weekly')
        .then(function () {
          return _script('https://unpkg.com/leaflet.gridlayer.googlemutant@0.14.0/'
                       + 'dist/Leaflet.GoogleMutant.js');
        });
    }
    return _sdk;
  }

  /**
   * Suma Google satélite/mapa al control de capas y lo deja activo.
   * `opts.esri` y `opts.etiquetas` son las capas a apagar al activarlo (el híbrido de Google
   * ya trae sus propios rótulos: dejar encima los de Esri duplica cada nombre de calle).
   * `opts.activar: false` las agrega sólo como opción, sin cambiar la capa en uso.
   */
  function agregarGoogle(map, layersCtl, opciones) {
    var opts = opciones || {};
    var esri = opts.esri, etiquetas = opts.etiquetas;
    // Las etiquetas de Esri acompañan SÓLO a su satélite: se van con cualquier otra capa y
    // vuelven al elegirlo de nuevo, para que el selector se comporte igual en los dos casos.
    if (etiquetas) {
      map.on('baselayerchange', function (e) {
        // ⚠ Fuera del ciclo del click: mientras el control de capas procesa el suyo tiene
        // `_handlingClick` en true y se saltea el refresco de sus casillas, así que un cambio
        // hecho acá dentro deja la capa puesta pero el tilde de «Etiquetas» sin marcar —
        // el control diciendo lo contrario de lo que el mapa muestra.
        setTimeout(function () {
          if (esri && e.layer === esri) {
            if (!map.hasLayer(etiquetas)) etiquetas.addTo(map);
          } else if (map.hasLayer(etiquetas)) {
            map.removeLayer(etiquetas);
          }
        }, 0);
      });
    }
    return _config().then(function (cfg) {
      if (!cfg || !cfg.google_maps_key) return null;   // sin key → se queda con Esri/OSM
      return _cargarSdk(cfg.google_maps_key).then(function () {
        if (!(L.gridLayer && L.gridLayer.googleMutant)) return null;
        var hybrid = L.gridLayer.googleMutant({ type: 'hybrid', maxZoom: MAX_ZOOM });
        var road = L.gridLayer.googleMutant({ type: 'roadmap', maxZoom: MAX_ZOOM });
        layersCtl.addBaseLayer(hybrid, NOMBRE_GOOGLE_SAT);
        layersCtl.addBaseLayer(road, NOMBRE_GOOGLE_MAPA);
        if (opts.activar !== false) {
          if (esri && map.hasLayer(esri)) map.removeLayer(esri);
          hybrid.addTo(map);
          // Leaflet sólo emite `baselayerchange` cuando el cambio sale del control; sin este
          // aviso el cartel seguiría anunciando la fecha de Esri sobre la imagen de Google.
          map.fire('baselayerchange', { layer: hybrid, name: NOMBRE_GOOGLE_SAT });
        }
        return { hybrid: hybrid, road: road };
      });
    }).catch(function (e) {
      console.warn('Google Maps no disponible (se mantiene Esri/OSM):', e);
      return null;
    });
  }

  /** Agrega el cartel a un mapa. Devuelve el control, con `.refrescar(lat, lng)`. */
  function agregarCartel(map, opciones) {
    var opts = opciones || {};
    var dato = null;
    var nombreCapa = opts.capaInicial || '🛰 Satélite';

    var Ctl = L.Control.extend({
      options: { position: opts.position || 'bottomleft' },
      onAdd: function () {
        this._d = L.DomUtil.create('div', 'imgfecha');
        L.DomEvent.disableClickPropagation(this._d);
        this._pintar();
        return this._d;
      },
      _pintar: function () {
        var html;
        if (/Google/i.test(nombreCapa)) {
          html = '<b>Google</b> · <span class="dud">fecha no publicada</span>';
        } else if (/Mapa|OSM/i.test(nombreCapa)) {
          html = '<b>Mapa</b> · sin imagen satelital';
        } else if (dato && dato.fecha) {
          html = '📅 <b>' + mesAnio(dato.fecha) + '</b> · Esri'
               + (dato.proveedor ? '/' + dato.proveedor : '')
               + (dato.resolucion_m ? ' · ' + String(dato.resolucion_m).replace('.', ',') + ' m' : '');
        } else {
          html = '🛰 Esri · <span class="dud">fecha desconocida</span>';
        }
        if (this._d) this._d.innerHTML = html;
      },
      /** Consulta la fecha para ese punto y re-pinta. Llamar cuando el mapa ya tiene datos. */
      refrescar: function (lat, lng) {
        var self = this;
        if (lat === undefined || lat === null) { self._pintar(); return; }
        fechaDeImagen(lat, lng).then(function (d) { dato = d; self._pintar(); });
      },
    });

    var ctl = new Ctl().addTo(map);
    // El nombre de la capa activa lo da Leaflet; sin esto el cartel mentiría al cambiar.
    map.on('baselayerchange', function (e) { nombreCapa = e.name || ''; ctl._pintar(); });
    return ctl;
  }

  global.ImageryFecha = { agregarCartel: agregarCartel, fechaDeImagen: fechaDeImagen,
                          mesAnio: mesAnio, capasBase: capasBase, MAX_ZOOM: MAX_ZOOM,
                          agregarGoogle: agregarGoogle };
})(window);
