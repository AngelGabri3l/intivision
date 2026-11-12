(function () {
  "use strict";
  var ID = "{camera_id}";
  var img = document.getElementById("vivo-img");
  var estado = document.getElementById("vivo-estado");
  var boton = document.getElementById("vivo-boton");
  if (!img || !estado || !boton) { return; }
  var ws = null, urlActual = null, intentos = 0, manual = false, final = false, pausado = false;
  var respaldo = null, timer = null, enVivo = false, motivoFinal = "";

  function texto(t) { estado.textContent = t; }
  function hace(iso) {
    if (!iso) { return ""; }
    var seg = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (isNaN(seg)) { return ""; }
    if (seg < 90) { return " desde hace " + seg + " s"; }
    var min = Math.round(seg / 60);
    return min < 120 ? " desde hace " + min + " min" : " desde hace " + Math.round(min / 60) + " h";
  }
  function mostrar(blob) {
    var nueva = URL.createObjectURL(blob), vieja = urlActual;
    urlActual = nueva;
    img.src = nueva;
    if (vieja) { URL.revokeObjectURL(vieja); }
  }
  function ultimaFoto() { img.src = "/cameras/" + ID + "/ultima.jpg?t=" + Date.now(); }
  function iniciarRespaldo() {
    if (respaldo) { return; }
    texto("Modo de respaldo: se actualiza la foto cada 2 segundos.");
    ultimaFoto();
    respaldo = setInterval(ultimaFoto, 2000);
  }
  function detenerRespaldo() { if (respaldo) { clearInterval(respaldo); respaldo = null; } }
  function cerrar() {
    clearTimeout(timer);
    if (ws) { var s = ws; ws = null; try { s.close(1000); } catch (e) { /* ya estaba cerrada */ } }
  }

  function conectar() {
    if (ws || final) { return; }
    pausado = false;
    texto("Conectando…");
    var s = new WebSocket((location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws/camera/" + ID);
    ws = s;
    s.binaryType = "blob";
    s.onmessage = function (ev) {
      if (typeof ev.data !== "string") {
        detenerRespaldo();
        mostrar(ev.data);
        if (!enVivo) { enVivo = true; intentos = 0; texto("En vivo"); }
        return;
      }
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "en_vivo") { enVivo = true; intentos = 0; detenerRespaldo(); texto("En vivo"); }
      else if (m.type === "esperando") { enVivo = false; texto("Pidiendo imagen a la cámara…"); }
      else if (m.type === "sin_senal") {
        enVivo = false;
        texto((m.motivo === "demasiados_flujos" ? "La mini PC ya está enviando el máximo de cámaras a la vez." : "Sin señal" + hace(m.desde) + ".") + " Se muestra la última imagen.");
        ultimaFoto();
      }
      else if (m.type === "limite") {
        motivoFinal = m.motivo === "demasiadas_conexiones" ? "Abriste esta cámara demasiadas veces seguidas. Espera un minuto." : "Ya hay " + m.max + " personas mirando esta cámara. Intenta más tarde.";
      }
      else if (m.type === "fin") { motivoFinal = "Se alcanzó el tiempo máximo de visita (" + Math.round(m.segundos / 60) + " min). Pulsa «Ver en vivo» para seguir."; }
    };
    s.onclose = function (ev) {
      if (ws === s) { ws = null; }
      enVivo = false;
      if (manual || pausado) { return; }
      if (ev.code === 4401) { final = true; texto("Tu sesión venció. Vuelve a iniciar sesión."); return; }
      if (ev.code === 4403 || ev.code === 4404) { final = true; texto("Esta cámara no está disponible."); return; }
      if (ev.code === 4429 || ev.code === 4408) {
        final = true;
        texto(motivoFinal || "No se puede mostrar la cámara ahora.");
        boton.textContent = "Ver en vivo";
        return;
      }
      intentos += 1;
      if (intentos >= 3) { iniciarRespaldo(); }
      timer = setTimeout(conectar, Math.min(15000, 1000 * Math.pow(2, Math.min(intentos, 4))));
    };
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      pausado = true; cerrar(); detenerRespaldo();
      if (!final && !manual) { texto("En pausa: la pestaña está oculta."); }
    } else if (!manual && !final) { conectar(); }
  });
  window.addEventListener("pagehide", function () { pausado = true; cerrar(); detenerRespaldo(); });
  boton.addEventListener("click", function () {
    if (ws || respaldo) {
      manual = true; cerrar(); detenerRespaldo(); texto("Detenido.");
      boton.textContent = "Ver en vivo";
    } else {
      manual = false; final = false; intentos = 0; motivoFinal = "";
      boton.textContent = "Detener";
      conectar();
    }
  });
  conectar();
})();
