"""Fuentes de video del agente: USB (V4L2), ONVIF y RTSP manual."""
from __future__ import annotations

from ..config import Config
from . import onvif
from .base import Descubridor, SourceInfo
from .rtsp import RtspManual
from .v4l2 import V4l2Descubridor

__all__ = ["SourceInfo", "construir_descubridores"]


def construir_descubridores(cfg: Config, incluir_onvif: bool = True) -> list[Descubridor]:
    descubridores = [Descubridor("usb", V4l2Descubridor().descubrir, cfg.poll_seconds)]
    if cfg.rtsp:
        descubridores.append(Descubridor("rtsp", RtspManual(cfg.rtsp).descubrir, cfg.poll_seconds * 2))
    if cfg.onvif_enabled and incluir_onvif:
        descubridores.append(Descubridor("onvif", onvif.descubrir, cfg.onvif_poll_seconds))
    return descubridores
