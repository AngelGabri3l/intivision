/* Mapa de calor por cámara (H8, fase D). Pide /admin/mapa/datos y dibuja con Leaflet (que se sirve desde este mismo sitio).
   Escribe SIEMPRE con textContent: ningún nombre de cámara ni lugar se interpreta como HTML. Las coordenadas que se muestran o se
   mandan nunca salen de un texto sin validar: el servidor las vuelve a validar. */
(function () {
  "use strict";

  var ATRIBUCION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">colaboradores de OpenStreetMap</a>';
  var NARANJA = "#ff6a00", GRIS = "#8a8a8a";

  var pedido = null, secuencia = 0;
  var datos = null;                // los últimos datos recibidos
  var mapa = null, capa = null;    // el mapa de Leaflet y la capa donde se dibujan las cámaras
  var primera = true;              // el primer dibujo ajusta el encuadre a las cámaras; los siguientes respetan lo que el administrador movió
  var armada = null;               // la cámara que se está ubicando: { id, name }
  var ocupado = false;             // mientras se guarda una ubicación no se acepta otra
  var pendiente = null;            // la ubicación que quedó lejos y espera confirmación: { lat, lng, precision }

  function porId(id) { return document.getElementById(id); }
  function nodo(tag, clase, texto) {
    var e = document.createElement(tag);
    if (clase) { e.className = clase; }
    if (texto !== undefined && texto !== null) { e.textContent = String(texto); }
    return e;
  }
  function vaciar(e) { while (e.firstChild) { e.removeChild(e.firstChild); } }
  function plural(n, uno, varios) { return n + " " + (n === 1 ? uno : varios); }
  function fechaLarga(iso) { var p = String(iso).split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function fechaHora(iso) { return fechaLarga(String(iso).slice(0, 10)) + " " + String(iso).slice(11, 16); }

  function estado(texto, error) {
    var e = porId("mapa-estado");
    e.textContent = texto || "";
    e.className = "mapa__estado" + (error ? " mapa__estado--error" : "");
  }
  function estadoUbicar(texto, error) {
    var e = porId("mapa-ubicar-estado");
    e.textContent = texto || "";
    e.className = "mapa__estado" + (error ? " mapa__estado--error" : "");
  }

  function respuesta(r) { return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; }); }

  function enviar(url, cuerpo) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(cuerpo || {}) }).then(respuesta);
  }

  // ------------------------------------------------------------------ el mapa

  function iniciarMapa(d) {
    if (mapa) { return true; }
    var L = window.L;
    if (!L) { estado("No se pudo cargar el mapa. Recarga la página.", true); return false; }
    mapa = L.map(porId("mapa-lienzo"), { center: [d.centro.lat, d.centro.lng], zoom: d.centro.definido ? 16 : 12 });
    if (d.tesela.disponible) { L.tileLayer(d.tesela.url, { maxZoom: 19, attribution: ATRIBUCION }).addTo(mapa); }
    capa = L.layerGroup().addTo(mapa);
    mapa.on("click", function (ev) { if (armada) { colocar(ev.latlng.lat, ev.latlng.lng, null, false); } });
    return true;
  }

  function enlacePanel(c) {
    var r = datos.rango;
    return "/admin?desde=" + encodeURIComponent(r.desde) + "&hasta=" + encodeURIComponent(r.hasta) + "&camara=" + encodeURIComponent(c.id);
  }

  function contenido(c) {
    var cont = nodo("div", "mapa-popup");
    cont.appendChild(nodo("strong", "", c.name));
    if (c.place) { cont.appendChild(nodo("p", "", c.place)); }
    cont.appendChild(nodo("p", "mapa-popup__tipo", c.kind === "common" ? "Zona común" : "Privada · el dueño permitió mostrarla"));
    cont.appendChild(nodo("p", "", plural(c.incidentes, "incidente", "incidentes") + " · " + plural(c.falsas, "falsa alarma", "falsas alarmas")));
    cont.appendChild(nodo("p", "", c.ultimo ? "Último evento: " + fechaHora(c.ultimo) : "Sin eventos en el rango"));
    var enlace = nodo("a", "", "Ver este rango en el panel");
    enlace.setAttribute("href", enlacePanel(c));
    cont.appendChild(enlace);
    if (c.kind === "common") {
      var fila = nodo("div", "mapa__botones");
      var mover = nodo("button", "btn btn--texto", "Mover"); mover.type = "button";
      mover.addEventListener("click", function () { armar({ id: c.id, name: c.name }); });
      var quitar = nodo("button", "btn btn--texto", "Quitar ubicación"); quitar.type = "button";
      var seguro = false;
      quitar.addEventListener("click", function () {
        if (!seguro) { seguro = true; quitar.textContent = "Confirmar: quitar ubicación"; return; }
        quitarUbicacion({ id: c.id, name: c.name });
      });
      fila.appendChild(mover); fila.appendChild(quitar); cont.appendChild(fila);
    }
    return cont;
  }

  function dibujar(d) {
    if (!mapa) { return; }
    var L = window.L;
    capa.clearLayers();
    var puntos = [];
    d.camaras.forEach(function (c) {
      var tam = Math.round(26 + 34 * Math.sqrt(d.maximo === 0 ? 0 : c.valor / d.maximo));
      var ll = [c.lat, c.lng];
      puntos.push(ll);
      L.circleMarker(ll, { radius: Math.round(tam * 0.9) + 10, stroke: false, fillColor: d.modo === "falsas" ? GRIS : NARANJA, fillOpacity: 0.18, interactive: false }).addTo(capa);
      var punto = nodo("div", "mapa-punto" + (d.modo === "falsas" ? " mapa-punto--falsas" : ""), c.valor);
      var m = L.marker(ll, { icon: L.divIcon({ html: punto, className: "mapa-punto-caja", iconSize: [tam, tam], iconAnchor: [tam / 2, tam / 2] }),
        title: c.name + ": " + c.valor, keyboard: true, riseOnHover: true }).addTo(capa);
      m.bindPopup(contenido(c), { minWidth: 220 });
    });
    if (primera) {
      primera = false;
      if (puntos.length) { mapa.fitBounds(L.latLngBounds(puntos).pad(0.3), { maxZoom: 18 }); }
    }
  }

  // ------------------------------------------------------------------ cámaras sin ubicar y ubicar una cámara

  function dibujarSinUbicar(lista) {
    var ul = porId("mapa-sin-ubicar");
    vaciar(ul);
    if (!lista.length) { ul.appendChild(nodo("li", "", "No hay cámaras de zona común sin ubicar.")); return; }
    lista.forEach(function (c) {
      var li = nodo("li");
      var texto = nodo("span", "", c.name);
      if (c.place) { texto.appendChild(nodo("small", "", c.place)); }
      var b = nodo("button", "btn btn--texto", "Ubicar"); b.type = "button";
      b.setAttribute("aria-label", "Ubicar la cámara " + c.name);
      b.addEventListener("click", function () { armar({ id: c.id, name: c.name }); });
      li.appendChild(texto); li.appendChild(b); ul.appendChild(li);
    });
  }

  function armar(camara) {
    armada = camara; pendiente = null;
    porId("mapa-ubicar").hidden = false;
    porId("mapa-lejos").hidden = true;
    porId("mapa-ubicando").textContent = "Ubicando: " + camara.name;
    estadoUbicar("");
    porId("mapa-pegar").value = "";
    porId("mapa-lienzo").classList.add("mapa--ubicando");
    if (mapa) { mapa.closePopup(); }
  }

  function desarmar() {
    armada = null; pendiente = null;
    porId("mapa-ubicar").hidden = true;
    porId("mapa-lienzo").classList.remove("mapa--ubicando");
  }

  function colocar(lat, lng, precision, confirmar) {
    if (!armada || ocupado) { return; }
    var cam = armada;
    ocupado = true;
    estadoUbicar("Guardando la ubicación…");
    enviar("/admin/camaras/" + encodeURIComponent(cam.id) + "/ubicacion", { lat: lat, lng: lng, precision_m: precision, confirmar_lejos: !!confirmar })
      .then(function (x) {
        ocupado = false;
        if (x.r.status === 401) { window.location.href = "/login"; return; }
        if (x.r.ok) {
          desarmar();
          estado("Ubicación guardada para " + cam.name + ".");
          cargar();
        } else if (x.d && x.d.code === "muy_lejos") {
          pendiente = { lat: lat, lng: lng, precision: precision };
          porId("mapa-lejos-texto").textContent = x.d.error;
          porId("mapa-lejos").hidden = false;
          estadoUbicar("");
        } else {
          estadoUbicar(x.r.status === 403 ? "Solo un administrador puede ubicar cámaras." : (x.d && x.d.error ? x.d.error : "No se pudo guardar la ubicación."), true);
        }
      })
      .catch(function () { ocupado = false; estadoUbicar("No se pudo conectar con el servidor. Inténtalo de nuevo.", true); });
  }

  function quitarUbicacion(cam) {
    enviar("/admin/camaras/" + encodeURIComponent(cam.id) + "/ubicacion/quitar", {})
      .then(function (x) {
        if (x.r.status === 401) { window.location.href = "/login"; return; }
        if (x.r.ok) { estado("Se quitó la ubicación de " + cam.name + "."); if (mapa) { mapa.closePopup(); } cargar(); }
        else { estado(x.d && x.d.error ? x.d.error : "No se pudo quitar la ubicación.", true); }
      })
      .catch(function () { estado("No se pudo conectar con el servidor. Inténtalo de nuevo.", true); });
  }

  porId("mapa-cancelar").addEventListener("click", desarmar);
  porId("mapa-gps").addEventListener("click", function () {
    if (!armada) { return; }
    estadoUbicar("Buscando tu ubicación…");
    window.VigiaCoordenadas.miUbicacion(function (r) {
      if (r.error) { estadoUbicar(r.error, true); return; }
      colocar(r.lat, r.lng, r.precision, false);
    });
  });
  porId("mapa-usar").addEventListener("click", function () {
    if (!armada) { return; }
    var r = window.VigiaCoordenadas.parsear(porId("mapa-pegar").value);
    if (r.error) { estadoUbicar(r.error, true); return; }
    colocar(r.lat, r.lng, null, false);
  });
  porId("mapa-lejos-si").addEventListener("click", function () { if (pendiente) { colocar(pendiente.lat, pendiente.lng, pendiente.precision, true); } });
  porId("mapa-lejos-no").addEventListener("click", function () { pendiente = null; porId("mapa-lejos").hidden = true; estadoUbicar("Elige otro punto."); });

  // ------------------------------------------------------------------ carga y filtros

  function render(d) {
    datos = d;
    porId("m-desde").value = d.rango.desde;
    porId("m-hasta").value = d.rango.hasta;
    porId("m-modo-incidentes").checked = d.modo === "incidentes";
    porId("m-modo-falsas").checked = d.modo === "falsas";
    var total = 0;
    d.camaras.forEach(function (c) { total += c.valor; });
    porId("mapa-resumen").textContent = fechaLarga(d.rango.desde) + " a " + fechaLarga(d.rango.hasta) + " · " + plural(d.camaras.length, "cámara", "cámaras") + " en el mapa · " +
      plural(total, d.modo === "falsas" ? "falsa alarma" : "incidente", d.modo === "falsas" ? "falsas alarmas" : "incidentes");
    porId("mapa-sin-config").hidden = !!d.tesela.disponible;
    dibujarSinUbicar(d.sin_ubicar);
    if (!iniciarMapa(d)) { return; }                  // Leaflet no cargó: el aviso de error se queda y no se pisa con «Actualizado»
    dibujar(d);
    estado("Actualizado a las " + new Date().toTimeString().slice(0, 5) + ".");
  }

  function consulta() {
    var q = [];
    var desde = porId("m-desde").value, hasta = porId("m-hasta").value;
    if (desde) { q.push("desde=" + encodeURIComponent(desde)); }
    if (hasta) { q.push("hasta=" + encodeURIComponent(hasta)); }
    q.push("modo=" + (porId("m-modo-falsas").checked ? "falsas" : "incidentes"));
    return q.join("&");
  }

  function cargar() {
    var mia = ++secuencia;
    if (pedido && pedido.abort) { pedido.abort(); }
    pedido = typeof AbortController === "function" ? new AbortController() : null;
    estado("Cargando…");
    var opciones = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (pedido) { opciones.signal = pedido.signal; }
    fetch("/admin/mapa/datos?" + consulta(), opciones)
      .then(respuesta)
      .then(function (x) {
        if (mia !== secuencia) { return; }
        if (x.r.status === 401) { window.location.href = "/login"; return; }
        if (x.r.status === 403) { estado("Esta pantalla es solo para administradores.", true); return; }
        if (!x.r.ok) { estado(x.d && x.d.error ? x.d.error : "No se pudo cargar el mapa.", true); return; }
        try { render(x.d); }
        catch (err) { estado("No se pudo dibujar el mapa. Recarga la página; si sigue igual, avisa al equipo.", true); }     // no es un problema de conexión
      })
      .catch(function (e) {
        if (mia !== secuencia || (e && e.name === "AbortError")) { return; }
        estado("No se pudo conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.", true);
      });
  }

  porId("mapa-filtros").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var desde = porId("m-desde").value, hasta = porId("m-hasta").value;
    if (desde && hasta && desde > hasta) { estado("La fecha inicial no puede ser posterior a la final.", true); return; }
    cargar();
  });
  porId("mapa-ultimos").addEventListener("click", function () { porId("m-desde").value = ""; porId("m-hasta").value = ""; cargar(); });
  porId("m-modo-incidentes").addEventListener("change", cargar);
  porId("m-modo-falsas").addEventListener("change", cargar);

  // El panel enlaza aquí con el rango que estaba mirando.
  (function leerInicial() {
    var q = new URLSearchParams(window.location.search);
    var re = /^\d{4}-\d{2}-\d{2}$/;
    if (re.test(q.get("desde") || "")) { porId("m-desde").value = q.get("desde"); }
    if (re.test(q.get("hasta") || "")) { porId("m-hasta").value = q.get("hasta"); }
    if (q.get("modo") === "falsas") { porId("m-modo-falsas").checked = true; porId("m-modo-incidentes").checked = false; }
  })();

  cargar();
})();
