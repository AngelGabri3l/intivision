"""Cámaras declaradas a mano con su dirección RTSP (las que no se descubren solas)."""
from __future__ import annotations

import logging
import socket
from urllib.parse import urlsplit

from ..config import EntradaRtsp, ocultar_credenciales
from .base import SourceInfo, ahora_iso

log = logging.getLogger("agent.rtsp")


def alcanzable(host: str, puerto: int, espera: float = 1.5) -> bool:
    """¿Responde algo en host:puerto? Una conexión TCP corta, sin autenticarse."""
    try:
        with socket.create_connection((host, puerto), timeout=espera):
            return True
    except OSError:
        return False


class RtspManual:
    def __init__(self, entradas: list[EntradaRtsp], sondear=alcanzable):
        self.entradas = entradas
        self.sondear = sondear

    def descubrir(self) -> list[SourceInfo]:
        fuentes = []
        for e in self.entradas:
            p = urlsplit(e.url)
            puerto = p.port or (322 if p.scheme == "rtsps" else 554)
            ok = self.sondear(p.hostname, puerto)
            fuentes.append(SourceInfo(
                device_key=f"rtsp:{e.name}",
                kind="rtsp",
                label=e.name,
                state="available" if ok else "disconnected",
                address=f"{p.hostname}:{puerto}",
                detail=None if ok else "no responde en la red",
                last_seen=ahora_iso() if ok else None,
                uri=e.url,
            ))
            if not ok:
                log.debug("rtsp %s: no responde (%s)", e.name, ocultar_credenciales(e.url))
        return fuentes
