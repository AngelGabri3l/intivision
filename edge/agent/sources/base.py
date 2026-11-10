"""Modelo común de una fuente de video (cámara), sin importar de qué tipo sea."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

TIPOS = ("usb", "onvif", "rtsp")
ESTADOS = ("available", "disconnected", "error")


def ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SourceInfo:
    """Una cámara conocida por el agente.

    El inventario que se envía al servidor se arma con to_dict(): NUNCA incluye `uri`,
    que es lo único que puede llevar usuario y contraseña.
    """
    device_key: str                  # identificador estable, p. ej. usb:/dev/v4l/by-id/...
    kind: str                        # usb | onvif | rtsp
    label: str                       # nombre legible
    state: str = "available"         # available | disconnected | error
    caps: dict = field(default_factory=dict)
    address: str | None = None       # equipo:puerto de una cámara de red (sin credenciales)
    detail: str | None = None        # explicación breve de un estado o de una limitación
    last_seen: str | None = None
    uri: str | None = field(default=None, repr=False)   # INTERNO: ruta o URL para abrirla

    def to_dict(self) -> dict:
        d = {
            "device_key": self.device_key,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "caps": self.caps,
            "last_seen": self.last_seen,
        }
        if self.address:
            d["address"] = self.address
        if self.detail:
            d["detail"] = self.detail
        return d

    def resolucion(self) -> str:
        """Texto para la tabla de `listar`: '1280x720 (MJPG, YUYV)' o '-'."""
        w, h = self.caps.get("max_width"), self.caps.get("max_height")
        if not w or not h:
            return "-"
        formatos = ", ".join(self.caps.get("formats", []))
        return f"{w}x{h}" + (f" ({formatos})" if formatos else "")


@dataclass
class Descubridor:
    """Una forma de buscar cámaras: qué tipo encuentra, cómo se llama a la búsqueda y cada cuánto."""
    tipo: str                                    # usb | onvif | rtsp
    funcion: Callable[[], list[SourceInfo]]
    intervalo: float                             # segundos entre sondeos
