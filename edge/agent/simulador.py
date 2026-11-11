"""
Eventos de prueba: sirve para ver todo el recorrido (cola, envío, servidor, Telegram) SIN esperar a que el motor de detección
vea algo. Cada cierto tiempo, por cada cámara asignada, mete un evento en la cola con una foto de lo que ve la cámara en ese
momento (con la marca «EVENTO DE PRUEBA»).

Por defecto los eventos son de nivel BAJO: aparecen en el historial pero NO mandan aviso por Telegram.
"""
from __future__ import annotations

import datetime
import logging
import threading

import cv2

from .evidencia import codificar_jpeg, imagen_de_prueba

log = logging.getLogger("agent.simulador")
PUNTAJE = {"BAJO": 1, "MEDIO": 4, "ALTO": 7}


def evento_de_prueba(camera_id: str, nivel: str = "BAJO") -> dict:
    return {"camera_id": camera_id, "level": nivel, "score": PUNTAJE[nivel], "people": 1,
            "reasons": "persona detectada", "occurred_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def foto_de_prueba(cuadro) -> bytes:
    """Un cuadro real de la cámara con la marca de prueba; si no hay cuadro, una imagen sintética."""
    hora = datetime.datetime.now().strftime("%H:%M:%S")
    if cuadro is None:
        return imagen_de_prueba(f"EVENTO DE PRUEBA {hora}")
    marcado = cuadro.copy()
    cv2.putText(marcado, f"EVENTO DE PRUEBA {hora}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return codificar_jpeg(marcado)


class SimuladorEventos(threading.Thread):
    def __init__(self, cola, config, gestor, enviador, cada_s: float, nivel: str = "BAJO"):
        super().__init__(name="simulador", daemon=True)
        self.cola, self.config, self.gestor, self.enviador = cola, config, gestor, enviador
        self.cada_s, self.nivel = cada_s, nivel
        self._parar = threading.Event()
        self.generados = 0

    def detener(self) -> None:
        self._parar.set()

    def generar(self) -> int:
        """Un evento de prueba por cada cámara asignada. Devuelve cuántos."""
        n = 0
        for cam in self.config.camaras():
            cuadro, _ = self.gestor.ultimo(cam["device_key"]) if self.gestor else (None, 0.0)
            try:
                self.cola.agregar(evento_de_prueba(cam["camera_id"], self.nivel), foto_de_prueba(cuadro))
            except (ValueError, OSError) as e:
                log.warning("no se pudo crear un evento de prueba (%s)", e)
                continue
            n += 1
        self.generados += n
        if n:
            self.enviador.despertar()
        return n

    def run(self) -> None:
        while not self._parar.wait(self.cada_s):
            self.generar()
