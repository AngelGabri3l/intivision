// Le avisa al servidor que la persona sigue ahí: se manda un aviso cuando hace algo (clic, tecla, toque, rueda o desplazamiento).
// Es lo que mantiene abierta la sesión mientras trabaja. Las consultas que una pantalla hace sola cada pocos segundos NO cuentan: una
// pestaña olvidada abierta no mantiene viva la sesión, y por eso este script no usa temporizadores, solo eventos de la persona.
(function () {
  "use strict";
  var CADA_MS = 120000;                              // como mucho un aviso cada 2 minutos
  var REINTENTO_MS = 15000;                          // si un aviso falla, el siguiente espera al menos 15 s: no se martilla a un servidor caído
  var EVENTOS = ["pointerdown", "keydown", "touchstart", "wheel", "scroll"];
  var ultimo = Date.now();                           // cargar la página ya cuenta: el servidor la renovó al entregarla
  var enviando = false;

  function avisar() {
    if (enviando || Date.now() - ultimo < CADA_MS || typeof fetch !== "function") { return; }
    enviando = true;
    ultimo = Date.now();
    fetch("/session/ping", { method: "POST", credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (r) {
        enviando = false;
        if (r.status === 401) { window.location.assign("/"); return; }        // la sesión ya terminó: el servidor explica por qué
        if (!r.ok) { ultimo = Date.now() - CADA_MS + REINTENTO_MS; }        // no llegó bien: se reintenta con la actividad, pero sin prisa
      })
      .catch(function () { enviando = false; ultimo = Date.now() - CADA_MS + REINTENTO_MS; });
  }

  EVENTOS.forEach(function (nombre) { document.addEventListener(nombre, avisar, { capture: true, passive: true }); });
})();

// Menú de usuario de la cabecera (el elemento details con la clase menu-usuario): se cierra con Esc o al tocar fuera. Sin ese menú en la página, no hace nada.
(function () {
  var menu = document.getElementsByClassName("menu-usuario")[0];
  if (!menu) { return; }
  document.addEventListener("click", function (e) { if (menu.open && !menu.contains(e.target)) { menu.open = false; } });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && menu.open) { menu.open = false; var boton = menu.querySelector("summary"); if (boton) { boton.focus(); } }
  });
})();
