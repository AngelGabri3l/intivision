"""Lectura y validación de agent.yaml.

Si el archivo no existe se usan los valores por defecto, para que `listar` funcione
sin configurar nada.

La conexión con el servidor NO va en el archivo: SERVER_URL y EDGE_TOKEN se leen del entorno (edge/.env en Docker),
para que el token no quede en un archivo que alguien pueda subir a git o compartir por error.
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
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
HOSTS_LOCALES = ("localhost", "127.0.0.1", "::1")
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
    pixel_format: str = "auto"          # auto | MJPG | YUYV (solo cámaras USB)
    assigned: list[str] = field(default_factory=list)
    rtsp: list[EntradaRtsp] = field(default_factory=list)
    data_dir: str = "/data"
    heartbeat_seconds: float = 30.0     # cada cuánto manda su inventario y pide su configuración
    queue_max_mb: float = 200.0         # tope de la cola de eventos pendientes
    queue_max_age_h: float = 24.0       # y de su antigüedad
    analysis_enabled: bool = True       # analizar con el motor las cámaras asignadas
    analysis_fps: float = 3.0           # cuadros por segundo que se intenta analizar por cámara
    analysis_event_cooldown_s: float = 10.0   # tiempo mínimo entre eventos de una misma cámara
    analysis_max_gap_s: float = 10.0    # sin cuadros más de este tiempo: se reinicia el análisis de esa cámara
    engine_config: str | None = None    # variable ENGINE_CONFIG: otro config.yaml para el motor
    live_enabled: bool = True           # enviar imagen en vivo cuando el servidor la pida (H7)
    live_max_streams: int = 4           # cámaras en vivo a la vez
    live_width: int = 640               # ancho de la imagen en vivo
    live_jpeg_quality: int = 70
    server_url: str | None = None       # variable SERVER_URL
    edge_token: str | None = field(default=None, repr=False)    # variable EDGE_TOKEN: nunca se imprime

    def servidor(self):
        """(url, token) listos para usar, None si no se configuró el servidor, o ConfigError si está a medias o mal."""
        url, token = self.server_url, self.edge_token
        if not url and not token:
            return None
        if not url:
            raise ConfigError("falta SERVER_URL (EDGE_TOKEN sí está definido)")
        if not token:
            raise ConfigError("falta EDGE_TOKEN (SERVER_URL sí está definido)")
        if not TOKEN_RE.match(token):
            raise ConfigError("EDGE_TOKEN no tiene el formato esperado (de 20 a 100 letras, números, guion o guion bajo); "
                              "revisa que no tenga espacios ni comillas")
        return validar_url_servidor(url), token


def validar_url_servidor(url) -> str:
    """La dirección base del servidor. Solo se acepta http:// hacia este mismo equipo (un túnel SSH): hacia un equipo
    remoto el token viajaría sin cifrar."""
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("SERVER_URL está vacía")
    try:
        p = urlsplit(url.strip())
        _ = p.port
    except ValueError:
        raise ConfigError("SERVER_URL no es una dirección válida") from None
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ConfigError("SERVER_URL debe empezar con https:// (o http:// solo para localhost) e incluir el equipo")
    if p.username or p.password:
        raise ConfigError("SERVER_URL no debe llevar usuario ni contraseña")
    if p.query or p.fragment:
        raise ConfigError("SERVER_URL no debe llevar parámetros")
    if p.scheme == "http" and p.hostname.lower() not in HOSTS_LOCALES:
        raise ConfigError("SERVER_URL usa http:// hacia un equipo remoto: el token viajaría sin cifrar. "
                          "Usa https:// (o un túnel SSH hacia localhost)")
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


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
    cfg.server_url = os.environ.get("SERVER_URL", "").strip() or None
    cfg.edge_token = os.environ.get("EDGE_TOKEN", "").strip() or None
    cfg.engine_config = os.environ.get("ENGINE_CONFIG", "").strip() or None
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
    formato = cap.get("pixel_format", "auto")
    if not isinstance(formato, str) or formato.strip().lower() not in ("auto", "mjpg", "yuyv"):
        raise ConfigError("'capture.pixel_format' debe ser auto, mjpg o yuyv")
    cfg.pixel_format = "auto" if formato.strip().lower() == "auto" else formato.strip().upper()

    cfg.heartbeat_seconds = _numero(datos, "heartbeat_seconds", cfg.heartbeat_seconds, 5, 3600)
    cola = datos.get("queue") or {}
    if not isinstance(cola, dict):
        raise ConfigError("'queue' debe ser un diccionario (max_mb, max_age_h)")
    cfg.queue_max_mb = _numero(cola, "max_mb", cfg.queue_max_mb, 5, 5000)
    cfg.queue_max_age_h = _numero(cola, "max_age_h", cfg.queue_max_age_h, 1, 720)

    an = datos.get("analysis") or {}
    if not isinstance(an, dict):
        raise ConfigError("'analysis' debe ser un diccionario (enabled, fps, event_cooldown_s, max_gap_s)")
    habilitado = an.get("enabled", True)
    if not isinstance(habilitado, bool):
        raise ConfigError("'analysis.enabled' debe ser true o false")
    cfg.analysis_enabled = habilitado
    cfg.analysis_fps = _numero(an, "fps", cfg.analysis_fps, 0.2, 15)
    cfg.analysis_event_cooldown_s = _numero(an, "event_cooldown_s", cfg.analysis_event_cooldown_s, 1, 3600)
    cfg.analysis_max_gap_s = _numero(an, "max_gap_s", cfg.analysis_max_gap_s, 2, 300)

    vivo = datos.get("live") or {}
    if not isinstance(vivo, dict):
        raise ConfigError("'live' debe ser un diccionario (enabled, max_streams, width, jpeg_quality)")
    activo = vivo.get("enabled", True)
    if not isinstance(activo, bool):
        raise ConfigError("'live.enabled' debe ser true o false")
    cfg.live_enabled = activo
    cfg.live_max_streams = int(_numero(vivo, "max_streams", cfg.live_max_streams, 1, 8))
    cfg.live_width = int(_numero(vivo, "width", cfg.live_width, 160, 1280))
    cfg.live_jpeg_quality = int(_numero(vivo, "jpeg_quality", cfg.live_jpeg_quality, 20, 95))

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
