"""
Grabador: el clip de video de cada evento en las cámaras cuyo plan lo incluye (Plan Premium).

Cómo funciona:
  • Para cada cámara con clip, guarda en memoria los últimos segundos (pre-buffer) como JPEG livianos, a ~8 cuadros por segundo.
  • Cuando el analizador encola un evento, se pide el clip: se toman los segundos previos y se sigue grabando hasta completar la
    duración del plan (20 s = 10 antes y 10 después del evento).
  • El clip se codifica en MP4 H.264 con ffmpeg (Telegram lo reproduce en el chat). Sin ffmpeg, se usa OpenCV (MPEG-4).
  • El archivo queda en la carpeta de clips con su ficha (.json); el EnviadorClips lo manda al servidor y lo borra.
  • Hay un tope de espacio: si se pasa, se borran los clips más viejos que aún no salieron.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque

import cv2
import numpy as np

log = logging.getLogger("agent.grabador")
PREVIO_MAX_S = 10.0            # segundos antes del evento (como mucho la mitad del clip)
EDAD_MAX_CUADRO_S = 2.0
EXTENSION_FICHA = ".json"


class Grabacion:
    def __init__(self, camera_id: str, edge_event_id: str, cuadros: list, total: int, hasta: float):
        self.camera_id, self.edge_event_id = camera_id, edge_event_id
        self.cuadros = list(cuadros)          # [(instante, jpeg)]
        self.total, self.hasta = total, hasta


def ffmpeg_disponible() -> str | None:
    return shutil.which("ffmpeg")


def codificar_mp4(jpegs: list[bytes], fps: float, destino: str, ffmpeg: str | None = None) -> bool:
    """Escribe los JPEG como un MP4. True si quedó un archivo válido."""
    if not jpegs:
        return False
    ffmpeg = ffmpeg if ffmpeg is not None else ffmpeg_disponible()
    if ffmpeg:
        orden = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", f"{fps:g}",
                 "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", "-an", destino]
        try:
            r = subprocess.run(orden, input=b"".join(jpegs), capture_output=True, timeout=120)
            if r.returncode == 0 and os.path.getsize(destino) > 0:
                return True
            log.warning("ffmpeg no pudo codificar un clip (%s); se intenta con OpenCV", r.stderr.decode("utf-8", "replace")[-160:].strip())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("ffmpeg falló al codificar un clip (%s); se intenta con OpenCV", e.__class__.__name__)
    primero = cv2.imdecode(np.frombuffer(jpegs[0], np.uint8), cv2.IMREAD_COLOR)
    if primero is None:
        return False
    alto, ancho = primero.shape[:2]
    escritor = cv2.VideoWriter(destino, cv2.VideoWriter_fourcc(*"mp4v"), fps, (ancho, alto))
    if not escritor.isOpened():
        return False
    try:
        for j in jpegs:
            c = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR)
            if c is None:
                continue
            if c.shape[:2] != (alto, ancho):
                c = cv2.resize(c, (ancho, alto))
            escritor.write(c)
    finally:
        escritor.release()
    return os.path.exists(destino) and os.path.getsize(destino) > 0


class Grabador(threading.Thread):
    def __init__(self, gestor, config, dir_clips: str, fps: float = 8.0, ancho: int = 640, calidad: int = 70,
                 max_mb: float = 300.0, reloj=time.monotonic, ffmpeg: str | None = None):
        super().__init__(name="grabador", daemon=True)
        self.gestor, self.config, self.dir = gestor, config, dir_clips
        self.fps, self.ancho, self.calidad, self.max_bytes = float(fps), int(ancho), int(calidad), int(max_mb * 1e6)
        self._reloj = reloj
        self.ffmpeg = ffmpeg if ffmpeg is not None else ffmpeg_disponible()
        self._previos: dict[str, deque] = {}
        self._ultimo: dict[str, float] = {}
        self._activas: list[Grabacion] = []
        self._listas: deque[Grabacion] = deque()
        self._lock = threading.Lock()
        self._parar, self._hay_trabajo = threading.Event(), threading.Event()
        self.grabados = 0
        self.descartados = 0
        os.makedirs(self.dir, exist_ok=True)
        self._codificador = threading.Thread(target=self._codificar_siempre, name="grabador-codificador", daemon=True)

    # --- qué cámaras graban
    def _camaras(self) -> dict[str, dict]:
        return {c["camera_id"]: c for c in self.config.camaras() if (c.get("plan") or {}).get("clip_s", 0) > 0}

    def solicitar(self, camera_id: str, edge_event_id: str) -> bool:
        """Pide el clip de un evento recién encolado. False si la cámara no tiene clips en su plan."""
        cam = self._camaras().get(camera_id)
        if cam is None:
            return False
        duracion = float(cam["plan"]["clip_s"])
        previo_s = min(PREVIO_MAX_S, duracion / 2)
        ahora = self._reloj()
        with self._lock:
            previos = [x for x in self._previos.get(camera_id, ()) if x[0] >= ahora - previo_s]
            total = max(1, int(round(duracion * self.fps)))
            self._activas.append(Grabacion(camera_id, edge_event_id, previos, total, ahora + (duracion - previo_s)))
        return True

    # --- el ciclo de captura
    def start(self) -> None:
        self._codificador.start()
        super().start()

    def detener(self) -> None:
        self._parar.set()
        self._hay_trabajo.set()

    def run(self) -> None:
        periodo = 1.0 / self.fps
        while not self._parar.is_set():
            t0 = self._reloj()
            try:
                self.pasada()
            except Exception:       # noqa: BLE001 - un problema con una cámara no debe detener al grabador
                log.exception("error en el grabador de clips")
            self._parar.wait(max(0.0, periodo - (self._reloj() - t0)))

    def pasada(self) -> None:
        camaras = self._camaras()
        ahora = self._reloj()
        previo_max = int(PREVIO_MAX_S * self.fps) + 2
        with self._lock:
            for cid in [k for k in self._previos if k not in camaras]:
                del self._previos[cid]
        for cid, cam in camaras.items():
            cuadro, instante = self.gestor.ultimo(cam["device_key"])
            if cuadro is None or instante == self._ultimo.get(cid) or ahora - instante > EDAD_MAX_CUADRO_S:
                continue
            self._ultimo[cid] = instante
            jpeg = self._jpeg(cuadro)
            if jpeg is None:
                continue
            with self._lock:
                self._previos.setdefault(cid, deque(maxlen=previo_max)).append((instante, jpeg))
                for g in self._activas:
                    if g.camera_id == cid and len(g.cuadros) < g.total:
                        g.cuadros.append((instante, jpeg))
        with self._lock:
            listas = [g for g in self._activas if len(g.cuadros) >= g.total or ahora >= g.hasta]
            self._activas = [g for g in self._activas if g not in listas]
            self._listas.extend(listas)
        if listas:
            self._hay_trabajo.set()

    def _jpeg(self, cuadro) -> bytes | None:
        alto, ancho = cuadro.shape[:2]
        if ancho > self.ancho:
            cuadro = cv2.resize(cuadro, (self.ancho, max(2, round(alto * self.ancho / ancho))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", cuadro, [cv2.IMWRITE_JPEG_QUALITY, self.calidad])
        return buf.tobytes() if ok else None

    # --- codificar y dejar listo para enviar
    def _codificar_siempre(self) -> None:
        while not self._parar.is_set():
            self._hay_trabajo.wait(1.0)
            self._hay_trabajo.clear()
            while True:
                with self._lock:
                    g = self._listas.popleft() if self._listas else None
                if g is None:
                    break
                try:
                    self.guardar(g)
                except Exception:   # noqa: BLE001
                    self.descartados += 1
                    log.exception("no se pudo guardar el clip de un evento")

    def guardar(self, g: Grabacion) -> str | None:
        """Codifica la grabación y la deja en la carpeta de clips. Devuelve la ruta, o None si no había imagen."""
        if not g.cuadros:
            self.descartados += 1
            log.warning("el clip de un evento quedó vacío (la cámara no entregó imagen): no se envía")
            return None
        nombre = "".join(ch for ch in g.edge_event_id if ch.isalnum() or ch in "-_")[:64] or "clip"
        destino = os.path.join(self.dir, nombre + ".mp4")
        fd, temporal = tempfile.mkstemp(prefix=".tmp_", suffix=".mp4", dir=self.dir)
        os.close(fd)
        try:
            if not codificar_mp4([j for _, j in g.cuadros], self.fps, temporal, self.ffmpeg):
                self.descartados += 1
                log.warning("no se pudo codificar el clip de un evento")
                return None
            with open(destino + EXTENSION_FICHA + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"edge_event_id": g.edge_event_id, "camera_id": g.camera_id,
                           "duration_s": round(len(g.cuadros) / self.fps, 2)}, f)
            os.replace(temporal, destino)
            os.replace(destino + EXTENSION_FICHA + ".tmp", destino[:-4] + EXTENSION_FICHA)
        finally:
            for resto in (temporal, destino + EXTENSION_FICHA + ".tmp"):
                if os.path.exists(resto):
                    os.remove(resto)
        self.grabados += 1
        log.info("clip listo: %.1f s de la cámara %s", len(g.cuadros) / self.fps, g.camera_id[:8])
        aplicar_tope(self.dir, self.max_bytes)
        return destino

    def estado(self) -> dict:
        with self._lock:
            return {"grabando": len(self._activas), "por_codificar": len(self._listas), "grabados": self.grabados,
                    "descartados": self.descartados, "ffmpeg": bool(self.ffmpeg)}


def pendientes(directorio: str) -> list[tuple[str, dict]]:
    """Los clips listos para enviar, del más viejo al más nuevo: [(ruta del mp4, ficha)]."""
    try:
        nombres = os.listdir(directorio)
    except OSError:
        return []
    salida = []
    for n in nombres:
        if not n.endswith(".mp4") or n.startswith("."):
            continue
        ruta = os.path.join(directorio, n)
        try:
            with open(ruta[:-4] + EXTENSION_FICHA, encoding="utf-8") as f:
                ficha = json.load(f)
            salida.append((os.path.getmtime(ruta), ruta, ficha))
        except (OSError, ValueError):
            continue
    return [(r, f) for _, r, f in sorted(salida)]


def borrar(ruta_mp4: str) -> None:
    for r in (ruta_mp4, ruta_mp4[:-4] + EXTENSION_FICHA):
        try:
            os.remove(r)
        except OSError:
            pass


def aplicar_tope(directorio: str, max_bytes: int) -> int:
    """Si los clips pendientes pasan del tope, borra los más viejos. Devuelve cuántos borró."""
    lista = pendientes(directorio)
    total = sum(os.path.getsize(r) for r, _ in lista if os.path.exists(r))
    borrados = 0
    for ruta, _ in lista:
        if total <= max_bytes:
            break
        try:
            total -= os.path.getsize(ruta)
        except OSError:
            pass
        borrar(ruta)
        borrados += 1
    if borrados:
        log.warning("se borraron %d clip(s) sin enviar porque se llegó al tope de espacio", borrados)
    return borrados
