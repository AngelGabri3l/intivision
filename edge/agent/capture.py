"""Lectura de cuadros de una cámara, igual para USB y para RTSP.

Cada cámara asignada tiene un hilo que lee continuamente y guarda SOLO el último cuadro:
quien lo consuma (la detección en H2) siempre recibe el más reciente y nunca se acumula
retraso ni memoria. Si la cámara falla, el hilo la reabre solo, con espera creciente.

Solo se DECODIFICAN cuadros al ritmo pedido (capture.fps): el resto se recibe y se descarta sin
procesarlo. Así una cámara que entrega muchos más cuadros de los pedidos (o cuadros dañados, como
pasa a veces con USB en máquinas virtuales) no satura el procesador ni llena los registros.
El límite es un "cubo de créditos": acepta ráfagas cortas (una cámara RTSP entrega varios cuadros
juntos y luego una pausa) y solo descarta cuando el ritmo MEDIO supera lo pedido.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

# Las cámaras RTSP se leen por TCP (más fiable que UDP en redes domésticas).
# Debe fijarse antes de abrir la primera cámara.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402  (después de fijar la variable de entorno)

from .sources.base import SourceInfo  # noqa: E402

log = logging.getLogger("agent.capture")

ESPERA_MAX = 10.0
TIMEOUT_RED_MS = 5000
VENTANA_FPS = 5.0               # segundos que cuentan para calcular los cuadros por segundo
FACTOR_DECODIFICACION = 1.25    # se decodifican hasta 1,25 veces los cuadros pedidos por segundo (margen)
RAFAGA_MAXIMA = 3.0             # cuadros seguidos que se aceptan de golpe (las cámaras RTSP los entregan en ráfagas)
FACTOR_RITMO_ANORMAL = 3.0      # la cámara entrega más de 3 veces los cuadros pedidos...
SEGUNDOS_RITMO_ANORMAL = 5.0    # ...durante al menos este tiempo seguido


def elegir_formato(pedido: str, disponibles: list[str]) -> str | None:
    """Qué formato de píxeles pedirle a una cámara USB.

    auto: MJPG si la cámara lo ofrece (comprime y permite más cuadros por USB 2.0).
    mjpg / yuyv: ese formato, si la cámara lo ofrece; si no, se avisa y se usa el automático.
    """
    pedido = (pedido or "auto").upper()
    if pedido in disponibles:
        return pedido
    if pedido != "AUTO":
        log.warning("la cámara no ofrece el formato %s (ofrece: %s); se usa el automático",
                    pedido, ", ".join(disponibles) or "ninguno conocido")
    return "MJPG" if "MJPG" in disponibles else None


def abrir_fuente(info: SourceInfo, ancho: int, alto: int, fps: int, formato: str = "auto"):
    """Abre la fuente con el backend que corresponde a su tipo. Devuelve un cv2.VideoCapture."""
    if info.kind == "usb":
        cap = cv2.VideoCapture(info.uri, cv2.CAP_V4L2)
        if not cap.isOpened():
            return cap
        elegido = elegir_formato(formato, info.caps.get("formats", []))
        if elegido:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*elegido))
        maxw, maxh = info.caps.get("max_width") or ancho, info.caps.get("max_height") or alto
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, min(ancho, maxw))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, min(alto, maxh))
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    if info.kind in ("rtsp", "onvif") and info.uri:
        return cv2.VideoCapture(info.uri, cv2.CAP_FFMPEG,
                                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_RED_MS,
                                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, TIMEOUT_RED_MS])
    raise ValueError(f"la fuente {info.device_key} no se puede abrir (sin ruta ni URL)")


def _ritmo(marcas: list[float], desde: float) -> float:
    recientes = [m for m in marcas if m >= desde]
    if len(recientes) > 1 and recientes[-1] > recientes[0]:
        return (len(recientes) - 1) / (recientes[-1] - recientes[0])
    return 0.0


class Captura(threading.Thread):
    def __init__(self, info: SourceInfo, ancho: int = 640, alto: int = 480, fps: int = 15,
                 abrir=abrir_fuente, formato: str = "auto"):
        super().__init__(name=f"captura-{info.label}", daemon=True)
        self.info = info
        self._ancho, self._alto, self._fps = ancho, alto, fps
        self._formato = formato
        self._abrir = abrir
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._cuadro = None
        self._cuando = 0.0
        self._marcas: deque[float] = deque(maxlen=600)     # cuadros decodificados
        self._crudos: deque[float] = deque(maxlen=4000)    # cuadros que entrega la cámara (se decodifiquen o no)
        self._total = 0
        self._reconexiones = 0
        self._error: str | None = None
        self._aviso: str | None = None
        self._anormal_desde: float | None = None
        # a una cámara de red se le da menos paciencia que a una USB (cada lectura fallida ya esperó 5 s)
        self._max_fallos = 3 if info.kind in ("rtsp", "onvif") else 30

    # --- lo que consulta el resto del agente ---
    def ultimo(self):
        """(cuadro, instante) del último cuadro leído, o (None, 0). No modificar el cuadro."""
        with self._lock:
            return self._cuadro, self._cuando

    def estadisticas(self) -> dict:
        with self._lock:
            desde = time.monotonic() - VENTANA_FPS            # solo los últimos segundos: un cuadro viejo no cuenta
            cuadro = self._cuadro
            return {
                "cuadros": self._total,
                "fps": round(_ritmo(list(self._marcas), desde), 1),
                "crudos_fps": round(_ritmo(list(self._crudos), desde), 1),
                "ancho": int(cuadro.shape[1]) if cuadro is not None else 0,
                "alto": int(cuadro.shape[0]) if cuadro is not None else 0,
                "edad_s": round(time.monotonic() - self._cuando, 1) if self._cuando else None,
                "reconexiones": self._reconexiones,
                "error": self._error,
                "aviso": self._aviso,
            }

    def detener(self) -> None:
        self._parar.set()

    # --- el hilo ---
    def _revisar_ritmo(self, ahora: float) -> None:
        """Avisa (una vez) si la cámara entrega muchos más cuadros de los pedidos durante varios segundos."""
        with self._lock:
            crudo = _ritmo(list(self._crudos), ahora - VENTANA_FPS)
        if crudo > FACTOR_RITMO_ANORMAL * self._fps:
            if self._anormal_desde is None:
                self._anormal_desde = ahora
            elif ahora - self._anormal_desde >= SEGUNDOS_RITMO_ANORMAL and self._aviso is None:
                self._aviso = (f"la cámara entrega {crudo:.0f} cuadros por segundo y se piden {self._fps}: "
                               "puede haber cuadros dañados (USB en máquina virtual); prueba capture.pixel_format: yuyv "
                               "o una resolución menor")
                log.warning("%s: %s", self.info.label, self._aviso)
        else:
            self._anormal_desde = None
            self._aviso = None

    def run(self) -> None:
        espera = 1.0
        while not self._parar.is_set():
            cap = None
            try:
                cap = self._abrir(self.info, self._ancho, self._alto, self._fps, formato=self._formato)
                if not cap.isOpened():
                    raise RuntimeError("no se pudo abrir la cámara")
                self._error = None
                espera = 1.0
                log.info("leyendo %s", self.info.label)
                fallos = 0
                credito, t_credito = 1.0, time.monotonic()
                ultimo_chequeo = time.monotonic()
                while not self._parar.is_set():
                    if not cap.grab():                  # recibe el cuadro SIN decodificarlo
                        fallos += 1
                        if fallos >= self._max_fallos:
                            raise RuntimeError("la cámara dejó de entregar cuadros")
                        time.sleep(0.05)
                        continue
                    ahora = time.monotonic()
                    with self._lock:
                        self._crudos.append(ahora)
                    if ahora - ultimo_chequeo >= 1.0:
                        ultimo_chequeo = ahora
                        self._revisar_ritmo(ahora)
                    credito = min(RAFAGA_MAXIMA, credito + (ahora - t_credito) * self._fps * FACTOR_DECODIFICACION)
                    t_credito = ahora
                    if credito < 1.0:
                        continue                        # el ritmo medio ya supera lo pedido: se descarta sin decodificar
                    credito -= 1.0
                    ok, cuadro = cap.retrieve()         # aquí se decodifica (MJPG -> imagen)
                    if not ok or cuadro is None:
                        fallos += 1
                        if fallos >= self._max_fallos:
                            raise RuntimeError("la cámara dejó de entregar cuadros")
                        continue
                    fallos = 0
                    with self._lock:
                        self._cuadro, self._cuando = cuadro, ahora
                        self._marcas.append(ahora)
                        self._total += 1
            except Exception as e:      # noqa: BLE001
                self._error = str(e)
                self._reconexiones += 1
                log.warning("%s: %s; se reintenta en %.0f s", self.info.label, e, espera)
            finally:
                if cap is not None:
                    cap.release()
            if self._parar.wait(espera):
                break
            espera = min(espera * 2, ESPERA_MAX)


class GestorCaptura:
    """Abre SOLO las cámaras asignadas (agent.yaml -> assigned) y disponibles."""

    def __init__(self, inventario, asignadas: list[str], ancho: int, alto: int, fps: int,
                 abrir=abrir_fuente, formato: str = "auto"):
        self.inventario = inventario
        self.asignadas = list(asignadas)
        self._cfg = (ancho, alto, fps)
        self._formato = formato
        self._abrir = abrir
        self._capturas: dict[str, Captura] = {}

    def sincronizar(self) -> None:
        for clave in self.asignadas:
            info = self.inventario.fuente(clave)
            debe = bool(info and info.state == "available" and info.uri)
            if debe and clave not in self._capturas:
                c = Captura(info, *self._cfg, abrir=self._abrir, formato=self._formato)
                self._capturas[clave] = c
                c.start()
            elif not debe and clave in self._capturas:
                self._capturas.pop(clave).detener()
                log.info("se detuvo la lectura de %s (ya no está disponible)", clave)

    def estados(self) -> dict[str, dict]:
        return {clave: c.estadisticas() for clave, c in self._capturas.items()}

    def abiertas(self) -> list[str]:
        return list(self._capturas)

    def detener_todo(self) -> None:
        for c in self._capturas.values():
            c.detener()
        for c in self._capturas.values():
            c.join(timeout=3)
        self._capturas.clear()
