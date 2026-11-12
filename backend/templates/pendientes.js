(function () {
  "use strict";
  var API_LISTA = "/admin/review/pending";
  var INTERVALO_MS = 10000;      // cada cuánto se pide la lista (solo con la pestaña visible)
  var OLVIDAR_MS = 30000;        // una alerta resuelta aquí no vuelve a mostrarse si llega una lista anterior a la decisión

  var lista = document.getElementById("pend-lista");
  var total = document.getElementById("pend-total");
  var estado = document.getElementById("pend-estado");
  var botonActualizar = document.getElementById("pend-actualizar");
  var enlaceMenu = document.getElementById("nav-pendientes");

  var tarjetas = {};             // id (texto) -> { el, reloj, msg, botones..., restante, recibido, escalada, enviando }
  var resueltas = {};            // id -> cuándo se resolvió desde esta pantalla
  var cargando = false;
  var listo = false;             // ya llegó la primera lista (antes no se puede decir «no hay alertas»)
  var vacio = null;

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
  function decir(texto) { estado.textContent = texto || ""; }
  function esRutaFoto(u) { return typeof u === "string" && /^\/evidence\/[A-Za-z0-9._-]+$/.test(u); }
  function claseNivel(n) { return n === "ALTO" ? "alto" : n === "MEDIO" ? "medio" : n === "BAJO" ? "bajo" : "otro"; }
  function hora(iso) {           // "2026-11-12T10:15:03" (hora de Lima) -> "12/11 10:15"
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
    return m ? m[3] + "/" + m[2] + " " + m[4] + ":" + m[5] : "";
  }
  function minutos(s) {
    var m = Math.floor(s / 60), r = Math.floor(s % 60);
    return m + ":" + (r < 10 ? "0" : "") + r;
  }
  function irAlInicio() { window.location.assign("/login"); }

  function mostrarVacio(si) {
    if (si && listo) {
      if (!vacio) {
        vacio = el("div", "vacio");
        vacio.appendChild(el("p", "vacio__titulo", "No hay alertas pendientes"));
        vacio.appendChild(el("p", "vacio__texto", "Cuando una alerta de una zona común espere revisión, aparecerá aquí."));
      }
      if (vacio.parentNode !== lista) { lista.appendChild(vacio); }
    } else if (vacio && vacio.parentNode === lista) {
      lista.removeChild(vacio);
    }
  }
  function actualizarTotales() {
    var n = Object.keys(tarjetas).length;
    total.textContent = n === 0 ? "" : n + (n === 1 ? " alerta en espera" : " alertas en espera");
    if (enlaceMenu) { enlaceMenu.textContent = "Pendientes" + (n > 0 ? " (" + n + ")" : ""); }
    mostrarVacio(n === 0);
  }

  function pintarReloj(t) {
    if (t.escalada) {
      t.reloj.textContent = "Escalada: ya se avisó a todos como no verificada.";
      t.reloj.className = "pend__reloj pend__reloj--urgente";
      return;
    }
    var s = t.restante - (Date.now() - t.recibido) / 1000;
    if (s > 0) {
      t.reloj.textContent = "Se escala en " + minutos(s);
      t.reloj.className = "pend__reloj" + (s <= 30 ? " pend__reloj--urgente" : "");
    } else {
      t.reloj.textContent = "Escalando…";
      t.reloj.className = "pend__reloj pend__reloj--urgente";
    }
  }
  function tictac() {
    Object.keys(tarjetas).forEach(function (id) { pintarReloj(tarjetas[id]); });
  }

  function fijarEnvio(t, si) {
    t.enviando = si;
    t.bConfirmar.disabled = si; t.bDescartar.disabled = si; t.bSi.disabled = si; t.bNo.disabled = si;
  }
  function quitar(id) {
    var t = tarjetas[id];
    if (t && t.el.parentNode) { t.el.parentNode.removeChild(t.el); }
    delete tarjetas[id];
  }
  function resolver(t, texto) {
    resueltas[t.id] = Date.now();
    quitar(t.id);
    decir(texto);
    actualizarTotales();
  }
  function fallo(t, texto) {
    fijarEnvio(t, false);
    t.panel.hidden = true;
    t.msg.textContent = texto;
  }

  function decidir(t, decision) {
    if (t.enviando) { return; }
    fijarEnvio(t, true);
    t.msg.textContent = "Enviando…";
    fetch("/admin/review/" + encodeURIComponent(t.id), {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ decision: decision })
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, d: d || {} }; },
                           function () { return { status: r.status, d: {} }; });
    }).then(function (x) {
      if (x.status === 200 && x.d.ok) {
        resolver(t, decision === "confirmar" ? "Alerta confirmada: se avisó a los vecinos." : "Alerta descartada como falsa alarma.");
      } else if (x.status === 409) {
        resolver(t, "Esta alerta ya la revisó otra persona" + (x.d.estado ? " (" + x.d.estado + ")" : "") + ".");
      } else if (x.status === 404) {
        resolver(t, "Esa alerta ya no está disponible.");
      } else if (x.status === 401) {
        irAlInicio();
      } else if (x.status === 403) {
        fallo(t, "Ya no tienes permiso para revisar alertas.");
      } else {
        fallo(t, "No se pudo registrar. Inténtalo de nuevo.");
      }
    }, function () {
      fallo(t, "Sin conexión. Inténtalo de nuevo.");
    });
  }

  function crearTarjeta(a) {
    var id = String(a.id);
    var art = el("article", "evento");
    art.setAttribute("data-id", id);
    var t = { id: id, el: art, enviando: false, escalada: !!a.escalated, restante: Number(a.seconds_left) || 0, recibido: Date.now() };
    var conFoto = esRutaFoto(a.image_url);
    if (conFoto) {
      var img = document.createElement("img");
      img.className = "evento__img";
      img.loading = "lazy";
      img.alt = "Foto de la alerta" + (a.camera_name ? " en " + a.camera_name : "");
      img.src = a.image_url;
      art.appendChild(img);
    } else {
      art.className += " evento--sin-imagen";
    }
    var cuerpo = el("div", "evento__cuerpo");
    var fila = el("div", "evento__fila");
    fila.appendChild(el("span", "nivel nivel--" + claseNivel(a.level), a.level || ""));
    fila.appendChild(el("span", "evento__puntaje", "Puntaje " + (a.score === undefined || a.score === null ? "" : a.score)));
    cuerpo.appendChild(fila);
    cuerpo.appendChild(el("p", "evento__camara", (a.camera_name || "Cámara sin nombre") + (a.camera_place ? " · " + a.camera_place : "")));
    cuerpo.appendChild(el("p", "evento__motivo", a.reasons || "Actividad detectada"));
    if (!conFoto) { cuerpo.appendChild(el("p", "pend__msg", "Sin foto")); }
    cuerpo.appendChild(el("p", "pend__msg", "Hora de Lima: " + hora(a.occurred_at)));
    t.reloj = el("p", "pend__reloj");
    cuerpo.appendChild(t.reloj);

    var botones = el("div", "pend__botones");
    t.bConfirmar = boton("Confirmar", "btn");
    t.bDescartar = boton("Descartar", "btn btn--texto");
    botones.appendChild(t.bConfirmar);
    botones.appendChild(t.bDescartar);
    cuerpo.appendChild(botones);

    t.panel = el("div", "pend__confirmar");
    t.panel.hidden = true;
    t.panel.appendChild(el("p", "pend__msg", "¿Confirmar esta alerta? Se avisará a los vecinos y sonará la sirena."));
    var filaSi = el("div", "pend__botones");
    t.bSi = boton("Sí, confirmar", "btn");
    t.bNo = boton("Cancelar", "btn btn--texto");
    filaSi.appendChild(t.bSi);
    filaSi.appendChild(t.bNo);
    t.panel.appendChild(filaSi);
    cuerpo.appendChild(t.panel);
    t.msg = el("p", "pend__msg");
    cuerpo.appendChild(t.msg);
    art.appendChild(cuerpo);

    t.bConfirmar.addEventListener("click", function () {
      if (t.enviando) { return; }
      t.panel.hidden = false;
      t.bConfirmar.disabled = true;
      t.bDescartar.disabled = true;
      t.bSi.focus();
    });
    t.bNo.addEventListener("click", function () {
      if (t.enviando) { return; }
      t.panel.hidden = true;
      t.bConfirmar.disabled = false;
      t.bDescartar.disabled = false;
      t.bConfirmar.focus();
    });
    t.bSi.addEventListener("click", function () { decidir(t, "confirmar"); });
    t.bDescartar.addEventListener("click", function () { decidir(t, "descartar"); });
    pintarReloj(t);
    return t;
  }

  function aplicar(items) {
    var ahora = Date.now();
    var vistos = {}, orden = [];
    items.forEach(function (a) {
      var id = String(a.id);
      if (resueltas[id] !== undefined) {
        if (ahora - resueltas[id] < OLVIDAR_MS) { return; }
        delete resueltas[id];
      }
      vistos[id] = true;
      var t = tarjetas[id];
      if (!t) {
        t = crearTarjeta(a);
        tarjetas[id] = t;
      } else {
        t.restante = Number(a.seconds_left) || 0;
        t.recibido = ahora;
        t.escalada = !!a.escalated;
        pintarReloj(t);
      }
      orden.push(t);
    });
    Object.keys(tarjetas).forEach(function (id) {
      if (!vistos[id] && !tarjetas[id].enviando) { quitar(id); }
    });
    listo = true;
    mostrarVacio(false);
    orden.forEach(function (t, i) {
      if (lista.children[i] !== t.el) { lista.insertBefore(t.el, lista.children[i] || null); }
    });
    actualizarTotales();
  }

  function cargar(manual) {
    if (cargando) { return; }
    cargando = true;
    fetch(API_LISTA, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401 || r.redirected) { irAlInicio(); return null; }
        if (r.status === 403) { decir("Ya no tienes permiso para ver esta pantalla."); return null; }
        if (!r.ok) { throw new Error("http " + r.status); }
        return r.json();
      })
      .then(function (d) {
        if (d) {
          aplicar(Array.isArray(d.events) ? d.events : []);
          decir(manual ? "Lista actualizada." : "");
        }
      }, function () {
        decir("No se pudo actualizar. Se reintentará en unos segundos.");
      })
      .then(function () { cargando = false; }, function () { cargando = false; });
  }

  botonActualizar.addEventListener("click", function () { cargar(true); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { cargar(false); } });
  setInterval(function () { if (!document.hidden) { cargar(false); } }, INTERVALO_MS);
  setInterval(tictac, 1000);
  decir("Cargando…");
  cargar(false);
})();
