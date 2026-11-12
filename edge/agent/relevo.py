"""
Relevo: la imagen en vivo desde la mini PC (H7).

El agente abre y MANTIENE una conexión hacia el servidor (wss://.../edge/ws). Es una conexión saliente, así que en la cuadra no hay
que abrir ningún puerto. Por ahí el servidor le dice:

    {"cmd": "stream", "camera_id": "<uuid>", "fps": 5}     alguien abrió esa cámara: manda imagen
    {"cmd": "stop",   "camera_id": "<uuid>"}                ya no hay nadie mirando: deja de mandar

y el agente responde con mensajes binarios: los 16 bytes del identificador de la cámara y a continuación un JPEG de ~640 px.
Cuando nadie mira, NO se envía nada (solo un latido de la conexión cada 20 s).

Reglas de seguridad:
  • Solo se envían cámaras ASIGNADAS (las de la configuración que dio el servidor). Pedir otra se rechaza.
  • Se verifica el certificado del servidor (no hay forma de desactivarlo) y no se sigue ninguna redirección.
  • Máximo de flujos a la vez, y de 1 a 8 cuadros por segundo, pida lo que pida el servidor.
  • El token solo viaja en la cabecera Authorization, nunca en la dirección.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid

import certifi
import cv2
import websocket

from . import __version__

log = logging.getLogger("agent.relevo")

FPS_MIN, FPS_MAX, FPS_DEFECTO = 1, 8, 5
MAX_BYTES = 200_000
EDAD_MAX_CUADRO_S = 2.0              # una cámara sin cuadros nuevos hace más que esto se declara sin señal
PING_CADA_S = 20.0                   # mantiene viva la conexión a través del router
ESTADISTICAS_CADA_S = 30.0
AVISO_CADA_S = 300
CIERRE_SIN_SESION = 4401


class Cerrada(Exception):
    """El servidor cerró la conexión. `codigo` dice por qué (4401: no acepta el token)."""

    def __init__(self, codigo):
        super().__init__(f"cerrada por el servidor (código {codigo})")
        self.codigo = codigo


def url_relevo(base_url: str) -> str:
    """https://x  ->  wss://x/edge/ws ; http://localhost:8000  ->  ws://localhost:8000/edge/ws (la dirección ya viene validada)."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://"):].rstrip("/") + "/edge/ws"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://"):].rstrip("/") + "/edge/ws"
    raise ValueError("la dirección del servidor debe empezar con https:// o http://")


def conectar_ws(url: str, token: str, timeout: float = 10.0):
    """Abre la conexión. El certificado se verifica con las autoridades públicas y las redirecciones NO se siguen."""
    return websocket.create_connection(
        url, header=[f"Authorization: Bearer {token}", f"User-Agent: vigia-agent/{__version__}"], timeout=timeout,
        sslopt={"ca_certs": certifi.where()}, suppress_origin=True, redirect_limit=0)


def codificar_vivo(cuadro, ancho: int = 640, calidad: int = 70, max_bytes: int = MAX_BYTES):
    """Un JPEG del cuadro, del ancho pedido y de hasta `max_bytes` (baja la calidad si hace falta). None si no cabe."""
    alto0, ancho0 = cuadro.shape[:2]
    if ancho0 != ancho:
        cuadro = cv2.resize(cuadro, (ancho, max(16, round(alto0 * ancho / ancho0))),
                            interpolation=cv2.INTER_AREA if ancho0 > ancho else cv2.INTER_LINEAR)
    for q in (calidad, calidad - 15, calidad - 30, 35):
        ok, buf = cv2.imencode(".jpg", cuadro, [cv2.IMWRITE_JPEG_QUALITY, max(20, q)])
        if ok and len(buf) <= max_bytes:
            return buf.tobytes()
    return None


def probar_relevo(base_url: str, token: str):
    """(ok, mensaje): abre la conexión, espera el «hola» del servidor y la cierra. Para diagnosticar redes y tokens."""
    try:
        ws = conectar_ws(url_relevo(base_url), token)
    except websocket.WebSocketBadStatusException as e:
        return False, f"el servidor rechazó la conexión (HTTP {e.status_code})"
    except Exception as e:      # noqa: BLE001
        return False, f"no se pudo abrir la conexión ({e.__class__.__name__})"
    try:
        ws.settimeout(5)
        opcode, datos = ws.recv_data()
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            codigo = int.from_bytes(datos[:2], "big") if len(datos) >= 2 else None
            return False, "el servidor no acepta el token" if codigo == CIERRE_SIN_SESION else f"el servidor cerró la conexión (código {codigo})"
        if opcode == websocket.ABNF.OPCODE_TEXT and json.loads(datos.decode("utf-8")).get("type") == "hola":
            return True, "conectado: el servidor respondió «hola»"
        return False, "el servidor respondió algo inesperado"
    except Exception as e:      # noqa: BLE001
        return False, f"la conexión se abrió, pero no hubo respuesta ({e.__class__.__name__})"
    finally:
        try:
            ws.close()
        except Exception:       # noqa: BLE001
            pass


class Relevo(threading.Thread):
    def __init__(self, base_url: str, token: str, gestor, config, ancho: int = 640, calidad: int = 70, max_flujos: int = 4,
                 conectar=None, reloj=time.monotonic, azar=None, base_s: float = 1.0, tope_s: float = 60.0):
        super().__init__(name="relevo", daemon=True)
        self.url, self._token = url_relevo(base_url), token
        self.gestor, self.config = gestor, config
        self.ancho, self.calidad, self.max_flujos = ancho, calidad, max_flujos
        self._conectar = conectar or (lambda: conectar_ws(self.url, self._token))
        self._reloj, self._azar = reloj, azar or random.Random()
        self.base_s, self.tope_s = base_s, tope_s
        self._parar = threading.Event()
        self._ws = None
        self.streams: dict[str, dict] = {}       # camera_id -> estado de ese flujo
        self.conectado = False
        self.sesiones = 0
        self.ultimo_error: str | None = None
        self._avisos: dict[str, float] = {}

    def detener(self) -> None:
        self._parar.set()

    def estado(self) -> dict:
        return {"conectado": self.conectado, "flujos": len(self.streams), "sesiones": self.sesiones, "ultimo_error": self.ultimo_error,
                "enviados": sum(s["enviados"] for s in self.streams.values())}

    # --- conexión, con reintentos
    def run(self) -> None:
        fallos = 0
        while not self._parar.is_set():
            try:
                ws = self._conectar()
            except Exception as e:      # noqa: BLE001
                fallos += 1
                self._fallo(e, fallos)
                continue
            fallos = 0
            self.sesiones += 1
            self.conectado = True
            log.info("conectado al relevo del servidor: se enviará imagen en vivo cuando alguien la pida")
            error = None
            try:
                self._sesion(ws)
            except Exception as e:      # noqa: BLE001 - el servidor cerró la conexión o se cortó la red
                error = e
            finally:
                self.conectado = False           # se dice YA, no después de la espera de reintento
                self._ws = None
                self.streams.clear()
                try:
                    ws.close()
                except Exception:       # noqa: BLE001
                    pass
            if error is not None:
                fallos = 1
                self._fallo(error, 1)
    def espera_para(self, fallos: int) -> float:
        base = min(self.tope_s, self.base_s * (2 ** max(0, fallos - 1)))
        return base * (1 + self._azar.uniform(-0.2, 0.2))

    def _fallo(self, e, fallos: int) -> None:
        token_malo = (isinstance(e, Cerrada) and e.codigo == CIERRE_SIN_SESION) or (
            isinstance(e, websocket.WebSocketBadStatusException) and e.status_code in (401, 403))
        self.ultimo_error = "token no aceptado" if token_malo else e.__class__.__name__
        if token_malo:
            self._decir("token", logging.WARNING, "el servidor no acepta el token de esta mini PC (¿se revocó?): no hay imagen en vivo")
            espera = self.tope_s
        else:
            self._decir("red", logging.INFO, "sin conexión con el relevo del servidor (%s); se reintenta", e.__class__.__name__)
            espera = self.espera_para(fallos)
        self._parar.wait(espera)

    def _decir(self, clave: str, nivel: int, mensaje: str, *args) -> None:
        ahora = time.monotonic()
        if ahora - self._avisos.get(clave, -AVISO_CADA_S - 1) >= AVISO_CADA_S:
            self._avisos[clave] = ahora
            log.log(nivel, mensaje, *args)

    # --- una conexión abierta
    def _sesion(self, ws) -> None:
        self._ws = ws
        ws.settimeout(0.25)
        ultimo_ping, ultimas_estadisticas = self._reloj(), self._reloj()
        while not self._parar.is_set():
            ws.settimeout(self._espera())        # no más de lo que falta para el siguiente cuadro: así se respeta el ritmo pedido
            self._recibir(ws)
            self._enviar_pendientes(ws)
            ahora = self._reloj()
            if ahora - ultimo_ping >= PING_CADA_S:
                ws.ping()
                ultimo_ping = ahora
            if ahora - ultimas_estadisticas >= ESTADISTICAS_CADA_S:
                ultimas_estadisticas = ahora
                self._estadisticas(ESTADISTICAS_CADA_S)

    def _espera(self) -> float:
        """Cuánto esperar un mensaje del servidor: hasta el siguiente cuadro que toca enviar, y nunca más de 0,25 s."""
        if not self.streams:
            return 0.25
        return max(0.005, min(0.25, min(s["proximo"] for s in self.streams.values()) - self._reloj()))

    def _recibir(self, ws) -> None:
        try:
            opcode, datos = ws.recv_data()
        except websocket.WebSocketTimeoutException:
            return
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            raise Cerrada(int.from_bytes(datos[:2], "big") if len(datos) >= 2 else None)
        if opcode == websocket.ABNF.OPCODE_TEXT:
            self._comando(datos.decode("utf-8", "replace"))

    def _responder(self, mensaje: dict) -> None:
        if self._ws is not None:
            self._ws.send(json.dumps(mensaje))

    def _device_key(self, camera_id):
        return next((c["device_key"] for c in self.config.camaras() if c["camera_id"] == camera_id), None)

    def _comando(self, texto: str) -> None:
        try:
            m = json.loads(texto)
        except ValueError:
            return
        if not isinstance(m, dict) or not isinstance(m.get("camera_id"), str):
            return
        try:
            cid = str(uuid.UUID(m["camera_id"]))
        except ValueError:
            return
        if m.get("cmd") == "stream":
            self._iniciar(cid, m.get("fps"))
        elif m.get("cmd") == "stop":
            self._detener(cid)

    def _iniciar(self, cid: str, fps) -> None:
        device_key = self._device_key(cid)
        if device_key is None:                   # no está asignada a esta mini PC: no se abre
            self._responder({"type": "error", "camera_id": cid, "motivo": "no_asignada"})
            return
        fps = max(FPS_MIN, min(FPS_MAX, fps if isinstance(fps, int) and not isinstance(fps, bool) else FPS_DEFECTO))
        if cid not in self.streams and len(self.streams) >= self.max_flujos:
            self._responder({"type": "error", "camera_id": cid, "motivo": "maximo_de_flujos"})
            return
        s = self.streams.setdefault(cid, {"enviados": 0, "bytes": 0, "instante": None, "ok": None, "proximo": self._reloj(), "desde": self._reloj()})
        s["device_key"], s["fps"] = device_key, fps
        log.info("imagen en vivo pedida: cámara %s a %d cuadros por segundo", cid[:8], fps)

    def _detener(self, cid: str) -> None:
        s = self.streams.pop(cid, None)
        if s is not None:
            log.info("imagen en vivo terminada: cámara %s (%d cuadros, %.0f KB)", cid[:8], s["enviados"], s["bytes"] / 1024)

    def _enviar_pendientes(self, ws) -> None:
        for cid, s in list(self.streams.items()):
            ahora = self._reloj()
            if ahora < s["proximo"]:
                continue
            s["proximo"] = max(s["proximo"] + 1.0 / s["fps"], ahora - 1.0 / s["fps"])
            cuadro, instante = self.gestor.ultimo(s["device_key"])
            ok = cuadro is not None and ahora - instante <= EDAD_MAX_CUADRO_S
            if ok != s["ok"]:                    # la cámara empezó o dejó de dar cuadros: se avisa al servidor
                s["ok"] = ok
                ws.send(json.dumps({"type": "estado", "camera_id": cid, "ok": ok}))
            if not ok or instante == s["instante"]:
                continue                         # sin cuadro, o el mismo que ya se mandó
            datos = codificar_vivo(cuadro, self.ancho, self.calidad)
            if datos is None:
                continue
            s["instante"] = instante
            ws.send_binary(uuid.UUID(cid).bytes + datos)
            s["enviados"] += 1
            s["bytes"] += len(datos)

    def _estadisticas(self, ventana: float) -> None:
        for cid, s in self.streams.items():
            log.info("relevo: cámara %s: %d cuadros enviados, %.0f KB en total", cid[:8], s["enviados"], s["bytes"] / 1024)
