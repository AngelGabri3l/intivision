"""Lectura y validación de agent.yaml.

Si el archivo no existe se usan los valores por defecto, para que `listar` funcione
sin configurar nada.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import yaml

log = logging.getLogger("agent.config")

NOMBRE_RTSP = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RUTA_POR_DEFECTO = "/data/agent.yaml"


class ConfigError(ValueError):
    """Error de configuración, con un mensaje pensado para quien instala."""


@dataclass
class EntradaRtsp:
    name: str
    url: str


@dataclass
class Config:
    poll_seconds: float = 5.0
    onvif_enabled: bool = True
    onvif_poll_seconds: float = 60.0
    width: int = 640
    height: int = 480
    fps: int = 15
    assigned: list[str] = field(default_factory=list)
    rtsp: list[EntradaRtsp] = field(default_factory=list)
    data_dir: str = "/data"


def ocultar_credenciales(url: str) -> str:
    """rtsp://usuario:clave@host/x  ->  rtsp://usuario:***@host/x  (para registros y mensajes)."""
    try:
        p = urlsplit(url)
        if p.password:
            host = p.hostname or ""
            if p.port:
                host += f":{p.port}"
            usuario = p.username or ""
            return urlunsplit((p.scheme, f"{usuario}:***@{host}", p.path, p.query, p.fragment))
    except ValueError:
        pass
    return url


def _numero(d: dict, clave: str, defecto, minimo, maximo, tipo=float):
    valor = d.get(clave, defecto)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ConfigError(f"'{clave}' debe ser un número (tiene: {valor!r})")
    if not (minimo <= valor <= maximo):
        raise ConfigError(f"'{clave}' debe estar entre {minimo} y {maximo} (tiene: {valor})")
    return tipo(valor)


def _validar_url_rtsp(nombre: str, url) -> str:
    if not isinstance(url, str):
        raise ConfigError(f"rtsp '{nombre}': la url debe ser texto")
    try:
        p = urlsplit(url)
        _ = p.port  # valida el puerto
    except ValueError:
        raise ConfigError(f"rtsp '{nombre}': la url no es válida") from None
    if p.scheme not in ("rtsp", "rtsps") or not p.hostname:
        raise ConfigError(f"rtsp '{nombre}': debe empezar con rtsp:// o rtsps:// e incluir el equipo")
    return url


def cargar(ruta: str | None = None) -> Config:
    ruta = ruta or os.environ.get("AGENT_CONFIG") or RUTA_POR_DEFECTO
    cfg = Config(data_dir=os.path.dirname(ruta) or ".")
    if not os.path.exists(ruta):
        log.info("no hay archivo de configuración en %s: se usan los valores por defecto", ruta)
        return cfg
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{ruta} no es un YAML válido: {e}") from None
    if not isinstance(datos, dict):
        raise ConfigError(f"{ruta}: debe ser un diccionario YAML")

    cfg.poll_seconds = _numero(datos, "poll_seconds", cfg.poll_seconds, 1, 3600)
    cfg.onvif_poll_seconds = _numero(datos, "onvif_poll_seconds", cfg.onvif_poll_seconds, 10, 86400)
    onvif = datos.get("onvif_enabled", True)
    if not isinstance(onvif, bool):
        raise ConfigError("'onvif_enabled' debe ser true o false")
    cfg.onvif_enabled = onvif

    cap = datos.get("capture") or {}
    if not isinstance(cap, dict):
        raise ConfigError("'capture' debe ser un diccionario (width, height, fps)")
    cfg.width = _numero(cap, "width", cfg.width, 160, 4096, int)
    cfg.height = _numero(cap, "height", cfg.height, 120, 4096, int)
    cfg.fps = _numero(cap, "fps", cfg.fps, 1, 60, int)

    asignadas = datos.get("assigned") or []
    if not isinstance(asignadas, list) or not all(isinstance(x, str) and x for x in asignadas):
        raise ConfigError("'assigned' debe ser una lista de identificadores (texto)")
    cfg.assigned = list(dict.fromkeys(asignadas))  # sin repetidos, conservando el orden

    vistos = set()
    for i, e in enumerate(datos.get("rtsp") or []):
        if not isinstance(e, dict) or "name" not in e or "url" not in e:
            raise ConfigError(f"rtsp[{i}]: cada entrada necesita 'name' y 'url'")
        nombre = e["name"]
        if not isinstance(nombre, str) or not NOMBRE_RTSP.match(nombre):
            raise ConfigError(f"rtsp[{i}]: el nombre solo admite letras, números, guion y guion bajo (máx. 40)")
        if nombre in vistos:
            raise ConfigError(f"rtsp: el nombre '{nombre}' está repetido")
        vistos.add(nombre)
        cfg.rtsp.append(EntradaRtsp(nombre, _validar_url_rtsp(nombre, e["url"])))
    return cfg
