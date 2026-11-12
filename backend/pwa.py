"""
PWA (H7): lo que permite instalar VIGÍA en el celular como una aplicación.

  • manifest.webmanifest: nombre, colores, íconos y que abra a pantalla completa.
  • Íconos: se dibujan aquí mismo (no hay archivos binarios en el repositorio).
  • Service worker: SOLO sirve la página «sin conexión» cuando no hay red al abrir una página. NO guarda cámaras, fotos, sesiones
    ni páginas: todo lo demás va directo a la red. (La imagen de las cámaras es privada: no debe quedar en el teléfono.)
"""
from __future__ import annotations

import functools

import cv2
import numpy as np

FONDO = "#0b1220"
ACENTO_BGR = (191, 212, 45)           # el verde azulado del panel, en BGR
FONDO_BGR = (32, 18, 11)

ICONOS = {"icon-192.png": (192, False), "icon-512.png": (512, False), "icon-maskable-512.png": (512, True), "apple-touch-icon.png": (180, False)}


def manifest() -> dict:
    return {
        "name": "VIGÍA", "short_name": "VIGÍA", "description": "Vigilancia vecinal con cámaras", "lang": "es",
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
// VIGÍA: service worker mínimo. Solo guarda la página «sin conexión» y el ícono. Nada de cámaras, fotos ni sesiones.
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
<meta name="theme-color" content="#0b1220"><title>Sin conexión · VIGÍA</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e6edf7;font-family:system-ui,sans-serif;text-align:center;padding:24px}
h1{font-size:1.6rem;margin:0 0 8px}p{margin:0;color:#9fb0c8}</style></head>
<body><main><h1>Sin conexión</h1><p>VIGÍA necesita internet para mostrar tus cámaras. Vuelve a intentarlo cuando tengas señal.</p></main></body></html>
"""

HEAD_TAGS = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<meta name="theme-color" content="#0b1220">'
    '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="VIGÍA">'
)


def inject(pagina: str, nonce: str) -> str:
    """Le agrega a una página las etiquetas de la PWA y el registro del service worker (con la clave de un solo uso de la página)."""
    registro = ('<script nonce="' + nonce + '">if ("serviceWorker" in navigator) { window.addEventListener("load", function () '
                '{ navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {}); }); }</script>')
    if "</head>" in pagina:
        pagina = pagina.replace("</head>", HEAD_TAGS + "</head>", 1)
    i = pagina.rfind("</body>")
    return pagina[:i] + registro + pagina[i:] if i != -1 else pagina + registro
