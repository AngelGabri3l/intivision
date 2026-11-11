"""
La configuración que el servidor le da a esta mini PC (GET /edge/config), con una copia en disco.

El servidor decide QUÉ cámaras se analizan (las que el administrador asignó), y con qué zona restringida y horario sensible.
La copia en disco sirve para seguir funcionando si el agente arranca sin conexión: usa la última configuración conocida.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading

log = logging.getLogger("agent.config_remota")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _numero(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def normalizar(bruta) -> dict | None:
    """La configuración del servidor, limpia y con lo que este agente entiende. None si no es una configuración."""
    if not isinstance(bruta, dict) or not isinstance(bruta.get("cameras"), list):
        return None
    camaras = []
    for c in bruta["cameras"]:
        if not (isinstance(c, dict) and isinstance(c.get("camera_id"), str) and UUID_RE.match(c["camera_id"])
                and isinstance(c.get("device_key"), str) and 0 < len(c["device_key"]) <= 300):
            log.warning("se ignoró una cámara de la configuración porque no tiene el formato esperado")
            continue
        zona = c.get("restricted_zone")
        if not (isinstance(zona, list) and all(isinstance(p, list) and len(p) == 2 and all(_numero(x) for x in p) for p in zona)):
            zona = None
        camaras.append({"camera_id": c["camera_id"].lower(), "device_key": c["device_key"],
                        "name": c["name"][:100] if isinstance(c.get("name"), str) else "",
                        "kind": c["kind"] if c.get("kind") in ("private", "common") else "private",
                        "restricted_zone": zona})
    horas = bruta.get("sensitive_hours")
    if not (isinstance(horas, list) and len(horas) == 2 and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in horas)):
        horas = None
    limpia = {"cameras": camaras, "sensitive_hours": horas}
    version = bruta.get("version")
    limpia["version"] = version[:64] if isinstance(version, str) and version else \
        hashlib.sha256(json.dumps(limpia, sort_keys=True).encode()).hexdigest()[:16]
    return limpia


class ConfigRemota:
    def __init__(self, ruta_cache: str, al_cambiar=None):
        self.ruta_cache = ruta_cache
        self.al_cambiar = al_cambiar
        self._lock = threading.RLock()
        self._cfg: dict | None = None
        self.origen: str | None = None           # "servidor" o "copia" (la de disco)

    @property
    def lista(self) -> bool:
        return self._cfg is not None

    @property
    def version(self) -> str | None:
        return self._cfg["version"] if self._cfg else None

    def camaras(self) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._cfg["cameras"]] if self._cfg else []

    def device_keys(self) -> list[str]:
        return [c["device_key"] for c in self.camaras()]

    def camera_id_de(self, device_key: str) -> str | None:
        return next((c["camera_id"] for c in self.camaras() if c["device_key"] == device_key), None)

    def horario_sensible(self):
        with self._lock:
            return list(self._cfg["sensitive_hours"]) if self._cfg and self._cfg["sensitive_hours"] else None

    def cargar_copia(self) -> bool:
        """Al arrancar sin red: la última configuración conocida."""
        try:
            with open(self.ruta_cache, encoding="utf-8") as f:
                limpia = normalizar(json.load(f))
        except (OSError, ValueError):
            return False
        if limpia is None:
            return False
        with self._lock:
            self._cfg, self.origen = limpia, "copia"
        log.info("configuración cargada de la copia en disco (versión %s, %d cámara(s))", limpia["version"], len(limpia["cameras"]))
        self._avisar()
        return True

    def aplicar(self, bruta) -> bool:
        """Aplica lo que mandó el servidor. Devuelve True si cambió algo (otra versión)."""
        limpia = normalizar(bruta)
        if limpia is None:
            log.warning("el servidor mandó una configuración que no se entiende: se conserva la anterior")
            return False
        with self._lock:
            igual = self._cfg is not None and self._cfg["version"] == limpia["version"]
            self._cfg, self.origen = limpia, "servidor"
        if igual:
            return False
        self._guardar(limpia)
        log.info("nueva configuración del servidor (versión %s): %d cámara(s) asignada(s)", limpia["version"], len(limpia["cameras"]))
        self._avisar()
        return True

    def _avisar(self) -> None:
        if self.al_cambiar:
            try:
                self.al_cambiar(self)
            except Exception:       # noqa: BLE001
                log.exception("falló la aplicación de la configuración")

    def _guardar(self, limpia: dict) -> None:
        """Atómico y solo para el dueño (0600): no queda un archivo a medias ni legible por otros."""
        temporal = self.ruta_cache + ".tmp"
        try:
            fd = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(limpia, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, self.ruta_cache)
        except OSError as e:
            log.warning("no pude guardar la copia de la configuración (%s)", e.__class__.__name__)
