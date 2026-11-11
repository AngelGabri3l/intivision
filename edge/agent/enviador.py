"""
Enviador: saca los eventos de la cola, de a uno y en orden, y los manda al servidor.

Qué hace según lo que pase con el primero de la cola:
    el servidor lo recibe (incluido un reenvío)   sale de la cola
    no hay red, el servidor falla o está ocupado  se queda, y se reintenta con una espera que crece: 1, 2, 4... hasta 60 s
    el token es inválido o fue revocado           se queda (NO se pierde nada) y se reintenta cada ~60 s
    el servidor lo rechaza (foto inválida, etc.)  se aparta en «rechazados» para no bloquear a los demás

Mientras el primero espera su turno, los demás NO se saltan la fila: así se conserva el orden.
"""
from __future__ import annotations

import logging
import random
import threading
import time

from .cliente import ClienteServidor, ErrorTransitorio, NoAutorizado, Rechazado
from .cola import ColaLocal, Item

log = logging.getLogger("agent.enviador")
AVISO_CADA_S = 300              # para no llenar los registros: un aviso repetido se dice como mucho cada 5 minutos


class Enviador(threading.Thread):
    def __init__(self, cola: ColaLocal, cliente: ClienteServidor, al_perder_camara=None,
                 base_s: float = 1.0, tope_s: float = 60.0, espera_401_s: float = 60.0, jitter: float = 0.2, azar=None):
        super().__init__(name="enviador", daemon=True)
        self.cola, self.cliente, self.al_perder_camara = cola, cliente, al_perder_camara
        self.base_s, self.tope_s, self.espera_401_s, self.jitter = base_s, tope_s, espera_401_s, jitter
        self._azar = azar or random.Random()
        self._parar, self._despertar = threading.Event(), threading.Event()
        self.fallos = 0                  # fallos seguidos (vuelve a 0 con el primer envío que sale bien)
        self.enviados = 0
        self.rechazados = 0
        self.ultimo_ok: float | None = None
        self.ultimo_error: str | None = None
        self._ultimo_aviso = 0.0

    def despertar(self) -> None:
        """Hay un evento nuevo en la cola: que se intente enviar ya."""
        self._despertar.set()

    def detener(self) -> None:
        self._parar.set()
        self._despertar.set()

    def estado(self) -> dict:
        return {"fallos_seguidos": self.fallos, "enviados": self.enviados, "rechazados": self.rechazados,
                "ultimo_ok": self.ultimo_ok, "ultimo_error": self.ultimo_error}

    def run(self) -> None:
        while not self._parar.is_set():
            item = self.cola.siguiente()
            if item is None:
                falta = self.cola.segundos_hasta_el_proximo()
                self._esperar(1.0 if falta is None else min(1.0, max(falta, 0.05)))
                continue
            self.enviar_uno(item)

    def _esperar(self, segundos: float) -> None:
        self._despertar.wait(segundos)
        self._despertar.clear()

    def espera_para(self, fallos: int) -> float:
        """1, 2, 4, 8... segundos, con tope, y una variación al azar para que varias mini PC no reintenten a la vez."""
        base = min(self.tope_s, self.base_s * (2 ** max(0, fallos - 1)))
        return base * (1 + self._azar.uniform(-self.jitter, self.jitter))

    def enviar_uno(self, item: Item) -> None:
        try:
            self.cliente.enviar_evento(item.payload, item.foto_ruta)
        except NoAutorizado as e:
            self._fallar(item, max(self.espera_401_s, 0.0), str(e), "el servidor no acepta el token de esta mini PC (¿se revocó?); "
                         "los eventos quedan guardados en la cola", grave=True)
        except ErrorTransitorio as e:
            self._fallar(item, self.espera_para(self.fallos + 1), str(e), None)
        except Rechazado as e:
            self.cola.rechazar(item.id, str(e))
            self.rechazados += 1
            log.warning("el servidor rechazó un evento y se apartó (%s)", e)
            if e.status == 422 and "asignada" in e.mensaje.lower() and self.al_perder_camara:
                self.al_perder_camara()
        except Exception as e:      # noqa: BLE001 - un error raro no debe matar al enviador
            log.exception("error inesperado al enviar un evento")
            self._fallar(item, self.espera_para(self.fallos + 1), f"error inesperado ({e.__class__.__name__})", None)
        else:
            self.cola.confirmar(item.id)
            self.enviados += 1
            self.ultimo_ok, self.ultimo_error = time.time(), None
            if self.fallos:
                log.info("se restableció la conexión con el servidor; se siguen enviando los eventos pendientes")
            self.fallos = 0

    def _fallar(self, item: Item, espera: float, motivo: str, mensaje: str | None, grave: bool = False) -> None:
        self.fallos += 1
        self.ultimo_error = motivo
        self.cola.reintentar_mas_tarde(item.id, espera)
        ahora = time.monotonic()
        if self.fallos == 1 or ahora - self._ultimo_aviso >= AVISO_CADA_S:
            self._ultimo_aviso = ahora
            pendientes = self.cola.estadisticas()["pendientes"]
            (log.warning if grave or self.fallos == 1 else log.info)(
                "%s; %d evento(s) esperan en la cola; se reintenta en ~%.0f s", mensaje or f"no se pudo enviar ({motivo})", pendientes, espera)
