"""Lectura de cuadros de una cámara, igual para USB y para RTSP.

Cada cámara asignada tiene un hilo que lee continuamente y guarda SOLO el último cuadro:
quien lo consuma (la detección en H2) siempre recibe el más reciente y nunca se acumula
retraso ni memoria. Si la cámara falla, el hilo la reabre solo, con espera creciente.
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
VENTANA_FPS = 5.0        # segundos que cuentan para calcular los cuadros por segundo


def abrir_fuente(info: SourceInfo, ancho: int, alto: int, fps: int):
    """Abre la fuente con el backend que corresponde a su tipo. Devuelve un cv2.VideoCapture."""
    if info.kind == "usb":
        cap = cv2.VideoCapture(info.uri, cv2.CAP_V4L2)
        if not cap.isOpened():
            return cap
        formatos = info.caps.get("formats", [])
        if "MJPG" in formatos:      # comprimido: permite más cuadros por segundo por USB 2.0
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
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


class Captura(threading.Thread):
    def __init__(self, info: SourceInfo, ancho: int = 640, alto: int = 480, fps: int = 15, abrir=abrir_fuente):
        super().__init__(name=f"captura-{info.label}", daemon=True)
        self.info = info
        self._ancho, self._alto, self._fps = ancho, alto, fps
        self._abrir = abrir
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._cuadro = None
        self._cuando = 0.0
        self._marcas: deque[float] = deque(maxlen=600)
        self._total = 0
        self._reconexiones = 0
        self._error: str | None = None
        # a una cámara de red se le da menos paciencia que a una USB (cada lectura fallida ya esperó 5 s)
        self._max_fallos = 3 if info.kind in ("rtsp", "onvif") else 30

    # --- lo que consulta el resto del agente ---
    def ultimo(self):
        """(cuadro, instante) del último cuadro leído, o (None, 0). No modificar el cuadro."""
        with self._lock:
            return self._cuadro, self._cuando

    def estadisticas(self) -> dict:
        with self._lock:
            limite = time.monotonic() - VENTANA_FPS          # solo los últimos segundos: un cuadro viejo no cuenta
            marcas = [m for m in self._marcas if m >= limite]
            cuadro = self._cuadro
            fps = (len(marcas) - 1) / (marcas[-1] - marcas[0]) if len(marcas) > 1 and marcas[-1] > marcas[0] else 0.0
            return {
                "cuadros": self._total,
                "fps": round(fps, 1),
                "ancho": int(cuadro.shape[1]) if cuadro is not None else 0,
                "alto": int(cuadro.shape[0]) if cuadro is not None else 0,
                "edad_s": round(time.monotonic() - self._cuando, 1) if self._cuando else None,
                "reconexiones": self._reconexiones,
                "error": self._error,
            }

    def detener(self) -> None:
        self._parar.set()

    # --- el hilo ---
    def run(self) -> None:
        espera = 1.0
        while not self._parar.is_set():
            cap = None
            try:
                cap = self._abrir(self.info, self._ancho, self._alto, self._fps)
                if not cap.isOpened():
                    raise RuntimeError("no se pudo abrir la cámara")
                self._error = None
                espera = 1.0
                log.info("leyendo %s", self.info.label)
                fallos = 0
                while not self._parar.is_set():
                    ok, cuadro = cap.read()
                    if not ok or cuadro is None:
                        fallos += 1
                        if fallos >= self._max_fallos:
                            raise RuntimeError("la cámara dejó de entregar cuadros")
                        time.sleep(0.05)
                        continue
                    fallos = 0
                    ahora = time.monotonic()
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

    def __init__(self, inventario, asignadas: list[str], ancho: int, alto: int, fps: int, abrir=abrir_fuente):
        self.inventario = inventario
        self.asignadas = list(asignadas)
        self._cfg = (ancho, alto, fps)
        self._abrir = abrir
        self._capturas: dict[str, Captura] = {}

    def sincronizar(self) -> None:
        for clave in self.asignadas:
            info = self.inventario.fuente(clave)
            debe = bool(info and info.state == "available" and info.uri)
            if debe and clave not in self._capturas:
                c = Captura(info, *self._cfg, abrir=self._abrir)
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
