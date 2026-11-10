"""Inventario de cámaras y vigilante de conexión en caliente.

El inventario es el CONTRATO con el servidor (H4: POST /edge/inventory). Su forma:
  {"agent_version": "...", "generated_at": "...", "sources": [ {device_key, kind, label,
    state, caps, last_seen, address?, detail?}, ... ]}
No incluye nunca la URL de apertura, que puede llevar usuario y contraseña.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .sources.base import Descubridor, SourceInfo, ahora_iso

log = logging.getLogger("agent.inventory")


@dataclass
class Evento:
    tipo: str            # aparecio | desaparecio | reaparecio | cambio
    fuente: SourceInfo


class Inventario:
    """Guarda las cámaras conocidas y detecta cuándo aparecen o desaparecen.

    `tolerancia`: cuántos sondeos seguidos puede faltar una cámara antes de darla por
    desconectada. El descubrimiento ONVIF usa UDP, que pierde mensajes: pide 2.
    """

    def __init__(self, tolerancia: dict[str, int] | None = None):
        self._lock = threading.RLock()
        self._fuentes: dict[str, SourceInfo] = {}
        self._ausencias: dict[str, int] = {}
        self._tolerancia = tolerancia or {"onvif": 2}

    def actualizar(self, tipo: str, vistas: list[SourceInfo]) -> list[Evento]:
        eventos: list[Evento] = []
        with self._lock:
            vistas_por_clave = {v.device_key: v for v in vistas}

            for clave, nueva in vistas_por_clave.items():
                self._ausencias.pop(clave, None)
                anterior = self._fuentes.get(clave)
                if anterior is None:
                    self._fuentes[clave] = nueva
                    eventos.append(Evento("aparecio", copy.copy(nueva)))
                    continue
                cambio = (anterior.state, anterior.caps, anterior.label, anterior.detail) != \
                         (nueva.state, nueva.caps, nueva.label, nueva.detail)
                reaparece = anterior.state == "disconnected" and nueva.state != "disconnected"
                if nueva.state == "disconnected":
                    nueva.last_seen = anterior.last_seen    # conserva cuándo se vio por última vez
                self._fuentes[clave] = nueva
                if reaparece:
                    eventos.append(Evento("reaparecio", copy.copy(nueva)))
                elif cambio:
                    eventos.append(Evento("cambio", copy.copy(nueva)))

            for clave, fuente in list(self._fuentes.items()):
                if fuente.kind != tipo or clave in vistas_por_clave or fuente.state == "disconnected":
                    continue
                n = self._ausencias.get(clave, 0) + 1
                self._ausencias[clave] = n
                if n >= self._tolerancia.get(tipo, 1):
                    fuente.state = "disconnected"
                    fuente.detail = "ya no se detecta"
                    eventos.append(Evento("desaparecio", copy.copy(fuente)))
        return eventos

    def fuente(self, clave: str) -> SourceInfo | None:
        with self._lock:
            f = self._fuentes.get(clave)
            return copy.copy(f) if f else None

    def fuentes(self) -> list[SourceInfo]:
        with self._lock:
            return sorted((copy.copy(f) for f in self._fuentes.values()), key=lambda f: (f.kind, f.device_key))

    def snapshot(self) -> dict:
        return {
            "agent_version": __version__,
            "generated_at": ahora_iso(),
            "sources": [f.to_dict() for f in self.fuentes()],
        }


def sondear_una_vez(inventario: Inventario, descubridores: list[Descubridor]) -> list[Evento]:
    """Ejecuta cada descubridor una vez (lo usa `listar`). Un fallo en uno no detiene a los demás."""
    eventos = []
    for d in descubridores:
        try:
            eventos += inventario.actualizar(d.tipo, d.funcion())
        except Exception as e:      # noqa: BLE001 - un descubridor roto no debe tumbar al agente
            log.warning("falló el descubrimiento '%s': %s", d.tipo, e)
    return eventos


class Vigilante(threading.Thread):
    """Repite cada descubridor a su ritmo y avisa de los cambios (conexión en caliente)."""

    def __init__(self, inventario: Inventario, descubridores: list[Descubridor],
                 al_cambiar: Callable[[list[Evento]], None] | None = None):
        super().__init__(name="vigilante", daemon=True)
        self.inventario = inventario
        self.descubridores = descubridores
        self.al_cambiar = al_cambiar
        self._parar = threading.Event()

    def detener(self) -> None:
        self._parar.set()

    def run(self) -> None:
        proximo = {id(d): 0.0 for d in self.descubridores}
        while not self._parar.is_set():
            for d in self.descubridores:
                if time.monotonic() < proximo[id(d)]:
                    continue
                try:
                    eventos = self.inventario.actualizar(d.tipo, d.funcion())
                except Exception as e:      # noqa: BLE001
                    log.warning("falló el descubrimiento '%s': %s (se conserva lo último conocido)", d.tipo, e)
                    eventos = []
                proximo[id(d)] = time.monotonic() + d.intervalo
                if eventos and self.al_cambiar:
                    try:
                        self.al_cambiar(eventos)
                    except Exception:       # noqa: BLE001
                        log.exception("falló el aviso de cambios")
            self._parar.wait(0.5)
