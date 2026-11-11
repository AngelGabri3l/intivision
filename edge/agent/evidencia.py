"""Fotos de evidencia: convertir un cuadro de video en un JPEG que el servidor acepte (como máximo 500 KB)."""
from __future__ import annotations

import cv2
import numpy as np

MAX_BYTES = 450_000          # el servidor rechaza más de 500.000: se deja margen
ANCHO_MAX = 1280


def codificar_jpeg(cuadro, max_bytes: int = MAX_BYTES, ancho_max: int = ANCHO_MAX) -> bytes:
    """
    Comprime un cuadro (matriz BGR de OpenCV) a un JPEG de hasta `max_bytes`: primero lo reduce si es muy ancho y
    luego baja la calidad hasta que cabe. Lanza ValueError si no es una imagen o si no logra que quepa.
    """
    if cuadro is None or not hasattr(cuadro, "shape") or cuadro.ndim not in (2, 3) or cuadro.shape[0] < 16 or cuadro.shape[1] < 16:
        raise ValueError("no es una imagen válida")
    alto, ancho = cuadro.shape[:2]
    if ancho > ancho_max:
        cuadro = cv2.resize(cuadro, (ancho_max, max(16, int(alto * ancho_max / ancho))), interpolation=cv2.INTER_AREA)
    for _ in range(4):                                   # si aun así no cabe, se reduce a tres cuartos y se repite
        for calidad in (85, 75, 65, 55, 45, 35):
            ok, buf = cv2.imencode(".jpg", cuadro, [cv2.IMWRITE_JPEG_QUALITY, calidad])
            if ok and len(buf) <= max_bytes:
                return buf.tobytes()
        alto, ancho = cuadro.shape[:2]
        if ancho < 64 or alto < 64:
            break
        cuadro = cv2.resize(cuadro, (int(ancho * 0.75), int(alto * 0.75)), interpolation=cv2.INTER_AREA)
    raise ValueError("no se pudo comprimir la imagen por debajo del tope")


def imagen_de_prueba(texto: str = "PRUEBA DEL AGENTE", ancho: int = 640, alto: int = 360) -> bytes:
    """Un JPEG sintético con un texto, para probar el envío cuando no hay una cámara de la que tomar un cuadro."""
    cuadro = np.full((alto, ancho, 3), 90, np.uint8)
    cv2.putText(cuadro, texto, (30, alto // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
    return codificar_jpeg(cuadro)
