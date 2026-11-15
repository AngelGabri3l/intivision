/* Ubicación de la cámara (H8, fase D): la sección de la ficha para el DUEÑO de una cámara privada. Escribe SIEMPRE con textContent.
   La ubicación es un dato personal: se guarda solo si la persona la elige, no se muestra al administrador sin su permiso expreso (casilla
   apagada por defecto) y se puede quitar o retirar en cualquier momento. */
(function () {
  "use strict";

  var raiz = document.getElementById("ubicacion");
  var d = raiz.dataset;
  var camara = d.camara;
  var CONSENTIMIENTO = "Permito que el administrador vea la ubicación exacta de mi cámara en el mapa de la comunidad.";

  function numero(t) { return t === "" || t === undefined ? null : parseFloat(t); }
  var actual = { lat: numero(d.lat), lng: numero(d.lng), precision: d.precision === "" ? null : parseInt(d.precision, 10),
    visible: d.visible === "1", adminVe: d.adminVe === "1", fijada: d.fijada || "" };
  var centro = { lat: numero(d.centroLat), lng: numero(d.centroLng), definido: d.centroDefinido === "1" };

  var punto = null;             // lo que se eligió en el editor y aún no se guardó: { lat, lng, precision }
  var mapaL = null, pin = null, teselas = null;
  var ocupado = false, quitando = false, avisoTeselas = false;

  function nodo(tag, clase, texto) {
    var e = document.createElement(tag);
    if (clase) { e.className = clase; }
    if (texto !== undefined && texto !== null) { e.textContent = String(texto); }
    return e;
  }
  function vaciar(e) { while (e.firstChild) { e.removeChild(e.firstChild); } }
  function fmt(v) { return v.toFixed(6); }
  function fechaLarga(iso) { var p = String(iso).slice(0, 10).split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function textoError(x, porDefecto) {
    return x.r.status === 401 ? "Tu sesión terminó. Vuelve a iniciar sesión y repite." : (x.d && x.d.error ? x.d.error : porDefecto);
  }
  function respuesta(r) { return r.json().catch(function () { return {}; }).then(function (x) { return { r: r, d: x }; }); }
  function enviar(sufijo, cuerpo) {
    return fetch("/cameras/" + encodeURIComponent(camara) + "/ubicacion" + sufijo, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(cuerpo || {}) }).then(respuesta);
  }

  var cuerpo = document.getElementById("ubic-cuerpo");
  var resumen = nodo("div"), editor = nodo("div", "ubic__editor");
  editor.hidden = true;
  cuerpo.appendChild(resumen); cuerpo.appendChild(editor);
  var mensaje = nodo("p", "ubic__mensaje");
  mensaje.setAttribute("role", "status"); mensaje.setAttribute("aria-live", "polite");
  cuerpo.appendChild(mensaje);

  function avisar(texto, error) { mensaje.textContent = texto || ""; mensaje.className = "ubic__mensaje" + (error ? " ubic__mensaje--error" : ""); }

  // ------------------------------------------------------------------ el resumen: estado, permiso y botones

  function permisoVisible() {
    var fila = nodo("label", "ubic__consentimiento");
    var c = nodo("input"); c.type = "checkbox"; c.id = "ubic-permiso"; c.checked = actual.visible;
    c.addEventListener("change", function () {
      var quiere = c.checked;
      enviar("/mapa", { visible: quiere }).then(function (x) {
        if (x.r.ok) {
          actual.visible = quiere;
          avisar(quiere ? "Listo: el administrador podrá ver tu cámara en el mapa." : "Listo: tu cámara ya no aparece en el mapa del administrador.");
        } else { avisar(textoError(x, "No se pudo cambiar el permiso."), true); }          // el redibujado de abajo devuelve la casilla a como estaba
        dibujarResumen();
      }).catch(function () { c.checked = !quiere; avisar("No se pudo conectar con el servidor. Inténtalo de nuevo.", true); });
    });
    fila.appendChild(c); fila.appendChild(nodo("span", "", CONSENTIMIENTO));
    return fila;
  }

  function dibujarResumen() {
    vaciar(resumen);
    if (actual.lat === null) {
      resumen.appendChild(nodo("p", "", "Esta cámara no tiene ubicación asignada."));
    } else {
      resumen.appendChild(nodo("p", "", "Ubicación guardada: " + fmt(actual.lat) + ", " + fmt(actual.lng) +
        (actual.precision !== null ? " (precisión de unos " + actual.precision + " m)" : "") + (actual.fijada ? ". Fijada el " + fechaLarga(actual.fijada) + "." : ".")));
      resumen.appendChild(permisoVisible());
      if (actual.visible && !actual.adminVe) {
        resumen.appendChild(nodo("p", "ubic__suave", "Ahora mismo no aparecerá en el mapa porque tienes esta cámara oculta al administrador."));
      }
    }
    var botones = nodo("div", "ubic__botones");
    var abrir = nodo("button", "btn", actual.lat === null ? "Asignar ubicación" : "Cambiar ubicación"); abrir.type = "button"; abrir.id = "ubic-abrir";
    abrir.addEventListener("click", abrirEditor);
    botones.appendChild(abrir);
    if (actual.lat !== null) {
      var q = nodo("button", "btn btn--texto", quitando ? "Confirmar: quitar ubicación" : "Quitar ubicación"); q.type = "button"; q.id = "ubic-quitar";
      q.addEventListener("click", function () {
        if (!quitando) { quitando = true; dibujarResumen(); return; }
        quitando = false;
        enviar("/quitar", {}).then(function (x) {
          if (x.r.ok) { actual.lat = actual.lng = null; actual.precision = null; actual.visible = false; actual.fijada = ""; avisar("Se quitó la ubicación de tu cámara."); }
          else { avisar(textoError(x, "No se pudo quitar la ubicación."), true); }
          dibujarResumen();
        }).catch(function () { avisar("No se pudo conectar con el servidor. Inténtalo de nuevo.", true); dibujarResumen(); });
      });
      botones.appendChild(q);
    }
    resumen.appendChild(botones);
  }

  // ------------------------------------------------------------------ el editor: elegir el punto

  var etPunto, btnGuardar, chkPermiso, cajaLejos, textoLejos, enlaceGoogle, campoPegar, avisoEditor, divMapa;

  function enlaceGoogleMaps() {
    var p = punto || (actual.lat !== null ? actual : null) || centro;
    return "https://www.google.com/maps/@" + p.lat + "," + p.lng + ",18z";
  }

  function textoPunto() {
    etPunto.textContent = punto ? "Punto elegido: " + fmt(punto.lat) + ", " + fmt(punto.lng) + (punto.precision !== null && punto.precision !== undefined ? " (precisión de unos " + punto.precision + " m)" : "")
      : "Aún no elegiste un punto.";
    btnGuardar.disabled = !punto || ocupado;
    enlaceGoogle.setAttribute("href", enlaceGoogleMaps());
  }

  // Dibuja (o mueve) el pin en el mapa. No elige nada: solo muestra dónde está.
  function colocarPin(lat, lng, centrar) {
    if (!mapaL || !window.L) { return; }
    if (!pin) {
      pin = window.L.marker([lat, lng], { draggable: true, keyboard: false,
        icon: window.L.divIcon({ html: nodo("div", "ubic-pin"), className: "ubic-pin-caja", iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(mapaL);
      pin.on("dragend", function () { var ll = pin.getLatLng(); ponerPunto(ll.lat, ll.lng, null, false); });
    } else { pin.setLatLng([lat, lng]); }
    if (centrar) { mapaL.setView([lat, lng], 18); }
  }

  // La persona eligió este punto (con un clic, arrastrando el pin, con el GPS o pegando coordenadas): ahora se puede guardar.
  function ponerPunto(lat, lng, precision, centrar) {
    punto = { lat: Math.round(lat * 1e6) / 1e6, lng: Math.round(lng * 1e6) / 1e6, precision: precision === undefined ? null : precision };
    cajaLejos.hidden = true;
    avisoEditor.textContent = punto.precision !== null && punto.precision > 100 ? "La ubicación del teléfono es poco precisa (unos " + punto.precision + " m). Ajusta el punto en el mapa antes de guardar." : "";
    colocarPin(punto.lat, punto.lng, centrar);
    textoPunto();
  }

  function iniciarMapa() {
    if (mapaL) { mapaL.invalidateSize(); return; }
    var L = window.L;
    if (!L) { avisoEditor.textContent = "No se pudo cargar el mapa. Usa el GPS o pega las coordenadas."; return; }
    var base = actual.lat !== null ? actual : centro;
    mapaL = L.map(divMapa, { center: [base.lat, base.lng], zoom: actual.lat !== null || centro.definido ? 17 : 12 });
    teselas = L.tileLayer("/mapa/teselas/{z}/{x}/{y}.png", { maxZoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">colaboradores de OpenStreetMap</a>' }).addTo(mapaL);
    teselas.on("tileerror", function () {
      if (!avisoTeselas) { avisoTeselas = true; avisoEditor.textContent = "El mapa de fondo no está disponible ahora. Puedes usar el GPS o pegar las coordenadas."; }
    });
    mapaL.on("click", function (ev) { ponerPunto(ev.latlng.lat, ev.latlng.lng, null, false); });
  }

  function construirEditor() {
    var fila1 = nodo("div", "ubic__botones");
    var gps = nodo("button", "btn btn--texto", "Usar mi ubicación actual"); gps.type = "button"; gps.id = "ubic-gps";
    gps.addEventListener("click", function () {
      avisoEditor.textContent = "Buscando tu ubicación…";
      window.VigiaCoordenadas.miUbicacion(function (r) {
        if (r.error) { avisoEditor.textContent = r.error; return; }
        avisoEditor.textContent = "";
        ponerPunto(r.lat, r.lng, r.precision, true);
      });
    });
    enlaceGoogle = nodo("a", "btn btn--texto", "Abrir Google Maps");
    enlaceGoogle.id = "ubic-google"; enlaceGoogle.setAttribute("target", "_blank"); enlaceGoogle.setAttribute("rel", "noopener noreferrer");
    fila1.appendChild(gps); fila1.appendChild(enlaceGoogle);
    divMapa = nodo("div", "ubic__mapa"); divMapa.id = "ubic-mapa";
    divMapa.setAttribute("role", "application"); divMapa.setAttribute("aria-label", "Mapa para elegir dónde está tu cámara");
    var pegar = nodo("div", "ubic__campo");
    var et = nodo("label", "ubic__suave", "O pega las coordenadas o un enlace de Google Maps (en Google Maps, mantén presionado el punto y copia los números)");
    campoPegar = nodo("input"); campoPegar.type = "text"; campoPegar.id = "ubic-pegar"; campoPegar.autocomplete = "off"; campoPegar.spellcheck = false;
    campoPegar.placeholder = "−12.046374, −77.042793"; et.setAttribute("for", "ubic-pegar");
    var usar = nodo("button", "btn btn--texto", "Usar estas coordenadas"); usar.type = "button"; usar.id = "ubic-usar";
    usar.addEventListener("click", function () {
      var r = window.VigiaCoordenadas.parsear(campoPegar.value);
      if (r.error) { avisoEditor.textContent = r.error; return; }
      avisoEditor.textContent = "";
      ponerPunto(r.lat, r.lng, null, true);
    });
    pegar.appendChild(et); pegar.appendChild(campoPegar); pegar.appendChild(usar);
    etPunto = nodo("p", "ubic__mensaje");
    etPunto.id = "ubic-punto";
    avisoEditor = nodo("p", "ubic__suave"); avisoEditor.id = "ubic-aviso"; avisoEditor.setAttribute("role", "status"); avisoEditor.setAttribute("aria-live", "polite");
    var consentimiento = nodo("label", "ubic__consentimiento");
    chkPermiso = nodo("input"); chkPermiso.type = "checkbox"; chkPermiso.id = "ubic-consentimiento";
    consentimiento.appendChild(chkPermiso); consentimiento.appendChild(nodo("span", "", CONSENTIMIENTO + " Puedes retirar el permiso cuando quieras."));
    cajaLejos = nodo("div", "ubic__lejos"); cajaLejos.hidden = true;
    textoLejos = nodo("p"); cajaLejos.appendChild(textoLejos);
    var filaLejos = nodo("div", "ubic__botones");
    var si = nodo("button", "btn", "Sí, es la correcta"); si.type = "button"; si.id = "ubic-lejos-si";
    var no = nodo("button", "btn btn--texto", "No, elegir otra"); no.type = "button"; no.id = "ubic-lejos-no";
    si.addEventListener("click", function () { guardar(true); });
    no.addEventListener("click", function () { cajaLejos.hidden = true; });
    filaLejos.appendChild(si); filaLejos.appendChild(no); cajaLejos.appendChild(filaLejos);
    var fila2 = nodo("div", "ubic__botones");
    btnGuardar = nodo("button", "btn", "Guardar ubicación"); btnGuardar.type = "button"; btnGuardar.id = "ubic-guardar"; btnGuardar.disabled = true;
    btnGuardar.addEventListener("click", function () { guardar(false); });
    var cancelar = nodo("button", "btn btn--texto", "Cancelar"); cancelar.type = "button"; cancelar.id = "ubic-cancelar";
    cancelar.addEventListener("click", cerrarEditor);
    fila2.appendChild(btnGuardar); fila2.appendChild(cancelar);
    [fila1, divMapa, pegar, etPunto, avisoEditor, consentimiento, cajaLejos, fila2].forEach(function (e) { editor.appendChild(e); });
  }

  function abrirEditor() {
    if (!divMapa) { construirEditor(); }
    editor.hidden = false;
    avisar("");
    punto = null;
    chkPermiso.checked = actual.visible;
    avisoEditor.textContent = "";
    cajaLejos.hidden = true;
    textoPunto();
    iniciarMapa();
    if (!mapaL) { return; }
    if (actual.lat !== null) { colocarPin(actual.lat, actual.lng, true); }       // se ve dónde está hoy; hasta elegir otro punto no se puede guardar
    else if (pin) { mapaL.removeLayer(pin); pin = null; }                        // se quitó la ubicación desde la última vez
  }

  function cerrarEditor() { editor.hidden = true; punto = null; cajaLejos.hidden = true; }

  function guardar(confirmar) {
    if (!punto || ocupado) { return; }
    ocupado = true;
    btnGuardar.disabled = true;
    var elegido = punto, quiere = chkPermiso.checked;
    avisoEditor.textContent = "Guardando…";
    enviar("", { lat: elegido.lat, lng: elegido.lng, precision_m: elegido.precision, visible_en_mapa: quiere, confirmar_lejos: !!confirmar })
      .then(function (x) {
        ocupado = false;
        if (x.r.ok) {
          actual.lat = x.d.lat; actual.lng = x.d.lng; actual.precision = x.d.precision_m; actual.visible = !!x.d.visible_en_mapa;
          actual.fijada = new Date().toISOString();
          cerrarEditor();
          dibujarResumen();
          avisar("Ubicación guardada. " + (actual.visible ? "El administrador la verá en el mapa." : "El administrador no la verá en el mapa."));
        } else if (x.d && x.d.code === "muy_lejos") {
          textoLejos.textContent = x.d.error; cajaLejos.hidden = false; avisoEditor.textContent = ""; textoPunto();
        } else {
          avisoEditor.textContent = textoError(x, "No se pudo guardar la ubicación.");
          textoPunto();
        }
      })
      .catch(function () { ocupado = false; avisoEditor.textContent = "No se pudo conectar con el servidor. Inténtalo de nuevo."; textoPunto(); });
  }

  dibujarResumen();
})();
