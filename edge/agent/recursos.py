"""
Recursos de la mini PC (H13): cuánta CPU y memoria usa el equipo. Un hilo toma una muestra cada pocos segundos y guarda las de los últimos
5 minutos; el latido manda al servidor el promedio y el pico de CPU y la memoria en uso, y «Equipos y cámaras» lo muestra. Así se ve el
uso real, con las cámaras reales.

Si `psutil` no está instalado el agente sigue funcionando igual, solo que sin reportar recursos.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque

log = logging.getLogger("agent.recursos")
MUESTREO_CADA_S = 5.0        # cada cuánto se mide (un pico más corto que esto no se ve)
VENTANA_S = 300.0            # la ventana del promedio y del pico: los últimos 5 minutos
MB = 1024 * 1024


def _leer_psutil():
    """(CPU % del equipo desde la lectura anterior, memoria en uso en MB, memoria total en MB)."""
    import psutil
    mem = psutil.virtual_memory()
    return psutil.cpu_percent(interval=None), (mem.total - mem.available) / MB, mem.total / MB


class MuestreadorRecursos(threading.Thread):
    def __init__(self, cada_s: float = MUESTREO_CADA_S, ventana_s: float = VENTANA_S, leer=None, reloj=time.monotonic):
        super().__init__(name="recursos", daemon=True)
        self.cada_s, self.ventana_s, self._reloj = cada_s, ventana_s, reloj
        self._muestras: deque = deque()          # (instante, CPU %)
        self._memoria = None                     # (usada MB, total MB) de la última muestra
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._avisado = False
        self.disponible = True
        self._leer = leer
        if leer is None:
            try:
                import psutil
                psutil.cpu_percent(interval=None)    # la primera lectura siempre da 0: se descarta
                self._leer = _leer_psutil
            except Exception:                        # noqa: BLE001 - sin psutil el agente sigue, sin reportar recursos
                self.disponible = False
                log.warning("no se pudo cargar psutil: esta mini PC no reportará su uso de CPU y memoria")

    def muestrear(self) -> None:
        """Una muestra. Un error o un valor imposible se ignora: medir nunca debe tirar al agente."""
        if not self.disponible:
            return
        try:
            cpu, usada, total = (float(x) for x in self._leer())
        except Exception:                            # noqa: BLE001
            if not self._avisado:
                self._avisado = True
                log.warning("no se pudo leer el uso de CPU y memoria", exc_info=True)
            return
        if not all(math.isfinite(x) for x in (cpu, usada, total)) or total <= 0 or usada < 0:
            return
        ahora = self._reloj()
        with self._lock:
            self._muestras.append((ahora, min(100.0, max(0.0, cpu))))
            self._memoria = (min(usada, total), total)
            corte = ahora - self.ventana_s
            while self._muestras and self._muestras[0][0] < corte:
                self._muestras.popleft()

    def resumen(self) -> dict | None:
        """Lo que se manda al servidor, o None si no hay muestras recientes (todavía no midió, o el hilo se detuvo)."""
        with self._lock:
            if not self._muestras or self._memoria is None:
                return None
            ahora = self._reloj()
            if ahora - self._muestras[-1][0] > max(3 * self.cada_s, 30.0):
                return None
            cpus = [c for _, c in self._muestras]
            usada, total = self._memoria
            cubierto = self._muestras[-1][0] - self._muestras[0][0] + self.cada_s
        return {"window_s": int(round(min(self.ventana_s, max(1.0, cubierto)))), "cpu_avg": round(sum(cpus) / len(cpus), 1),
                "cpu_peak": round(max(cpus), 1), "mem_used_mb": int(round(usada)), "mem_total_mb": int(round(total))}

    def run(self) -> None:
        if not self.disponible:
            return
        while not self._parar.is_set():
            self.muestrear()
            self._parar.wait(self.cada_s)

    def detener(self) -> None:
        self._parar.set()
