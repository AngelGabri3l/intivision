/* Panel de incidentes (H8). Pide /admin/panel/datos y dibuja todo con SVG y DOM. Escribe SIEMPRE con textContent: ningún dato
   (nombres de cámara, lugares, descripciones) se interpreta como HTML. */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var DIAS_CORTOS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
  var NIVELES = ["ALTO", "MEDIO", "BAJO"];
  var CLASE_NIVEL = { ALTO: "sv-alto", MEDIO: "sv-medio", BAJO: "sv-bajo" };
  var TITULO = { hoy: "Hoy", "7d": "Últimos 7 días", "30d": "Últimos 30 días" };
  var PREVIO = { hoy: "ayer a esta hora", "7d": "los 7 días anteriores", "30d": "los 30 días anteriores" };

  var pedido = null;        // el AbortController del pedido en curso
  var secuencia = 0;        // cuál es el pedido más reciente: las respuestas viejas se ignoran
  var ultimo = null;        // los últimos datos recibidos (para volver a dibujar el mapa al girar el celular)

  function porId(id) { return document.getElementById(id); }

  function nodo(tag, clase, texto) {
    var e = document.createElement(tag);
    if (clase) { e.className = clase; }
    if (texto !== undefined && texto !== null) { e.textContent = String(texto); }
    return e;
  }

  function svg(tag, atributos, texto) {
    var e = document.createElementNS(SVG, tag);
    for (var k in atributos) {
      if (Object.prototype.hasOwnProperty.call(atributos, k)) { e.setAttribute(k, String(atributos[k])); }
    }
    if (texto !== undefined && texto !== null) { e.textContent = String(texto); }
    return e;
  }

  function vaciar(e) { while (e.firstChild) { e.removeChild(e.firstChild); } }

  function miles(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0"); }
  function fechaCorta(iso) { var p = String(iso).split("-"); return p[2] + "/" + p[1]; }
  function fechaLarga(iso) { var p = String(iso).split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function plural(n, uno, varios) { return n + " " + (n === 1 ? uno : varios); }

  function hace(segundos) {
    if (segundos === null || segundos === undefined) { return ""; }
    if (segundos < 60) { return segundos + " s"; }
    if (segundos < 3600) { return Math.floor(segundos / 60) + " min"; }
    if (segundos < 86400) { return Math.floor(segundos / 3600) + " h"; }
    return Math.floor(segundos / 86400) + (Math.floor(segundos / 86400) === 1 ? " día" : " días");
  }

  function estado(texto, error) {
    var e = porId("panel-estado");
    e.textContent = texto || "";
    e.className = "panel__estado" + (error ? " panel__estado--error" : "");
  }

  function leyenda(contenedor, items) {
    vaciar(contenedor);
    items.forEach(function (it) {
      var li = nodo("li");
      li.appendChild(nodo("span", "leyenda__muestra " + it.clase));
      li.appendChild(document.createTextNode(it.texto));
      contenedor.appendChild(li);
    });
  }

  // ------------------------------------------------------------------ tarjetas

  function textoDelta(c, previo) {
    if (c.diferencia === 0) { return "■ Igual que " + previo; }
    var flecha = c.diferencia > 0 ? "▲ +" : "▼ −";
    var txt = flecha + Math.abs(c.diferencia);
    if (c.porcentaje !== null && c.porcentaje !== undefined) {
      txt += " (" + (c.porcentaje > 0 ? "+" : (c.porcentaje < 0 ? "−" : "")) + Math.abs(c.porcentaje) + " %)";
    }
    txt += " frente a " + previo;
    if (c.nuevo) { txt += " (antes: ninguno)"; }
    return txt;
  }

  function tarjetaIncidentes(clave, t) {
    var a = nodo("article", "tarjeta");
    a.appendChild(nodo("h3", "tarjeta__titulo", TITULO[clave]));
    a.appendChild(nodo("p", "tarjeta__valor", miles(t.actual.total)));
    var partes = ["ALTO " + t.actual.ALTO, "MEDIO " + t.actual.MEDIO];
    if (t.actual.BAJO) { partes.push("BAJO " + t.actual.BAJO); }
    a.appendChild(nodo("p", "tarjeta__detalle", partes.join(" · ")));
    a.appendChild(nodo("p", "tarjeta__delta", textoDelta(t.comparacion, PREVIO[clave])));
    if (t.comparacion_alto.diferencia !== 0) {
      var alto = t.comparacion_alto;
      a.appendChild(nodo("p", "tarjeta__detalle", "ALTO: " + (alto.diferencia > 0 ? "▲ +" : "▼ −") + Math.abs(alto.diferencia)));
    }
    return a;
  }

  function dibujarTarjetas(d) {
    var cont = porId("panel-tarjetas");
    vaciar(cont);
    ["hoy", "7d", "30d"].forEach(function (k) { cont.appendChild(tarjetaIncidentes(k, d.tarjetas[k])); });

    var cam = nodo("article", "tarjeta");
    cam.appendChild(nodo("h3", "tarjeta__titulo", "Cámaras en línea"));
    if (d.camaras.total === 0) {
      cam.appendChild(nodo("p", "tarjeta__valor", "—"));
      cam.appendChild(nodo("p", "tarjeta__detalle", "Sin cámaras de mini PC que puedas ver"));
    } else {
      cam.appendChild(nodo("p", "tarjeta__valor", d.camaras.en_linea + " de " + d.camaras.total));
      cam.appendChild(nodo("p", "tarjeta__detalle", d.camaras.en_linea === d.camaras.total ? "Todas dan señal" : (d.camaras.total - d.camaras.en_linea) + " sin señal"));
    }
    cont.appendChild(cam);

    var pen = nodo("article", "tarjeta");
    pen.appendChild(nodo("h3", "tarjeta__titulo", "Pendientes de revisión"));
    pen.appendChild(nodo("p", "tarjeta__valor", d.pendientes === null || d.pendientes === undefined ? "—" : d.pendientes));
    pen.appendChild(nodo("p", "tarjeta__detalle", d.pendientes === 0 ? "Nada espera revisión" : "Esperan que alguien las revise"));
    var enlace = nodo("a", "tarjeta__enlace", "Revisar pendientes");
    enlace.setAttribute("href", "/admin/pendientes");
    pen.appendChild(enlace);
    cont.appendChild(pen);
  }

  // ------------------------------------------------------------------ incidentes por día

  function paso(max) {
    var crudo = max / 4;
    var potencia = Math.pow(10, Math.floor(Math.log(crudo) / Math.LN10));
    var f = crudo / potencia;
    var m = f <= 1 ? 1 : (f <= 2 ? 2 : (f <= 5 ? 5 : 10));
    return Math.max(1, m * potencia);
  }

  function dibujarDiario(serie) {
    var cont = porId("panel-diario");
    vaciar(cont);
    var max = 0;
    serie.forEach(function (s) { if (s.total > max) { max = s.total; } });
    if (max === 0) {
      cont.appendChild(nodo("p", "panel__nota", "No hay incidentes en este rango."));
      vaciar(porId("panel-diario-leyenda"));
      return;
    }
    var W = 720, H = 260, L = 40, R = 10, T = 12, B = 30;
    var salto = paso(max);
    var tope = salto * Math.ceil(max / salto);
    var escala = (H - T - B) / tope;
    var n = serie.length;
    var ancho = (W - L - R) / n;
    var barra = Math.max(1, ancho * 0.7);
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, "class": "panel__grafico", role: "img", "aria-label": "Incidentes por día en el rango, apilados por nivel" });
    s.appendChild(svg("title", {}, "Incidentes por día en el rango, apilados por nivel"));
    for (var v = 0; v <= tope; v += salto) {
      var y = H - B - v * escala;
      s.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, "class": "sv-eje" }));
      s.appendChild(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "class": "sv-texto sv-texto--suave" }, v));
    }
    serie.forEach(function (dia, i) {
      var x = L + i * ancho + (ancho - barra) / 2;
      var g = svg("g", {});
      var base = H - B;
      NIVELES.forEach(function (nivel) {
        var alto = dia[nivel] * escala;
        if (alto > 0) {
          g.appendChild(svg("rect", { x: x, y: base - alto, width: barra, height: alto, "class": CLASE_NIVEL[nivel] }));
          base -= alto;
        }
      });
      var partes = NIVELES.filter(function (nv) { return dia[nv] > 0; }).map(function (nv) { return dia[nv] + " " + nv; });
      g.appendChild(svg("title", {}, fechaLarga(dia.dia) + ": " + plural(dia.total, "incidente", "incidentes") + (partes.length ? " (" + partes.join(", ") + ")" : "")));
      s.appendChild(g);
    });
    var vistos = {};
    for (var k = 0; k < 6; k++) {
      var i2 = n === 1 ? 0 : Math.round(k * (n - 1) / 5);
      if (vistos[i2]) { continue; }
      vistos[i2] = true;
      s.appendChild(svg("text", { x: L + i2 * ancho + ancho / 2, y: H - 10, "text-anchor": "middle", "class": "sv-texto sv-texto--suave" }, fechaCorta(serie[i2].dia)));
    }
    cont.appendChild(s);
    leyenda(porId("panel-diario-leyenda"), [
      { clase: "leyenda__muestra--alto", texto: "ALTO" }, { clase: "leyenda__muestra--medio", texto: "MEDIO" }, { clase: "leyenda__muestra--bajo", texto: "BAJO" }
    ]);
  }

  // ------------------------------------------------------------------ mapa de calor: día de la semana por hora

  function nivelCalor(v, maximo) { return v === 0 ? 0 : Math.min(4, Math.ceil(v * 4 / maximo)); }

  function dibujarCalor(c) {
    var cont = porId("panel-calor");
    vaciar(cont);
    var mq = window.matchMedia ? window.matchMedia("(max-width: 640px)") : null;
    var bloque = mq && mq.matches ? 2 : 1;           // en el celular, bloques de 2 horas: una celda de 1 hora no se toca con el dedo
    var cols = 24 / bloque;
    var celdas = c.matriz.map(function (fila) {
      var r = [];
      for (var k = 0; k < cols; k++) {
        var suma = 0;
        for (var j = 0; j < bloque; j++) { suma += fila[k * bloque + j]; }
        r.push(suma);
      }
      return r;
    });
    var maximo = 0;
    celdas.forEach(function (f) { f.forEach(function (v) { if (v > maximo) { maximo = v; } }); });
    var W = 720, L = 40, T = 24, R = 6, alto = 26, B = 8;
    var ancho = (W - L - R) / cols;
    var H = T + 7 * (alto + 2) + B;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, "class": "panel__grafico", role: "img",
      "aria-label": "Mapa de calor: incidentes por día de la semana y hora del día, hora de Lima" });
    s.appendChild(svg("title", {}, "Incidentes por día de la semana y hora del día (hora de Lima)"));
    function rotulo(k) { return bloque === 1 ? String(k) : (k * bloque) + "–" + (k * bloque + bloque - 1); }
    for (var k = 0; k < cols; k++) {
      if (bloque === 1 && k % 3 !== 0) { continue; }
      s.appendChild(svg("text", { x: L + k * ancho + (ancho - 2) / 2, y: 16, "text-anchor": "middle", "class": "sv-texto sv-texto--suave" }, rotulo(k)));
    }
    celdas.forEach(function (fila, d) {
      var y = T + d * (alto + 2);
      s.appendChild(svg("text", { x: 0, y: y + alto / 2 + 4, "class": "sv-texto" }, DIAS_CORTOS[d]));
      fila.forEach(function (v, k) {
        var x = L + k * ancho;
        var g = svg("g", {});
        g.appendChild(svg("rect", { x: x, y: y, width: ancho - 2, height: alto, rx: 3, "class": "sv-calor-" + nivelCalor(v, maximo) }));
        if (v > 0) { g.appendChild(svg("text", { x: x + (ancho - 2) / 2, y: y + alto / 2 + 4, "text-anchor": "middle", "class": "sv-texto" }, v)); }
        g.appendChild(svg("title", {}, c.dias[d] + " " + (bloque === 1 ? k + ":00 a " + k + ":59" : (k * bloque) + ":00 a " + (k * bloque + bloque - 1) + ":59") + ": " + plural(v, "incidente", "incidentes")));
        s.appendChild(g);
      });
    });
    cont.appendChild(s);

    var items = [{ clase: "leyenda__muestra--calor-0", texto: "0" }];
    for (var nv = 1; nv <= 4; nv++) {
      var lo = Math.floor((nv - 1) * maximo / 4) + 1, hi = Math.floor(nv * maximo / 4);
      if (maximo > 0 && lo <= hi) { items.push({ clase: "leyenda__muestra--calor-" + nv, texto: lo === hi ? String(lo) : lo + "–" + hi }); }
    }
    leyenda(porId("panel-calor-leyenda"), items);
    var nota = porId("panel-calor-nota");
    nota.className = "panel__nota";
    var texto = plural(c.total, "incidente", "incidentes") + " en el rango" + (c.incluye_falsas ? ", contando las falsas alarmas" : ", sin las falsas alarmas") +
      ". Son totales, no promedios: en 30 días cada día de la semana aparece 4 o 5 veces.";
    if (c.pocos_datos) {
      texto = "Son pocos datos (menos de 20 incidentes): un patrón aquí puede ser casualidad. " + texto;
      nota.className = "panel__nota panel__nota--aviso";
    }
    nota.textContent = texto;
  }

  // ------------------------------------------------------------------ ranking de cámaras

  function dibujarRanking(filas) {
    var cont = porId("panel-ranking");
    vaciar(cont);
    if (!filas.length) { cont.appendChild(nodo("p", "panel__nota", "No hay eventos en este rango.")); return; }
    var tabla = nodo("table", "panel__tabla");
    var cab = nodo("tr");
    ["Cámara", "Incidentes", "Falsas alarmas", "% de falsas", "Último evento"].forEach(function (t, i) {
      var th = nodo("th", i > 0 && i < 4 ? "num" : "", t);
      th.setAttribute("scope", "col");
      cab.appendChild(th);
    });
    var thead = nodo("thead"); thead.appendChild(cab); tabla.appendChild(thead);
    var cuerpo = nodo("tbody");
    filas.forEach(function (f) {
      var tr = nodo("tr");
      var td = nodo("td");
      td.appendChild(document.createTextNode(f.name + (f.archivada ? " (archivada)" : "")));
      td.appendChild(nodo("small", "", (f.kind === "common" ? "Zona común" : "Privada") + (f.place ? " · " + f.place : "")));
      tr.appendChild(td);
      var total = f.incidentes + f.falsas;
      tr.appendChild(nodo("td", "num", f.incidentes));
      tr.appendChild(nodo("td", "num", f.falsas));
      tr.appendChild(nodo("td", "num", (total ? Math.round(f.falsas * 100 / total) : 0) + " %"));
      tr.appendChild(nodo("td", "", f.ultimo ? fechaCorta(f.ultimo.slice(0, 10)) + " " + f.ultimo.slice(11, 16) : "—"));
      cuerpo.appendChild(tr);
    });
    tabla.appendChild(cuerpo);
    cont.appendChild(tabla);
  }

  // ------------------------------------------------------------------ señales más frecuentes

  function dibujarSenales(filas) {
    var cont = porId("panel-senales");
    vaciar(cont);
    if (!filas.length) {
      cont.appendChild(nodo("p", "panel__nota", "No hay señales registradas en este rango."));
      vaciar(porId("panel-senales-leyenda"));
      return;
    }
    var max = 0;
    filas.forEach(function (f) { if (f.incidentes + f.falsas > max) { max = f.incidentes + f.falsas; } });
    var W = 720, L = 240, R = 110, alto = 26, T = 6;
    var H = T + filas.length * alto + 6;
    var area = W - L - R;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, "class": "panel__grafico", role: "img", "aria-label": "Señales más frecuentes, divididas en incidentes y falsas alarmas" });
    s.appendChild(svg("title", {}, "Señales más frecuentes en el rango, divididas en incidentes y falsas alarmas"));
    filas.forEach(function (f, i) {
      var y = T + i * alto;
      var g = svg("g", {});
      g.appendChild(svg("text", { x: 6, y: y + 17, "class": "sv-texto" }, f.description));
      var wi = f.incidentes * area / max, wf = f.falsas * area / max;
      if (wi > 0) { g.appendChild(svg("rect", { x: L, y: y + 4, width: wi, height: alto - 10, "class": "sv-inc" })); }
      if (wf > 0) { g.appendChild(svg("rect", { x: L + wi, y: y + 4, width: wf, height: alto - 10, "class": "sv-fal" })); }
      g.appendChild(svg("text", { x: L + wi + wf + 8, y: y + 17, "class": "sv-texto sv-texto--suave" }, f.incidentes + (f.falsas ? " (" + f.falsas + " falsas)" : "")));
      g.appendChild(svg("title", {}, f.description + ": " + plural(f.incidentes, "incidente", "incidentes") + " y " + plural(f.falsas, "falsa alarma", "falsas alarmas")));
      s.appendChild(g);
    });
    cont.appendChild(s);
    leyenda(porId("panel-senales-leyenda"), [{ clase: "leyenda__muestra--inc", texto: "Incidentes" }, { clase: "leyenda__muestra--fal", texto: "Falsas alarmas" }]);
  }

  // ------------------------------------------------------------------ equipos

  function dibujarEquipos(equipos) {
    var cont = porId("panel-equipos");
    vaciar(cont);
    if (!equipos.length) { cont.appendChild(nodo("p", "panel__nota", "Todavía no hay mini PC registradas.")); return; }
    equipos.forEach(function (e) {
      var caja = nodo("article", "panel__equipo");
      var izq = nodo("div");
      var nombre = nodo("p", "panel__equipo-nombre", e.name);
      var sello = nodo("span", "panel__sello");
      if (e.state === "online") { sello.textContent = "En línea"; sello.className += " panel__sello--ok"; }
      else if (e.state === "offline") { sello.textContent = "Sin señal hace " + hace(e.seconds_since); sello.className += " panel__sello--mal"; }
      else if (e.state === "revoked") { sello.textContent = "Revocado"; sello.className += " panel__sello--mal"; }
      else { sello.textContent = "Aún no da señal"; }
      nombre.appendChild(sello);
      izq.appendChild(nombre);
      izq.appendChild(nodo("p", "panel__equipo-datos", "Versión " + (e.version || "desconocida")));
      caja.appendChild(izq);
      var der = nodo("div");
      der.appendChild(nodo("p", "panel__equipo-datos", "Cámaras en línea: " + e.camaras_en_linea + " de " + e.camaras));
      der.appendChild(nodo("p", "panel__equipo-datos", e.sin_asignar === 0 ? "Ninguna cámara sin asignar" : plural(e.sin_asignar, "cámara sin asignar", "cámaras sin asignar")));
      caja.appendChild(der);
      cont.appendChild(caja);
    });
  }

  // ------------------------------------------------------------------ filtros y carga

  function llenarFiltros(d) {
    porId("f-desde").value = d.rango.desde;
    porId("f-hasta").value = d.rango.hasta;
    var sel = porId("f-camara");
    var actual = d.camara || "";
    vaciar(sel);
    var todas = nodo("option", "", "Todas las cámaras");
    todas.value = "";
    sel.appendChild(todas);
    d.opciones_camara.forEach(function (c) {
      var o = nodo("option", "", c.name + (c.place ? " · " + c.place : "") + (c.archivada ? " (archivada)" : ""));
      o.value = c.id;
      sel.appendChild(o);
    });
    sel.value = actual;
    porId("panel-rango").textContent = fechaLarga(d.rango.desde) + " a " + fechaLarga(d.rango.hasta) + " · " + plural(d.rango.dias, "día", "días");
    var q = ["desde=" + encodeURIComponent(d.rango.desde), "hasta=" + encodeURIComponent(d.rango.hasta)];
    if (d.camara) { q.push("camara=" + encodeURIComponent(d.camara)); }
    porId("panel-exportar").setAttribute("href", "/admin/panel/exportar.csv?" + q.join("&"));
    var nota = porId("panel-exportar-nota");
    var ex = d.exportacion;
    nota.textContent = ex.filas === 0 ? "No hay eventos para exportar en este rango."
      : (ex.recortada ? "El rango tiene " + miles(ex.filas) + " eventos; el CSV incluirá los " + miles(ex.limite) + " más recientes."
        : "El CSV tendrá " + plural(ex.filas, "fila", "filas") + " (todas las cámaras que puedes ver, con su estado).");
  }

  function render(d) {
    ultimo = d;
    llenarFiltros(d);
    dibujarTarjetas(d);
    dibujarDiario(d.serie_diaria);
    dibujarCalor(d.calor);
    dibujarRanking(d.ranking);
    dibujarSenales(d.senales);
    dibujarEquipos(d.equipos);
    estado("Actualizado a las " + String(d.generado_en).slice(11, 16) + " (hora de Lima).");
  }

  function consulta() {
    var q = [];
    var desde = porId("f-desde").value, hasta = porId("f-hasta").value, camara = porId("f-camara").value;
    if (desde) { q.push("desde=" + encodeURIComponent(desde)); }
    if (hasta) { q.push("hasta=" + encodeURIComponent(hasta)); }
    if (camara) { q.push("camara=" + encodeURIComponent(camara)); }
    if (porId("f-falsas").checked) { q.push("falsas=true"); }
    return q.join("&");
  }

  function cargar() {
    var mia = ++secuencia;
    if (pedido && pedido.abort) { pedido.abort(); }
    pedido = typeof AbortController === "function" ? new AbortController() : null;
    estado("Cargando…");
    var q = consulta();
    var opciones = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (pedido) { opciones.signal = pedido.signal; }
    fetch("/admin/panel/datos" + (q ? "?" + q : ""), opciones)
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; }); })
      .then(function (x) {
        if (mia !== secuencia) { return; }
        if (x.r.status === 401) { window.location.href = "/login"; return; }
        if (x.r.status === 403) { estado("Esta pantalla es solo para administradores.", true); return; }
        if (!x.r.ok) { estado(x.d && x.d.error ? x.d.error : "No se pudo cargar el panel.", true); return; }
        render(x.d);
      })
      .catch(function (e) {
        if (mia !== secuencia || (e && e.name === "AbortError")) { return; }
        estado("No se pudo conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.", true);
      });
  }

  porId("panel-filtros").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var desde = porId("f-desde").value, hasta = porId("f-hasta").value;
    if (desde && hasta && desde > hasta) { estado("La fecha inicial no puede ser posterior a la final.", true); return; }
    cargar();
  });
  porId("panel-ultimos").addEventListener("click", function () {
    porId("f-desde").value = "";
    porId("f-hasta").value = "";
    cargar();
  });
  porId("f-camara").addEventListener("change", cargar);
  porId("f-falsas").addEventListener("change", cargar);

  var mq = window.matchMedia ? window.matchMedia("(max-width: 640px)") : null;
  function girar() { if (ultimo) { dibujarCalor(ultimo.calor); } }
  if (mq) {
    if (mq.addEventListener) { mq.addEventListener("change", girar); } else if (mq.addListener) { mq.addListener(girar); }
  }

  cargar();
})();
