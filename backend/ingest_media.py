"""
Fotos de evidencia que manda una mini PC con cada evento (H2).

Una foto se acepta solo si es un JPEG de verdad, completo, de tamaño razonable y con dimensiones razonables.
Se revisa en este orden, de lo barato a lo caro:

    1. que no esté vacía y no pase de MAX_PHOTO_BYTES                          (413)
    2. que empiece como un JPEG y termine como un JPEG                         (415)
    3. que sus dimensiones, leídas de la CABECERA, estén dentro de lo permitido (422)
    4. que se pueda decodificar                                                (415)

El paso 3 va ANTES de decodificar a propósito: un JPEG diminuto puede declarar dimensiones enormes y, al
decodificarlo, ocupar gigabytes de memoria («bomba de descompresión»).

El nombre del archivo lo genera el servidor: nunca se usa nada que venga de la mini PC.
"""
import os
import uuid

import cv2
import numpy as np

MAX_PHOTO_BYTES = 500_000
MIN_SIDE, MAX_SIDE = 16, 4096


class PhotoError(Exception):
    """La foto no se acepta: lleva el código HTTP y el mensaje para la mini PC."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def jpeg_size(data):
    """(ancho, alto) leídos de la cabecera del JPEG, sin decodificarlo; None si la estructura no es válida."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marcador = data[i + 1]
        if marcador == 0xFF:                                   # relleno entre segmentos
            i += 1
            continue
        if marcador in (0xD8, 0x01) or 0xD0 <= marcador <= 0xD7:   # marcadores sin longitud
            i += 2
            continue
        if marcador in (0xD9, 0xDA):                           # fin o inicio de datos antes de la cabecera de tamaño
            return None
        largo = int.from_bytes(data[i + 2:i + 4], "big")
        if largo < 2:
            return None
        if 0xC0 <= marcador <= 0xCF and marcador not in (0xC4, 0xC8, 0xCC):   # SOF: aquí está el tamaño
            if i + 9 > n:
                return None
            alto = int.from_bytes(data[i + 5:i + 7], "big")
            ancho = int.from_bytes(data[i + 7:i + 9], "big")
            return ancho, alto
        i += 2 + largo
    return None


def validate_photo(data):
    """Lanza PhotoError si la foto no se acepta."""
    if not data:
        raise PhotoError(422, "La foto está vacía.")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError(413, f"La foto pasa de {MAX_PHOTO_BYTES // 1000} KB.")
    if not data.startswith(b"\xff\xd8\xff"):
        raise PhotoError(415, "La foto debe ser un JPEG.")
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise PhotoError(415, "El JPEG está incompleto.")
    tamano = jpeg_size(data)
    if tamano is None:
        raise PhotoError(415, "El JPEG no es válido.")
    ancho, alto = tamano
    if not (MIN_SIDE <= ancho <= MAX_SIDE and MIN_SIDE <= alto <= MAX_SIDE):
        raise PhotoError(422, f"La foto debe medir entre {MIN_SIDE} y {MAX_SIDE} px por lado.")
    imagen = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        raise PhotoError(415, "No se pudo leer el JPEG.")


def save_photo(data, evidence_dir):
    """
    Guarda la foto con un nombre que genera el servidor y devuelve ese nombre. Se escribe primero a un archivo
    temporal y luego se renombra: nadie puede encontrar una foto a medias.
    """
    os.makedirs(evidence_dir, exist_ok=True)
    nombre = f"ev_{uuid.uuid4().hex}.jpg"
    temporal = os.path.join(evidence_dir, f".{nombre}.tmp")
    try:
        with open(temporal, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporal, os.path.join(evidence_dir, nombre))
    except BaseException:
        _quitar(temporal)
        raise
    return nombre


def remove_photo(evidence_dir, name):
    """Borra una foto que acabamos de guardar y que al final no se usó (duplicado o error)."""
    _quitar(os.path.join(evidence_dir, os.path.basename(name)))


def _quitar(ruta):
    try:
        os.remove(ruta)
    except OSError:
        pass
