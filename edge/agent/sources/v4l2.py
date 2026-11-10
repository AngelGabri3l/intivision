"""Cámaras USB e integradas (Video4Linux2).

Cómo se encuentran:
  1. Se recorren los nodos de /sys/class/video4linux/videoN.
  2. Una cámara USB suele crear DOS nodos: uno de captura y otro de metadatos. Se descartan
     los de metadatos preguntándole al driver (VIDIOC_QUERYCAP).
  3. Cada cámara recibe un identificador ESTABLE: el enlace de /dev/v4l/by-id (usa el número
     de serie, no cambia entre reinicios); si no tiene serie, /dev/v4l/by-path (depende del
     puerto USB); y como último recurso /dev/videoN (puede cambiar).
  4. Se leen sus formatos y resoluciones con VIDIOC_ENUM_FMT / VIDIOC_ENUM_FRAMESIZES. Esas
     consultas no abren el flujo de video, así que no estorban si otro programa la está usando.

Las rutas de /dev y /sys se pueden cambiar (AGENT_DEV_ROOT, AGENT_SYS_ROOT) para probar el
descubrimiento con un árbol de archivos falso.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import struct

from .base import SourceInfo, ahora_iso

log = logging.getLogger("agent.v4l2")

# --- constantes del kernel (linux/videodev2.h) ---------------------------------------------
BUF_TYPE_VIDEO_CAPTURE = 1
CAP_VIDEO_CAPTURE = 0x00000001
CAP_META_CAPTURE = 0x00800000
CAP_DEVICE_CAPS = 0x80000000
FRMSIZE_DISCRETE, FRMSIZE_CONTINUOUS, FRMSIZE_STEPWISE = 1, 2, 3


def _ioc(direccion: int, tipo: str, nr: int, tamano: int) -> int:
    return (direccion << 30) | (tamano << 16) | (ord(tipo) << 8) | nr


_LEER, _ESCRIBIR = 2, 1
FMT_CAPABILITY = "16s32s32sIII12x"      # struct v4l2_capability   (104 bytes)
FMT_FMTDESC = "III32sII12x"             # struct v4l2_fmtdesc      (64 bytes)
FMT_FRMSIZEENUM = "III24s8x"            # struct v4l2_frmsizeenum  (44 bytes)
VIDIOC_QUERYCAP = _ioc(_LEER, "V", 0, struct.calcsize(FMT_CAPABILITY))
VIDIOC_ENUM_FMT = _ioc(_LEER | _ESCRIBIR, "V", 2, struct.calcsize(FMT_FMTDESC))
VIDIOC_ENUM_FRAMESIZES = _ioc(_LEER | _ESCRIBIR, "V", 74, struct.calcsize(FMT_FRMSIZEENUM))


def _texto(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def _preguntar(fd: int, pedido: int, estructura: bytes, ioctl=fcntl.ioctl) -> bytes:
    buf = bytearray(estructura)
    ioctl(fd, pedido, buf, True)
    return bytes(buf)


def leer_capacidades(ruta: str, ioctl=fcntl.ioctl, abrir=os.open, cerrar=os.close) -> dict:
    """Pregunta al driver qué es el nodo y qué formatos y tamaños admite.

    Devuelve {"capture": bool, "card": str, "formats": [...], "max_width": int, "max_height": int}
    o {"error": "..."} si no se pudo consultar (permisos, nodo inexistente...).
    """
    try:
        fd = abrir(ruta, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        return {"error": f"sin permisos para abrir {ruta} (¿el usuario del contenedor está en el grupo 'video'?)"}
    except OSError as e:
        return {"error": f"no se pudo abrir {ruta}: {os.strerror(e.errno or 0)}"}
    try:
        crudo = _preguntar(fd, VIDIOC_QUERYCAP, bytes(struct.calcsize(FMT_CAPABILITY)), ioctl)
        _driver, card, _bus, _version, capacidades, caps_nodo = struct.unpack(FMT_CAPABILITY, crudo)[:6]
        propias = caps_nodo if capacidades & CAP_DEVICE_CAPS else capacidades
        es_captura = bool(propias & CAP_VIDEO_CAPTURE) and not (propias & CAP_META_CAPTURE)
        resultado = {"capture": es_captura, "card": _texto(card), "formats": [], "max_width": 0, "max_height": 0}
        if not es_captura:
            return resultado

        mayor = {}      # formato -> (ancho, alto) más grande
        for i in range(64):
            try:
                crudo = _preguntar(fd, VIDIOC_ENUM_FMT, struct.pack(FMT_FMTDESC, i, BUF_TYPE_VIDEO_CAPTURE, 0, b"", 0, 0), ioctl)
            except OSError as e:
                if e.errno == errno.EINVAL:
                    break
                raise
            pixfmt = struct.unpack(FMT_FMTDESC, crudo)[4]
            nombre = struct.pack("<I", pixfmt).decode("ascii", "replace").strip("\0 ")
            mayor[nombre] = (0, 0)
            for j in range(256):
                try:
                    crudo = _preguntar(fd, VIDIOC_ENUM_FRAMESIZES, struct.pack(FMT_FRMSIZEENUM, j, pixfmt, 0, b""), ioctl)
                except OSError as e:
                    if e.errno == errno.EINVAL:
                        break
                    raise
                _i, _p, tipo, union = struct.unpack(FMT_FRMSIZEENUM, crudo)
                if tipo == FRMSIZE_DISCRETE:
                    w, h = struct.unpack("<II", union[:8])
                else:       # continuo o escalonado: el máximo está en max_width y max_height
                    _minw, w, _stw, _minh, h, _sth = struct.unpack("<6I", union)
                if w * h > mayor[nombre][0] * mayor[nombre][1]:
                    mayor[nombre] = (w, h)
                if tipo != FRMSIZE_DISCRETE:
                    break
        resultado["formats"] = list(mayor)
        if mayor:
            resultado["max_width"], resultado["max_height"] = max(mayor.values(), key=lambda s: s[0] * s[1])
        return resultado
    except OSError as e:
        return {"error": f"el driver no respondió a la consulta de {ruta}: {os.strerror(e.errno or 0)}"}
    finally:
        cerrar(fd)


def _enlaces(raiz_dev: str, subcarpeta: str) -> dict[str, list[str]]:
    """{ruta real del nodo: [nombres de enlace]} para /dev/v4l/by-id o /dev/v4l/by-path."""
    carpeta = os.path.join(raiz_dev, "v4l", subcarpeta)
    salida: dict[str, list[str]] = {}
    try:
        nombres = sorted(os.listdir(carpeta))
    except OSError:
        return salida
    for nombre in nombres:
        destino = os.path.realpath(os.path.join(carpeta, nombre))
        salida.setdefault(destino, []).append(nombre)
    return salida


def _elegir(nombres: list[str] | None) -> str | None:
    """Entre varios enlaces del mismo nodo, prefiere el '-video-index0' (el de captura)."""
    if not nombres:
        return None
    for n in nombres:
        if n.endswith("-video-index0"):
            return n
    return nombres[0]


def _limpiar_nombre(nombre: str) -> str:
    """'HD Webcam C270: HD Webcam C270' -> 'HD Webcam C270' (muchos drivers repiten el nombre)."""
    partes = [p.strip() for p in nombre.split(":")]
    if len(partes) == 2 and partes[0] == partes[1]:
        return partes[0]
    return nombre.strip()


class V4l2Descubridor:
    def __init__(self, raiz_dev: str | None = None, raiz_sys: str | None = None, leer=leer_capacidades):
        self.raiz_dev = raiz_dev or os.environ.get("AGENT_DEV_ROOT", "/dev")
        self.raiz_sys = raiz_sys or os.environ.get("AGENT_SYS_ROOT", "/sys")
        self.leer = leer
        self._cache: dict[str, dict] = {}     # ruta real -> capacidades ya consultadas

    def _nodos(self) -> list[tuple[int, str]]:
        carpeta = os.path.join(self.raiz_sys, "class", "video4linux")
        try:
            nombres = os.listdir(carpeta)
        except OSError:
            return []
        encontrados = []
        for n in nombres:
            m = re.fullmatch(r"video(\d+)", n)
            if m:
                encontrados.append((int(m.group(1)), n))
        return sorted(encontrados)

    def _capacidades(self, ruta_real: str) -> dict:
        if ruta_real in self._cache:
            return self._cache[ruta_real]
        info = self.leer(ruta_real)
        if "error" not in info:          # los errores no se guardan: se reintentan en el siguiente sondeo
            self._cache[ruta_real] = info
        return info

    def descubrir(self) -> list[SourceInfo]:
        raiz = os.path.realpath(self.raiz_dev)
        por_id = _enlaces(raiz, "by-id")
        por_ruta = _enlaces(raiz, "by-path")
        vistos: set[str] = set()
        fuentes: list[SourceInfo] = []

        for numero, nombre_nodo in self._nodos():
            nodo = os.path.join(raiz, nombre_nodo)
            if not os.path.exists(nodo):
                continue
            real = os.path.realpath(nodo)
            vistos.add(real)
            info = self._capacidades(real)

            if "error" in info:
                nombre_sys = self._leer_sys(nombre_nodo, "name") or nombre_nodo
                indice = self._leer_sys(nombre_nodo, "index")
                if indice not in (None, "0"):
                    continue                 # sin poder preguntar, solo se asume captura el índice 0
                estado, detalle, caps, etiqueta = "error", info["error"], {}, _limpiar_nombre(nombre_sys)
            else:
                if not info["capture"]:
                    continue
                estado, detalle = "available", None
                caps = {"formats": info["formats"], "max_width": info["max_width"], "max_height": info["max_height"]}
                etiqueta = _limpiar_nombre(info["card"] or self._leer_sys(nombre_nodo, "name") or nombre_nodo)

            enlace_id = _elegir(por_id.get(real))
            enlace_ruta = _elegir(por_ruta.get(real))
            if enlace_id:
                clave, ruta_abrir = f"usb:/dev/v4l/by-id/{enlace_id}", os.path.join(raiz, "v4l", "by-id", enlace_id)
            elif enlace_ruta:
                clave, ruta_abrir = f"usb:/dev/v4l/by-path/{enlace_ruta}", os.path.join(raiz, "v4l", "by-path", enlace_ruta)
                detalle = detalle or "sin número de serie: el identificador depende del puerto USB"
            else:
                clave, ruta_abrir = f"usb:/dev/video{numero}", nodo
                detalle = detalle or "sin identificador estable: puede cambiar al reiniciar"

            fuentes.append(SourceInfo(device_key=clave, kind="usb", label=etiqueta, state=estado, caps=caps,
                                      detail=detalle, last_seen=ahora_iso(), uri=ruta_abrir))

        for real in list(self._cache):      # nodos que ya no existen: se olvidan sus capacidades
            if real not in vistos:
                del self._cache[real]
        return fuentes

    def _leer_sys(self, nodo: str, archivo: str) -> str | None:
        try:
            with open(os.path.join(self.raiz_sys, "class", "video4linux", nodo, archivo), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None
