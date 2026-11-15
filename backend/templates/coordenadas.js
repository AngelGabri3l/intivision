/* Coordenadas (H8, fase D): lee lo que una persona pega y pide la ubicación al GPS. Nunca visita una dirección: solo lee el texto.
   Se incluye dentro del script de cada pantalla que lo necesita (mapa del administrador y ficha de la cámara). */
(function (raiz) {
  "use strict";

  var NUM = "-?\\d{1,3}(?:\\.\\d+)?";
  var CORTO = /(?:^|\/\/)(?:maps\.app\.goo\.gl|goo\.gl\/maps|g\.co\/kgs)(?:[\/?#]|$)/i;

  function enRango(lat, lng) {
    return isFinite(lat) && isFinite(lng) && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180;
  }

  function resultado(lat, lng) {
    if (!enRango(lat, lng)) { return { error: "Esas coordenadas no existen: la latitud va de −90 a 90 y la longitud de −180 a 180." }; }
    if (lat === 0 && lng === 0) { return { error: "Las coordenadas 0, 0 no son una ubicación real." }; }
    return { lat: lat, lng: lng };
  }

  function par(a, b) { return resultado(parseFloat(a), parseFloat(b)); }

  // «12.046374 S» → −12.046374 (S y O/W son negativas)
  function conLetra(token) {
    var m = /^(\d{1,3}(?:\.\d+)?)\s*°?\s*([NSEWO])$/i.exec(token);
    if (!m) { return null; }
    var v = parseFloat(m[1]), l = m[2].toUpperCase();
    return { valor: (l === "S" || l === "W" || l === "O") ? -v : v, eje: (l === "N" || l === "S") ? "lat" : "lng" };
  }

  function desdeUrl(texto) {
    var m = /^https?:\/\/([^\/?#\s]+)/i.exec(texto);
    if (!m || !/^(?:[a-z0-9-]+\.)*google\.(?:com(?:\.[a-z]{2})?|[a-z]{2})(?::\d+)?$/i.test(m[1])) { return { error: "Ese enlace no parece de Google Maps. Pega las coordenadas o un enlace de google.com/maps." }; }
    var t = texto;
    try { t = decodeURIComponent(texto); } catch (e) { /* se queda con el texto tal cual */ }
    var r;
    // 1) el punto del lugar (!3d<lat>!4d<lng>): es el pin, no el centro de la pantalla
    if ((r = new RegExp("!3d(" + NUM + ")!4d(" + NUM + ")").exec(t))) { return par(r[1], r[2]); }
    // 2) parámetros de búsqueda o de ruta: q, ll, query, destination, center
    if ((r = new RegExp("[?&](?:q|ll|query|destination|center)=(?:loc:)?\\s*(" + NUM + ")\\s*,\\s*(" + NUM + ")(?=[&#]|$)").exec(t))) { return par(r[1], r[2]); }
    // 3) un pin soltado: /maps/place/-12.04,-77.04
    if ((r = new RegExp("/maps/place/(" + NUM + ")\\s*,\\s*(" + NUM + ")(?=[/@?#]|$)").exec(t))) { return par(r[1], r[2]); }
    // 4) el centro de la vista: @lat,lng,zoom (último recurso: puede no ser el pin)
    if ((r = new RegExp("/@(" + NUM + "),(" + NUM + ")(?=[,/?#]|$)").exec(t))) { return par(r[1], r[2]); }
    return { error: "No encontré coordenadas en ese enlace. En Google Maps, mantén presionado el punto y copia los números que aparecen." };
  }

  function parsear(entrada) {
    var t = String(entrada === null || entrada === undefined ? "" : entrada).replace(/[\u2212\u2012\u2013\u2014]/g, "-").trim();
    if (!t) { return { error: "Pega unas coordenadas o un enlace de Google Maps." }; }
    if (t.length > 2000) { return { error: "El texto es demasiado largo." }; }
    if (CORTO.test(t)) {
      return { error: "Ese es un enlace corto y no se puede leer. Ábrelo en Google Maps, mantén presionado el punto y copia los números que aparecen (por ejemplo −12.046374, −77.042793)." };
    }
    if (/^https?:\/\//i.test(t)) { return desdeUrl(t); }
    if (/°\s*\d+\s*['′]/.test(t) || /\d\s*['′]\s*\d/.test(t)) { return { error: "Usa coordenadas decimales (por ejemplo −12.046374, −77.042793), no grados, minutos y segundos." }; }
    t = t.replace(/[()\[\]]/g, " ").replace(/°/g, " ").replace(/\s+/g, " ").trim();
    if (/^-?\d+,\d+\s+-?\d+,\d+$/.test(t)) { return { error: "Usa punto como separador decimal (−12.046374) y una coma entre la latitud y la longitud." }; }
    var piezas = t.indexOf(";") >= 0 ? t.split(/\s*;\s*/) : (t.indexOf(",") >= 0 ? t.split(/\s*,\s*/) : t.split(" "));
    piezas = piezas.filter(function (p) { return p.length; });
    // «12.046374 S 77.042793 O» sin coma: cuatro piezas que son dos números con su letra
    if (piezas.length === 4 && conLetra(piezas[0] + piezas[1]) && conLetra(piezas[2] + piezas[3])) { piezas = [piezas[0] + piezas[1], piezas[2] + piezas[3]]; }
    if (piezas.length !== 2) { return { error: "No pude leer dos números. Escribe la latitud y la longitud, por ejemplo −12.046374, −77.042793." }; }
    var a = conLetra(piezas[0]), b = conLetra(piezas[1]);
    if (a || b) {
      if (!a || !b || a.eje === b.eje) { return { error: "Si usas letras N, S, E u O, pon una para la latitud y otra para la longitud (por ejemplo 12.046374 S, 77.042793 O)." }; }
      return a.eje === "lat" ? resultado(a.valor, b.valor) : resultado(b.valor, a.valor);
    }
    var num = new RegExp("^" + NUM + "$");
    if (!num.test(piezas[0]) || !num.test(piezas[1])) { return { error: "No pude leer dos números. Escribe la latitud y la longitud, por ejemplo −12.046374, −77.042793." }; }
    return par(piezas[0], piezas[1]);
  }

  // La ubicación del teléfono: { lat, lng, precision } o { error }. Pide permiso al navegador; la política del sitio solo lo permite en nuestras páginas.
  function miUbicacion(devolver) {
    var nav = raiz.navigator;
    if (!nav || !nav.geolocation) { devolver({ error: "Tu navegador no permite obtener la ubicación. Usa el mapa o pega las coordenadas." }); return; }
    nav.geolocation.getCurrentPosition(function (pos) {
      var c = pos.coords;
      var r = resultado(c.latitude, c.longitude);
      if (r.error) { devolver({ error: "El teléfono no encontró señal de GPS todavía. Inténtalo de nuevo al aire libre." }); return; }
      r.precision = typeof c.accuracy === "number" && isFinite(c.accuracy) ? Math.round(c.accuracy) : null;
      devolver(r);
    }, function (e) {
      var mensajes = { 1: "No diste permiso para ver tu ubicación. Actívalo en el navegador o usa el mapa.",
        2: "No se pudo obtener la ubicación (¿sin señal de GPS?). Usa el mapa o pega las coordenadas.",
        3: "Tardó demasiado en encontrar tu ubicación. Inténtalo de nuevo al aire libre." };
      devolver({ error: mensajes[e && e.code] || "No se pudo obtener la ubicación." });
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  }

  raiz.VigiaCoordenadas = { parsear: parsear, miUbicacion: miUbicacion };
})(window);
