"""
EnviadorClips: manda al servidor los clips que dejó el Grabador, del más viejo al más nuevo, y los borra al confirmarse.

    el servidor lo recibe (o ya lo tenía)          se borra
    el evento todavía no llegó al servidor (409)   se reintenta más tarde
    no hay red, el servidor falla o el token        se reintenta con una espera que crece (hasta 60 s)
    el servidor lo rechaza (plan sin clips, etc.)   se borra y se dice en el registro
    lleva más de MAX_EDAD_S sin poder salir         se borra (ya no le sirve a nadie)
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .cliente import ErrorTransitorio, NoAutorizado, Rechazado
from .grabador import borrar, pendientes

log = logging.getLogger("agent.enviador_clips")
MAX_EDAD_S = 6 * 3600


class EnviadorClips(threading.Thread):
    def __init__(self, cliente, dir_clips: str, base_s: float = 2.0, tope_s: float = 60.0, reloj=time.time):
        super().__init__(name="enviador-clips", daemon=True)
        self.cliente, self.dir, self.base_s, self.tope_s, self._reloj = cliente, dir_clips, base_s, tope_s, reloj
        self._parar = threading.Event()
        self._espera: dict[str, float] = {}       # ruta -> cuándo se puede volver a intentar
        self._fallos: dict[str, int] = {}
        self.enviados = 0
        self.rechazados = 0

    def detener(self) -> None:
        self._parar.set()

    def run(self) -> None:
        while not self._parar.is_set():
            try:
                self.pasada()
            except Exception:       # noqa: BLE001
                log.exception("error al enviar clips")
            self._parar.wait(2.0)

    def pasada(self) -> int:
        """Intenta enviar los clips que ya les toca. Devuelve cuántos salieron."""
        salieron = 0
        ahora = self._reloj()
        for ruta, ficha in pendientes(self.dir):
            if self._parar.is_set():
                break
            try:
                edad = ahora - os.path.getmtime(ruta)
            except OSError:
                continue
            if edad > MAX_EDAD_S:
                borrar(ruta); self._olvidar(ruta)
                log.warning("se descartó un clip que no pudo salir en %d horas", MAX_EDAD_S // 3600)
                continue
            if self._espera.get(ruta, 0) > ahora:
                continue
            eid = ficha.get("edge_event_id")
            if not isinstance(eid, str) or not eid:
                borrar(ruta); continue
            try:
                self.cliente.enviar_clip(eid, ruta, ficha.get("duration_s"))
            except Rechazado as e:
                if e.status == 409:            # el evento todavía no llegó: se espera a que salga de la cola de eventos
                    self._reintentar(ruta, ahora)
                    continue
                borrar(ruta); self._olvidar(ruta); self.rechazados += 1
                log.warning("el servidor rechazó un clip y se descartó (%s)", e)
                continue
            except (ErrorTransitorio, NoAutorizado) as e:
                self._reintentar(ruta, ahora)
                log.info("no se pudo enviar un clip (%s); se reintenta", e)
                break                           # sin red: no tiene sentido probar con los demás ahora
            borrar(ruta); self._olvidar(ruta)
            self.enviados += 1
            salieron += 1
        return salieron

    def _reintentar(self, ruta: str, ahora: float) -> None:
        n = self._fallos.get(ruta, 0) + 1
        self._fallos[ruta] = n
        self._espera[ruta] = ahora + min(self.tope_s, self.base_s * (2 ** (n - 1)))

    def _olvidar(self, ruta: str) -> None:
        self._espera.pop(ruta, None)
        self._fallos.pop(ruta, None)

    def estado(self) -> dict:
        return {"pendientes": len(pendientes(self.dir)), "enviados": self.enviados, "rechazados": self.rechazados}
