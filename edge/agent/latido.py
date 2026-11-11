"""
Latido: cada pocos segundos (por defecto 30) el agente le dice al servidor «sigo vivo, estas son mis cámaras» y le pregunta
«¿qué cámaras debo analizar?».

El inventario hace de latido: con cada envío el servidor marca activas las cámaras que están disponibles y guarda la versión del
agente. Si el agente deja de latir (se apagó o se cayó el internet), el servidor se da cuenta porque esas cámaras dejan de
refrescarse, y a los 10 minutos avisa por Telegram que perdieron señal.
"""
from __future__ import annotations

import logging
import threading
import time

from .cliente import ClienteServidor, ErrorTransitorio, NoAutorizado, Rechazado
from .config_remota import ConfigRemota
from .inventory import Inventario

log = logging.getLogger("agent.latido")
AVISO_CADA_S = 300


class Latido(threading.Thread):
    def __init__(self, cliente: ClienteServidor, inventario: Inventario, config: ConfigRemota, cola=None,
                 cada_s: float = 30.0, espera_error_s: float = 10.0):
        super().__init__(name="latido", daemon=True)
        self.cliente, self.inventario, self.config, self.cola = cliente, inventario, config, cola
        self.cada_s, self.espera_error_s = cada_s, espera_error_s
        self._parar, self._despertar = threading.Event(), threading.Event()
        self.ultimo_ok: float | None = None
        self.ultimo_error: str | None = None
        self.ciclos = 0
        self._avisos: dict[str, float] = {}

    def despertar(self) -> None:
        """Pide un latido ahora (cambió una cámara, o el servidor dijo que una cámara ya no está asignada)."""
        self._despertar.set()

    def detener(self) -> None:
        self._parar.set()
        self._despertar.set()

    def run(self) -> None:
        while not self._parar.is_set():
            bien = self.ciclo()
            self._despertar.wait(self.cada_s if bien else min(self.cada_s, self.espera_error_s))
            self._despertar.clear()

    def ciclo(self) -> bool:
        """Un latido completo: manda el inventario y pide la configuración. True si las dos cosas salieron bien."""
        self.ciclos += 1
        bien = self._intentar("el inventario", self._mandar_inventario)
        bien = self._intentar("la configuración", lambda: self.config.aplicar(self.cliente.obtener_config())) and bien
        if bien:
            self.ultimo_ok, self.ultimo_error = time.time(), None
        return bien

    def _mandar_inventario(self) -> None:
        instantanea = self.inventario.snapshot()
        if self.cola is not None:
            e = self.cola.estadisticas()
            instantanea["queue"] = {"pending": e["pendientes"], "oldest_age_s": e["mas_viejo_s"], "dropped": e["descartados"]}
        self.cliente.enviar_inventario(instantanea)

    def _intentar(self, que: str, accion) -> bool:
        try:
            accion()
            return True
        except NoAutorizado:
            self._decir(f"token:{que}", logging.WARNING, "el servidor no acepta el token de esta mini PC (¿se revocó?): no se pudo enviar el inventario ni pedir la configuración")
        except Rechazado as e:
            self._decir(f"rechazo:{que}", logging.WARNING, "el servidor rechazó %s (%s)", que, e)
        except ErrorTransitorio as e:
            self._decir(f"red:{que}", logging.INFO, "no se pudo hablar con el servidor para %s (%s)", que, e)
        except Exception:       # noqa: BLE001
            log.exception("error inesperado en el latido")
        self.ultimo_error = que
        return False

    def _decir(self, clave: str, nivel: int, mensaje: str, *args) -> None:
        """Un mismo aviso, como mucho cada 5 minutos: si el internet se cae una hora, no se escriben 120 líneas iguales."""
        ahora = time.monotonic()
        if ahora - self._avisos.get(clave, -AVISO_CADA_S - 1) >= AVISO_CADA_S:
            self._avisos[clave] = ahora
            log.log(nivel, mensaje, *args)
