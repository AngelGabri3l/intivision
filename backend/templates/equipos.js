(function () {
  "use strict";
  var API_DATOS = "/admin/equipos/datos";
  var INTERVALO_MS = 15000;      // cada cuánto se pide la lista (solo con la pestaña visible)
  var TIPOS = { usb: "USB", rtsp: "RTSP", onvif: "ONVIF" };

  var raiz = document.getElementById("eq-lista");
  var resumen = document.getElementById("eq-resumen");
  var estado = document.getElementById("eq-estado");
  var botonActualizar = document.getElementById("eq-actualizar");

  var datos = { personas: [], limites: { name_min: 2, name_max: 60, place_max: 80 } };
  var equipos = {};              // id del equipo -> { id, el, chip, meta, lista, filas: { id de la fuente -> fila } }
  var cargando = false, repetir = false, listo = false, vacio = null;
  var mensajeDeLista = false;     // el mensaje actual es de la lista (cargando, error, actualizada) y no de una acción: se puede borrar

  // Todo lo que viene del servidor entra a la página con textContent: nunca se arma HTML con ello.
  function el(tag, clase, contenido) {
    var e = document.createElement(tag);
    if (clase) { e.className = clase; }
    if (contenido !== undefined && contenido !== null) { e.textContent = contenido; }
    return e;
  }
  function boton(texto, clase) {
    var b = el("button", clase, texto);
    b.type = "button";
    return b;
  }
  function decir(texto, deLista) {
    estado.textContent = texto || "";
    mensajeDeLista = !!deLista;
  }
  function irAlInicio() { window.location.assign("/login"); }
  function hace(seg) {
    seg = Math.max(0, Math.floor(Number(seg) || 0));
    if (seg < 60) { return seg + " s"; }
    if (seg < 3600) { return Math.floor(seg / 60) + " min"; }
    if (seg < 86400) { return Math.floor(seg / 3600) + " h"; }
    return Math.floor(seg / 86400) + " d";
  }
  function limpiar(t) { return String(t || "").replace(/\s+/g, " ").trim(); }
  function nombrePersona(id) {
    for (var i = 0; i < datos.personas.length; i++) {
      if (datos.personas[i].id === id) { return datos.personas[i].full_name; }
    }
    return "esa persona";
  }

  // ------------------------------------------------------------------ qué dice cada etiqueta
  function describirEquipo(e) {
    if (e.state === "online") { return { texto: "En línea", clase: "eq-chip eq-chip--linea" }; }
    if (e.state === "offline") { return { texto: "Sin señal desde hace " + hace(e.seconds_since), clase: "eq-chip eq-chip--sin-senal" }; }
    if (e.state === "pending") { return { texto: "Pendiente: aún no se conecta", clase: "eq-chip eq-chip--neutro" }; }
    if (e.state === "revoked") { return { texto: "Revocado", clase: "eq-chip eq-chip--sin-senal" }; }
    return { texto: "Estado desconocido", clase: "eq-chip eq-chip--neutro" };
  }
  function metaEquipo(e) {
    var v = e.version ? "Versión " + e.version : "Versión sin reportar";
    return v + (e.last_seen_at ? " · Última señal: hace " + hace(e.seconds_since) : " · Sin señales todavía");
  }
  function describirAsignacion(f) {
    var c = f.camera;
    if (!c) { return { texto: "Sin asignar", clase: "eq-chip eq-chip--sin", detalle: "" }; }
    var detalle = c.name + (c.place ? " · " + c.place : "");
    if (c.kind === "common") { return { texto: "Zona común", clase: "eq-chip eq-chip--comun", detalle: detalle }; }
    return { texto: "Asignada a " + (c.owner_name || "un vecino"), clase: "eq-chip eq-chip--asignada", detalle: detalle };
  }
  function describirConexion(f, e) {
    if (f.state === "disconnected") { return { texto: "Desconectada", clase: "eq-chip eq-chip--no" }; }
    if (f.state === "error") { return { texto: "Con error", clase: "eq-chip eq-chip--no" }; }
    if (e.state !== "online") { return { texto: "Sin señal del equipo", clase: "eq-chip eq-chip--no" }; }
    return { texto: "Disponible", clase: "eq-chip eq-chip--ok" };
  }
  function metaFuente(f) {
    var p = [TIPOS[f.kind] || f.kind];
    if (f.resolution) { p.push(f.resolution); }
    if (f.address) { p.push(f.address); }
    return p.join(" · ");
  }

  // ------------------------------------------------------------------ llamadas al servidor
  function pedir(url, cuerpo) {
    var opciones = { method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (cuerpo !== undefined) {
      opciones.headers["Content-Type"] = "application/json";
      opciones.body = JSON.stringify(cuerpo);
    }
    return fetch(url, opciones).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, d: d || {} }; },
                           function () { return { status: r.status, d: {} }; });
    });
  }

  // ------------------------------------------------------------------ paneles de una cámara
  function cerrarPanel(fila) {
    while (fila.panel.firstChild) { fila.panel.removeChild(fila.panel.firstChild); }
    fila.panel.hidden = true;
    fila.modo = null;
  }
  function fijarEnvio(fila, si) {
    fila.enviando = si;
    var botones = fila.el.querySelectorAll("button");
    for (var i = 0; i < botones.length; i++) { botones[i].disabled = si; }
  }
  function falloDe(fila, x) {
    if (x.status === 401) { irAlInicio(); return; }
    if (x.status === 403) { fila.msg.textContent = "Ya no tienes permiso para hacer esto."; return; }
    fila.msg.textContent = "No se pudo completar. Inténtalo de nuevo.";
  }

  function validar(destino, nombre, lugar, dueno) {
    var lim = datos.limites;
    if (nombre.length < lim.name_min || nombre.length > lim.name_max) {
      return "El nombre debe tener entre " + lim.name_min + " y " + lim.name_max + " caracteres.";
    }
    if (lugar.length > lim.place_max) { return "El lugar admite hasta " + lim.place_max + " caracteres."; }
    if (destino === "private" && !dueno) { return "Elige a qué persona se asigna la cámara."; }
    return "";
  }

  function abrirFormulario(fila, modo) {
    cerrarPanel(fila);
    var f = fila.datos, c = f.camera, lim = datos.limites;
    var pref = "f" + String(f.id).replace(/[^A-Za-z0-9]/g, "") + "-";
    var inicial = (modo === "reasignar" && c && c.kind === "common") ? "common" : "private";
    fila.panel.hidden = false;
    fila.modo = modo;
    fila.msg.textContent = "";

    var grupo = el("fieldset", "eq-opciones");
    grupo.appendChild(el("legend", "eq-legend", "Asignar a"));
    var radios = {};
    [["common", "Zona común"], ["private", "Una persona"]].forEach(function (par) {
      var etiqueta = el("label", "eq-opcion");
      var r = document.createElement("input");
      r.type = "radio"; r.name = pref + "destino"; r.value = par[0]; r.checked = (par[0] === inicial);
      radios[par[0]] = r;
      etiqueta.appendChild(r);
      etiqueta.appendChild(document.createTextNode(par[1]));
      grupo.appendChild(etiqueta);
    });

    var campoVecino = el("div", "eq-campo");
    var etVecino = el("label", null, "Persona");
    etVecino.setAttribute("for", pref + "vecino");
    var selector = document.createElement("select");
    selector.id = pref + "vecino";
    var marca = el("option", null, "Elige una persona");
    marca.value = ""; marca.disabled = true; marca.selected = true;
    selector.appendChild(marca);
    datos.personas.forEach(function (p) {
      var o = el("option", null, p.full_name + (p.role === "community_admin" ? " · administrador" : ""));
      o.value = p.id;
      selector.appendChild(o);
    });
    if (modo === "reasignar" && c && c.kind === "private" && c.owner_id) { selector.value = c.owner_id; }
    campoVecino.appendChild(etVecino);
    campoVecino.appendChild(selector);
    campoVecino.hidden = (inicial === "common");

    var par = el("div", "eq-par");
    var campoNombre = el("div", "eq-campo");
    var etNombre = el("label", null, "Nombre de la cámara");
    etNombre.setAttribute("for", pref + "nombre");
    var inNombre = document.createElement("input");
    inNombre.type = "text"; inNombre.id = pref + "nombre"; inNombre.maxLength = lim.name_max;
    inNombre.value = (c ? c.name : f.label).slice(0, lim.name_max);
    campoNombre.appendChild(etNombre); campoNombre.appendChild(inNombre);
    var campoLugar = el("div", "eq-campo");
    var etLugar = el("label", null, "Lugar (opcional)");
    etLugar.setAttribute("for", pref + "lugar");
    var inLugar = document.createElement("input");
    inLugar.type = "text"; inLugar.id = pref + "lugar"; inLugar.maxLength = lim.place_max;
    inLugar.value = c && c.place ? c.place : "";
    campoLugar.appendChild(etLugar); campoLugar.appendChild(inLugar);
    par.appendChild(campoNombre); par.appendChild(campoLugar);

    var error = el("p", "eq-error");
    error.setAttribute("role", "alert");
    var botones = el("div", "eq-fila__acciones");
    var bEnviar = boton(modo === "reasignar" ? "Reasignar cámara" : "Asignar cámara", "btn");
    var bCancelar = boton("Cancelar", "btn btn--texto");
    botones.appendChild(bEnviar); botones.appendChild(bCancelar);

    [grupo, campoVecino, par, error, botones].forEach(function (x) { fila.panel.appendChild(x); });

    radios.common.addEventListener("change", function () { campoVecino.hidden = true; });
    radios.private.addEventListener("change", function () { campoVecino.hidden = false; });
    bCancelar.addEventListener("click", function () { if (!fila.enviando) { cerrarPanel(fila); } });
    bEnviar.addEventListener("click", function () {
      if (fila.enviando) { return; }
      var destino = radios.common.checked ? "common" : "private";
      var nombre = limpiar(inNombre.value), lugar = limpiar(inLugar.value), dueno = selector.value;
      var problema = validar(destino, nombre, lugar, dueno);
      if (problema) { error.textContent = problema; return; }
      error.textContent = "";
      var cuerpo = { destino: destino, name: nombre, place: lugar };
      if (destino === "private") { cuerpo.owner_id = dueno; }
      fijarEnvio(fila, true);
      fila.msg.textContent = "Enviando…";
      pedir("/admin/sources/" + encodeURIComponent(f.id) + (modo === "reasignar" ? "/reassign" : "/assign"), cuerpo).then(function (x) {
        fijarEnvio(fila, false);
        fila.msg.textContent = "";
        if (x.status === 201) {
          cerrarPanel(fila);
          decir("Cámara «" + nombre + "» " + (modo === "reasignar" ? "reasignada a " : "asignada a ") +
                (destino === "common" ? "la zona común" : nombrePersona(dueno)) + ".");
          cargar(false);
        } else if (x.status === 409 && x.d.code === "ya_asignada") {
          cerrarPanel(fila);
          decir("Otra persona asignó esta cámara mientras tanto. Se actualizó la lista.");
          cargar(false);
        } else if (x.status === 409 || x.status === 422) {
          error.textContent = x.d.error || "No se pudo asignar la cámara.";
        } else if (x.status === 404) {
          cerrarPanel(fila);
          decir("Esa cámara ya no existe.");
          cargar(false);
        } else {
          falloDe(fila, x);
        }
      }, function () {
        fijarEnvio(fila, false);
        fila.msg.textContent = "Sin conexión. Inténtalo de nuevo.";
      });
    });
    inNombre.focus();
  }

  function abrirConfirmacion(fila) {
    cerrarPanel(fila);
    var f = fila.datos, c = f.camera;
    if (!c) { return; }
    fila.panel.hidden = false;
    fila.modo = "quitar";
    fila.msg.textContent = "";
    var quien = c.kind === "common" ? "la zona común" : (c.owner_name || "su dueño");
    fila.panel.appendChild(el("p", "eq-pregunta", "¿Quitar la asignación de " + quien + "?"));
    fila.panel.appendChild(el("p", "eq-msg", "La cámara «" + c.name + "» se archiva y su historial se conserva."));
    var botones = el("div", "eq-fila__acciones");
    var bSi = boton("Sí, quitar", "btn");
    var bNo = boton("Cancelar", "btn btn--texto");
    botones.appendChild(bSi); botones.appendChild(bNo);
    fila.panel.appendChild(botones);
    bNo.addEventListener("click", function () { if (!fila.enviando) { cerrarPanel(fila); fila.bQuitar.focus(); } });
    bSi.addEventListener("click", function () {
      if (fila.enviando) { return; }
      fijarEnvio(fila, true);
      fila.msg.textContent = "Enviando…";
      pedir("/admin/sources/" + encodeURIComponent(f.id) + "/unassign").then(function (x) {
        fijarEnvio(fila, false);
        fila.msg.textContent = "";
        if (x.status === 200) {
          cerrarPanel(fila);
          decir("Se quitó la asignación de «" + c.name + "». La cámara quedó archivada.");
          cargar(false);
        } else if (x.status === 409 || x.status === 404) {
          cerrarPanel(fila);
          decir(x.status === 409 ? "Esa cámara ya no estaba asignada." : "Esa cámara ya no existe.");
          cargar(false);
        } else {
          falloDe(fila, x);
        }
      }, function () {
        fijarEnvio(fila, false);
        fila.msg.textContent = "Sin conexión. Inténtalo de nuevo.";
      });
    });
    bSi.focus();
  }

  // ------------------------------------------------------------------ filas y tarjetas
  function crearFila(equipo, f) {
    var fila = { id: f.id, datos: f, enviando: false, modo: null };
    fila.el = el("div", "eq-fila");
    fila.el.setAttribute("data-fuente", f.id);
    var info = el("div", "eq-fila__info");
    fila.nombre = el("div", "eq-fila__nombre");
    fila.meta = el("div", "eq-fila__meta");
    info.appendChild(fila.nombre); info.appendChild(fila.meta);
    var est = el("div", "eq-fila__estado");
    fila.asignacion = el("span");
    fila.detalle = el("span", "eq-fila__lugar");
    fila.conexion = el("span");
    est.appendChild(fila.asignacion); est.appendChild(fila.detalle); est.appendChild(fila.conexion);
    var acciones = el("div", "eq-fila__acciones");
    fila.bAsignar = boton("Asignar", "btn");
    fila.bReasignar = boton("Reasignar", "btn");
    fila.bQuitar = boton("Quitar asignación", "btn btn--texto");
    acciones.appendChild(fila.bAsignar); acciones.appendChild(fila.bReasignar); acciones.appendChild(fila.bQuitar);
    fila.panel = el("div", "eq-fila__panel");
    fila.panel.hidden = true;
    fila.msg = el("p", "eq-msg");
    fila.el.appendChild(info); fila.el.appendChild(est); fila.el.appendChild(acciones);
    fila.el.appendChild(fila.panel); fila.el.appendChild(fila.msg);
    fila.bAsignar.addEventListener("click", function () { if (!fila.enviando) { abrirFormulario(fila, "asignar"); } });
    fila.bReasignar.addEventListener("click", function () { if (!fila.enviando) { abrirFormulario(fila, "reasignar"); } });
    fila.bQuitar.addEventListener("click", function () { if (!fila.enviando) { abrirConfirmacion(fila); } });
    actualizarFila(fila, equipo, f);
    return fila;
  }
  function actualizarFila(fila, equipo, f) {
    fila.datos = f;
    fila.nombre.textContent = f.label;
    fila.meta.textContent = metaFuente(f);
    var a = describirAsignacion(f), c = describirConexion(f, equipo);
    fila.asignacion.className = a.clase; fila.asignacion.textContent = a.texto;
    fila.detalle.textContent = a.detalle;
    fila.conexion.className = c.clase; fila.conexion.textContent = c.texto;
    var asignada = !!f.camera;
    fila.bAsignar.hidden = asignada;
    fila.bReasignar.hidden = !asignada;
    fila.bQuitar.hidden = !asignada;
  }

  function crearEquipo(e) {
    var t = { id: e.id, filas: {} };
    t.el = el("section", "eq-equipo");
    t.el.setAttribute("data-equipo", e.id);
    var cab = el("div", "eq-equipo__cab");
    var titulo = el("div", "eq-equipo__titulo");
    t.nombre = el("h2", "eq-equipo__nombre");
    t.chip = el("span");
    titulo.appendChild(t.nombre); titulo.appendChild(t.chip);
    t.meta = el("p", "eq-equipo__meta");
    cab.appendChild(titulo); cab.appendChild(t.meta);
    t.lista = el("div", "eq-equipo__filas");
    t.sinCamaras = el("p", "eq-vacio-equipo", "Este equipo todavía no reportó cámaras.");
    t.el.appendChild(cab); t.el.appendChild(t.lista); t.el.appendChild(t.sinCamaras);
    return t;
  }
  function actualizarEquipo(t, e) {
    t.datos = e;
    t.nombre.textContent = e.name;
    var d = describirEquipo(e);
    t.chip.className = d.clase; t.chip.textContent = d.texto;
    t.meta.textContent = metaEquipo(e);
    var vistas = {}, orden = [];
    (e.sources || []).forEach(function (f) {
      vistas[f.id] = true;
      var fila = t.filas[f.id];
      if (!fila) { fila = crearFila(e, f); t.filas[f.id] = fila; } else { actualizarFila(fila, e, f); }
      orden.push(fila);
    });
    Object.keys(t.filas).forEach(function (id) {
      if (!vistas[id] && !t.filas[id].enviando) {
        if (t.filas[id].el.parentNode) { t.filas[id].el.parentNode.removeChild(t.filas[id].el); }
        delete t.filas[id];
      }
    });
    orden.forEach(function (fila, i) {
      if (t.lista.children[i] !== fila.el) { t.lista.insertBefore(fila.el, t.lista.children[i] || null); }
    });
    t.sinCamaras.hidden = orden.length > 0;
  }

  function mostrarVacio(si) {
    if (si && listo) {
      if (!vacio) {
        vacio = el("div", "vacio");
        vacio.appendChild(el("p", "vacio__titulo", "Aún no hay equipos"));
        vacio.appendChild(el("p", "vacio__texto", "Cuando conectes una mini PC con su código de acceso, aparecerá aquí con sus cámaras."));
      }
      if (vacio.parentNode !== raiz) { raiz.appendChild(vacio); }
    } else if (vacio && vacio.parentNode === raiz) {
      raiz.removeChild(vacio);
    }
  }
  function pintarResumen() {
    var ids = Object.keys(equipos), camaras = 0, sin = 0;
    ids.forEach(function (id) {
      (equipos[id].datos.sources || []).forEach(function (f) { camaras++; if (!f.camera) { sin++; } });
    });
    while (resumen.firstChild) { resumen.removeChild(resumen.firstChild); }
    [[ids.length, ids.length === 1 ? "equipo" : "equipos"], [camaras, camaras === 1 ? "cámara detectada" : "cámaras detectadas"], [sin, "sin asignar"]].forEach(function (par) {
      var d = el("div", "eq-dato");
      d.appendChild(el("strong", null, String(par[0])));
      d.appendChild(document.createTextNode(par[1]));
      resumen.appendChild(d);
    });
  }

  function aplicar(d) {
    datos.personas = Array.isArray(d.personas) ? d.personas : [];
    if (d.limites && typeof d.limites === "object") { datos.limites = d.limites; }
    var vistos = {}, orden = [];
    (Array.isArray(d.equipos) ? d.equipos : []).forEach(function (e) {
      vistos[e.id] = true;
      var t = equipos[e.id];
      if (!t) { t = crearEquipo(e); equipos[e.id] = t; }
      actualizarEquipo(t, e);
      orden.push(t);
    });
    Object.keys(equipos).forEach(function (id) {
      if (!vistos[id]) { if (equipos[id].el.parentNode) { equipos[id].el.parentNode.removeChild(equipos[id].el); } delete equipos[id]; }
    });
    listo = true;
    mostrarVacio(false);
    orden.forEach(function (t, i) {
      if (raiz.children[i] !== t.el) { raiz.insertBefore(t.el, raiz.children[i] || null); }
    });
    pintarResumen();
    mostrarVacio(orden.length === 0);
  }

  function cargar(manual) {
    if (cargando) { repetir = true; return; }
    cargando = true;
    fetch(API_DATOS, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401 || r.redirected) { irAlInicio(); return null; }
        if (r.status === 403) { decir("Ya no tienes permiso para ver esta pantalla.", true); return null; }
        if (!r.ok) { throw new Error("http " + r.status); }
        return r.json();
      })
      .then(function (d) {
        if (d) {
          aplicar(d);
          if (manual) { decir("Lista actualizada.", true); } else if (mensajeDeLista) { decir(""); }
        }
      }, function () {
        decir("No se pudo actualizar. Se reintentará en unos segundos.", true);
      })
      .then(function () {
        cargando = false;
        if (repetir) { repetir = false; cargar(false); }
      }, function () { cargando = false; });
  }

  botonActualizar.addEventListener("click", function () { cargar(true); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { cargar(false); } });
  setInterval(function () { if (!document.hidden) { cargar(false); } }, INTERVALO_MS);
  decir("Cargando…", true);
  cargar(false);
})();
