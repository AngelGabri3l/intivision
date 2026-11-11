"""
Cliente HTTP del servidor (VPS): manda eventos con foto, el inventario y pide la configuración.

Qué hace con cada respuesta (lo decide el enviador, que es quien reintenta):
    200                          todo bien
    401 o 403                    NoAutorizado: el token es inválido o fue revocado. Los eventos NO se pierden.
    timeout, sin red, 5xx, 429   ErrorTransitorio: se reintenta más tarde
    cualquier otro 4xx           Rechazado: el servidor dijo que ese evento no sirve; reintentarlo no cambia nada

El token viaja solo en la cabecera Authorization y NUNCA aparece en un mensaje ni en un error de este módulo.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from . import __version__

log = logging.getLogger("agent.cliente")

TIMEOUT = (5, 20)               # (conectar, leer) en segundos
TIMEOUT_FOTO = (5, 40)          # una foto sobre una conexión lenta tarda más
CODIGOS_TRANSITORIOS = {408, 425, 429}


class ErrorTransitorio(Exception):
    """No se pudo hablar con el servidor ahora (o falló él): se reintenta."""


class NoAutorizado(Exception):
    """El servidor no reconoce el token (inválido o revocado)."""


class Rechazado(Exception):
    """El servidor rechazó la petición y repetirla no ayuda. `status` es el código HTTP."""

    def __init__(self, status: int, mensaje: str):
        super().__init__(f"{status}: {mensaje}")
        self.status, self.mensaje = status, mensaje


@dataclass
class Resultado:
    duplicate: bool
    event_id: int | None


def redactar(texto, token: str | None) -> str:
    """Quita el token de un texto (por si una librería lo incluyera en un mensaje de error)."""
    texto = str(texto)
    return texto.replace(token, "***") if token else texto


class ClienteServidor:
    def __init__(self, base_url: str, token: str, sesion: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._s = sesion or requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": f"vigia-agent/{__version__}"})

    # --- lo que usa el resto del agente ---
    def enviar_evento(self, payload: dict, foto_ruta: str | None) -> Resultado:
        """Manda un evento (y su foto, si hay) a /ingest."""
        archivos = {"evento": (None, json.dumps(payload, ensure_ascii=False))}
        foto = None
        try:
            if foto_ruta:
                try:
                    foto = open(foto_ruta, "rb")
                except OSError as e:
                    log.warning("no pude abrir la foto de un evento (%s): se envía sin foto", e.__class__.__name__)
                else:
                    archivos["foto"] = ("evidencia.jpg", foto, "image/jpeg")
            r = self._pedir("POST", "/ingest", files=archivos, timeout=TIMEOUT_FOTO if foto else TIMEOUT)
        finally:
            if foto:
                foto.close()
        cuerpo = self._json(r)
        return Resultado(bool(cuerpo.get("duplicate")), cuerpo.get("event_id"))

    def enviar_inventario(self, instantanea: dict) -> dict:
        """Manda el inventario de cámaras (también sirve de latido)."""
        return self._json(self._pedir("POST", "/edge/inventory", json=instantanea))

    def obtener_config(self) -> dict:
        """Las cámaras que el administrador asignó a esta mini PC, el horario sensible y las zonas."""
        return self._json(self._pedir("GET", "/edge/config"))

    # --- por dentro ---
    def _pedir(self, metodo: str, ruta: str, timeout=TIMEOUT, **kw) -> requests.Response:
        try:
            r = self._s.request(metodo, self.base_url + ruta, timeout=timeout, allow_redirects=False, **kw)
        except requests.exceptions.SSLError as e:
            raise ErrorTransitorio(f"no se pudo verificar el certificado del servidor ({redactar(e.__class__.__name__, self._token)})") from None
        except requests.exceptions.Timeout:
            raise ErrorTransitorio("el servidor tardó demasiado en responder") from None
        except requests.exceptions.RequestException as e:
            raise ErrorTransitorio(f"sin conexión con el servidor ({e.__class__.__name__})") from None
        if 300 <= r.status_code < 400:
            # una redirección (por ejemplo de http a https) haría que el token se reenviara a otra dirección
            raise Rechazado(r.status_code, "el servidor redirige a otra dirección: corrige SERVER_URL")
        if r.status_code in (401, 403):
            raise NoAutorizado(f"el servidor no acepta el token ({r.status_code})")
        if r.status_code >= 500 or r.status_code in CODIGOS_TRANSITORIOS:
            raise ErrorTransitorio(f"el servidor respondió {r.status_code}")
        if r.status_code >= 400:
            raise Rechazado(r.status_code, self._mensaje(r))
        return r

    def _mensaje(self, r: requests.Response) -> str:
        try:
            m = r.json().get("error") or r.json().get("detail")
        except (ValueError, AttributeError):
            m = None
        return redactar((m if isinstance(m, str) else r.text)[:200].strip() or "sin detalle", self._token)

    def _json(self, r: requests.Response) -> dict:
        try:
            cuerpo = r.json()
        except ValueError:
            raise ErrorTransitorio("el servidor respondió algo que no es JSON") from None
        if not isinstance(cuerpo, dict):
            raise ErrorTransitorio("el servidor respondió algo inesperado")
        return cuerpo
