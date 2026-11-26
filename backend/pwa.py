"""
PWA (H7): lo que permite instalar INTIVISION en el celular como una aplicación.

  • manifest.webmanifest: nombre, colores, íconos y que abra a pantalla completa.
  • Íconos: se dibujan aquí mismo (no hay archivos binarios en el repositorio).
  • Service worker: SOLO sirve la página «sin conexión» cuando no hay red al abrir una página. NO guarda cámaras, fotos, sesiones
    ni páginas: todo lo demás va directo a la red. (La imagen de las cámaras es privada: no debe quedar en el teléfono.)
"""
from __future__ import annotations

import functools
import os

import cv2
import numpy as np

FONDO = "#0b1220"
ACENTO_BGR = (191, 212, 45)           # el verde azulado del panel, en BGR
FONDO_BGR = (32, 18, 11)

ICONOS = {"icon-192.png": (192, False), "icon-512.png": (512, False), "icon-maskable-512.png": (512, True), "apple-touch-icon.png": (180, False)}


def manifest() -> dict:
    return {
        "name": "INTIVISION", "short_name": "INTIVISION", "description": "Vigilancia vecinal con cámaras", "lang": "es",
        "start_url": "/cameras", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": FONDO, "theme_color": FONDO,
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


@functools.lru_cache(maxsize=8)
def icon_png(size: int, maskable: bool = False) -> bytes:
    """Un escudo con una V. En la versión «maskable» el dibujo queda más chico, dentro de la zona que el sistema nunca recorta."""
    img = np.full((size, size, 3), FONDO_BGR, np.uint8)
    m = 0.22 if maskable else 0.14
    s = float(size)
    puntos = np.array([[0.5 * s, m * s], [(1 - m) * s, (m + 0.10) * s], [(1 - m) * s, 0.52 * s], [0.5 * s, (1 - m) * s],
                       [m * s, 0.52 * s], [m * s, (m + 0.10) * s]], np.int32)
    cv2.fillPoly(img, [puntos], ACENTO_BGR)
    escala = size / 170.0
    (ancho, alto), base = cv2.getTextSize("V", cv2.FONT_HERSHEY_DUPLEX, escala, max(2, int(escala * 2.2)))
    cv2.putText(img, "V", (int((s - ancho) / 2), int(0.5 * s + alto / 2)), cv2.FONT_HERSHEY_DUPLEX, escala, FONDO_BGR, max(2, int(escala * 2.2)), cv2.LINE_AA)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def icono(nombre: str):
    """Los bytes del PNG de ese nombre, o None si no existe."""
    if nombre not in ICONOS:
        return None
    tam, mascara = ICONOS[nombre]
    return icon_png(tam, mascara)


SERVICE_WORKER = """\
// INTIVISION: service worker mínimo. Solo guarda la página «sin conexión» y el ícono. Nada de cámaras, fotos ni sesiones.
const CACHE = 'vigia-sin-conexion-v1';
const BASICOS = ['/offline.html', '/icons/icon-192.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(BASICOS); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (claves) {
    return Promise.all(claves.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  // Solo al abrir una página sin red se muestra «sin conexión». Las respuestas de la red NUNCA se guardan.
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(function () { return caches.match('/offline.html'); }));
  }
  // Todo lo demás (imágenes de cámaras, fotos, WebSocket, formularios): directo a la red, sin intervenir.
});
"""

OFFLINE_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b1220"><title>Sin conexión · INTIVISION</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e6edf7;font-family:system-ui,sans-serif;text-align:center;padding:24px}
h1{font-size:1.6rem;margin:0 0 8px}p{margin:0;color:#9fb0c8}</style></head>
<body><main><h1>Sin conexión</h1><p>INTIVISION necesita internet para mostrar tus cámaras. Vuelve a intentarlo cuando tengas señal.</p></main></body></html>
"""

HEAD_TAGS = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<meta name="theme-color" content="#0b1220">'
    '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="INTIVISION">'
)


SESION_JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "sesion.js"), encoding="utf-8").read()


MENU_CSS = """<style id="menu-usuario-css">
.menu-usuario { position: relative; }
.menu-usuario > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 0 2px; }
.menu-usuario > summary::-webkit-details-marker { display: none; }
.menu-usuario > summary:focus-visible { outline: 2px solid hsl(var(--ring)); outline-offset: 2px; }
.menu-usuario__avatar { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid hsl(var(--foreground)); font-size: 13px; font-weight: 700; flex: none; }
.menu-usuario__texto { display: flex; flex-direction: column; min-width: 0; text-align: left; line-height: 1.25; }
.menu-usuario__nombre { max-width: 24ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 700; }
.menu-usuario__rol { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: hsl(var(--muted-foreground)); }
.menu-usuario__flecha { font-size: 12px; transition: transform .15s; }
.menu-usuario[open] .menu-usuario__flecha { transform: rotate(180deg); }
.solo-lectores { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.menu-usuario__panel { position: absolute; right: 0; top: calc(100% + 6px); z-index: 60; min-width: 250px; display: grid; padding: 6px;
  background: hsl(var(--card)); color: hsl(var(--card-foreground)); border: 1px solid hsl(var(--foreground)); box-shadow: 4px 4px 0 0 hsl(var(--foreground)); }
.menu-usuario__cabecera { display: none; padding: 10px 12px; border-bottom: 1px solid hsl(var(--border)); }
.menu-usuario__cabecera span { display: block; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: hsl(var(--muted-foreground)); }
.menu-usuario__panel a, .menu-usuario__salir { display: flex; align-items: center; width: 100%; min-height: 44px; padding: 0 12px; background: none; border: 0;
  border-left: 3px solid transparent; color: inherit; font: inherit; font-size: 13px; text-align: left; text-decoration: none; cursor: pointer; }
.menu-usuario__panel a:hover, .menu-usuario__salir:hover { background: hsl(var(--muted)); }
.menu-usuario__panel a[aria-current="page"] { border-left-color: hsl(var(--accent)); font-weight: 700; }
.menu-usuario__panel form { margin: 0; border-top: 1px solid hsl(var(--border)); margin-top: 4px; padding-top: 4px; }
@media (max-width: 560px) { .menu-usuario__texto, .menu-usuario__flecha { display: none; } .menu-usuario__cabecera { display: block; } .menu-usuario__panel { min-width: 220px; } }
</style>"""
def _con_menu(pagina: str) -> str:
    """Los estilos del menú de usuario, en <head> y una sola vez por página (su script va dentro del script de sesión)."""
    if "menu-usuario-css" not in pagina and "</head>" in pagina:
        pagina = pagina.replace("</head>", MENU_CSS + "</head>", 1)
    return pagina


def _script_sesion(nonce: str) -> str:
    return '<script nonce="' + nonce + '">' + SESION_JS + "</script>"


def inject_sesion(pagina: str, nonce: str) -> str:
    """Le agrega a una página con sesión el aviso de actividad de la persona (ver templates/sesion.js)."""
    pagina = _con_menu(pagina)
    script = _script_sesion(nonce)
    i = pagina.rfind("</body>")
    return pagina[:i] + script + pagina[i:] if i != -1 else pagina + script


def inject(pagina: str, nonce: str) -> str:
    """Le agrega a una página las etiquetas de la PWA, el registro del service worker y el aviso de actividad (con la clave de un solo uso de la página)."""
    registro = ('<script nonce="' + nonce + '">if ("serviceWorker" in navigator) { window.addEventListener("load", function () '
                '{ navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {}); }); }</script>') + _script_sesion(nonce)
    if "</head>" in pagina:
        pagina = pagina.replace("</head>", HEAD_TAGS + "</head>", 1)
    pagina = _con_menu(pagina)
    i = pagina.rfind("</body>")
    return pagina[:i] + registro + pagina[i:] if i != -1 else pagina + registro
