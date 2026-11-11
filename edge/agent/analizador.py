"""
Analizador: la detección dentro del agente.

Toma el último cuadro de cada cámara asignada, lo pasa por el motor de VIGÍA (YOLO de personas + postura + puntaje de amenaza,
los mismos módulos que usa el servidor con las cámaras web) y, cuando el puntaje llega a MEDIO o ALTO, mete un evento con su foto
en la cola de envío.

Cómo funciona:
  • UNA sesión del motor por cámara (cada una mide su propio merodeo y su propia persistencia).
  • Un turno cada 1/fps segundos: en cada turno se analiza el ÚLTIMO cuadro de cada cámara. Nunca se acumulan cuadros: si el
    motor es lento, simplemente se analizan menos por segundo (y el registro dice cuántos).
  • Cada cuadro se lleva a 640 px de ancho antes de analizarlo: las zonas restringidas se dibujan en esa medida.
  • La zona restringida y el horario sensible los decide el servidor (GET /edge/config), cámara por cámara.
  • Si no se puede cargar el motor (sin PyTorch, sin los pesos), el agente SIGUE funcionando en modo «solo cámaras» y lo dice.

No modifica ningún archivo del motor (engine.py, threat_scoring.py...): los usa tal como están.
"""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import yaml

from .evidencia import MAX_BYTES, codificar_jpeg

log = logging.getLogger("agent.analizador")

ANCHO_ANALISIS = 640
EDAD_MAX_CUADRO_S = 2.0          # un cuadro más viejo que esto no se analiza: la cámara está congelada
NIVELES = ("BAJO", "MEDIO", "ALTO")
AVISO_CADA_S = 60


class MotorNoDisponible(Exception):
    """El motor de detección no se pudo cargar. El mensaje explica por qué, pensado para quien instala."""


# ------------------------------------------------------------------------------------------ cargar el motor
def _directorios_candidatos():
    if os.environ.get("ENGINE_DIR"):
        yield os.environ["ENGINE_DIR"]
    yield "/app"                                         # dentro de la imagen de Docker
    yield str(Path(__file__).resolve().parents[2])      # en el repositorio: edge/agent/analizador.py -> la raíz del proyecto


def buscar_dir_motor() -> str:
    visitados = []
    for d in _directorios_candidatos():
        visitados.append(d)
        if os.path.isfile(os.path.join(d, "engine.py")):
            return d
    raise MotorNoDisponible("no encuentro engine.py (busqué en: " + ", ".join(visitados) + ")")


def cargar_motor(ruta_config: str | None = None):
    """El GuardianEngine, listo para crear sesiones. Lanza MotorNoDisponible si no se puede."""
    dir_motor = buscar_dir_motor()
    if dir_motor not in sys.path:
        sys.path.insert(0, dir_motor)
    ruta = ruta_config or os.environ.get("ENGINE_CONFIG") or os.path.join(dir_motor, "config.yaml")
    try:
        with open(ruta, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise MotorNoDisponible(f"no pude leer la configuración del motor ({ruta}): {e.__class__.__name__}") from None
    if not isinstance(cfg, dict):
        raise MotorNoDisponible(f"la configuración del motor ({ruta}) debe ser un diccionario YAML")
    try:
        from engine import GuardianEngine
        return GuardianEngine(cfg)
    except ImportError as e:
        raise MotorNoDisponible(f"falta una librería del motor ({e.name or e}). ¿La imagen se construyó con MOTOR=0? "
                                "Reconstruye con MOTOR=1 (docker compose build)") from None
    except Exception as e:      # noqa: BLE001 - los pesos que no se pueden bajar, un modelo dañado...
        raise MotorNoDisponible(f"no se pudo cargar el motor: {e.__class__.__name__}: {str(e)[:200]}") from None


# ------------------------------------------------------------------------------------------ una cámara
class SesionCamara:
    """La sesión del motor de UNA cámara, con su zona y su horario, y las cuentas de lo que ha pasado."""

    def __init__(self, motor, dir_evidencia: str, zona=None, horas=None):
        self.motor, self.dir = motor, dir_evidencia
        self.zona = [list(p) for p in zona] if zona else None
        self.horas = list(horas) if horas else None
        self.sesion = None
        self.ultimo_instante: float | None = None       # del último cuadro analizado (para no analizar el mismo dos veces)
        self.ultimo_analisis: float | None = None       # cuándo se analizó (para detectar que la cámara estuvo caída)
        self.errores = 0
        self.analizados = 0
        self.eventos = 0
        self.ms_total = 0.0
        self.nivel, self.personas, self.puntaje, self.motivos = "-", 0, 0, []
        self._crear()

    @staticmethod
    def firma_de(zona, horas):
        """Lo que identifica la configuración de una sesión: si cambia la zona o el horario, hay que rehacerla."""
        return (tuple(tuple(p) for p in zona) if zona else None, tuple(horas) if horas else None)

    def firma(self):
        return self.firma_de(self.zona, self.horas)

    def _crear(self) -> None:
        sesion = self.motor.new_session(self.dir)
        if self.zona or self.horas:
            try:
                kw = dict(self.motor._scorer_kwargs)
                if self.zona:
                    kw["restricted_zone"] = self.zona
                if self.horas:
                    kw["sensitive_hours"] = self.horas
                sesion.scorer = type(sesion.scorer)(**kw)
            except (AttributeError, TypeError) as e:
                log.warning("este motor no permite zona ni horario por cámara (%s): se usan los de su configuración", e.__class__.__name__)
        self.sesion = sesion

    def reiniciar(self) -> None:
        """Una sesión nueva: se olvida el merodeo y la persistencia acumulados."""
        self._crear()

    def procesar(self, cuadro) -> dict:
        """Analiza un cuadro (llevado antes a 640 px de ancho) y devuelve lo que responde el motor."""
        alto, ancho = cuadro.shape[:2]
        if ancho != ANCHO_ANALISIS:
            cuadro = cv2.resize(cuadro, (ANCHO_ANALISIS, max(16, round(alto * ANCHO_ANALISIS / ancho))),
                                interpolation=cv2.INTER_AREA if ancho > ANCHO_ANALISIS else cv2.INTER_LINEAR)
        t0 = time.perf_counter()
        res = self.sesion.process(cuadro)
        self.ms_total += (time.perf_counter() - t0) * 1000
        self.analizados += 1
        if isinstance(res, dict):
            self.nivel = res.get("level", "-")
            self.personas = res.get("people", 0)
            self.puntaje = res.get("score", 0)
            self.motivos = list(res.get("reasons") or [])
        return res

    def ms_promedio(self) -> float:
        return self.ms_total / self.analizados if self.analizados else 0.0


def evento_a_payload(camera_id: str, ev: dict) -> dict | None:
    """El evento del motor convertido al formato de POST /ingest. None si no tiene lo mínimo."""
    if not isinstance(ev, dict) or ev.get("level") not in NIVELES:
        return None
    try:
        score = max(0, min(100, int(ev.get("score") or 0)))
        personas = max(0, min(100, int(ev.get("people") or 0)))
    except (TypeError, ValueError):
        return None
    motivos = ev.get("reasons")
    if isinstance(motivos, (list, tuple)):
        motivos = ", ".join(str(m) for m in motivos)
    return {"camera_id": camera_id, "level": ev["level"], "score": score, "people": personas,
            "reasons": str(motivos or "")[:500],
            "occurred_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def foto_del_evento(ruta: str) -> bytes | None:
    """Lee la foto que dejó el motor, la deja por debajo del tope del servidor y BORRA el archivo."""
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
    except OSError:
        return None
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass
    if len(datos) > MAX_BYTES:
        try:
            datos = codificar_jpeg(cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR))
        except ValueError:
            return None
    return datos


# ------------------------------------------------------------------------------------------ el hilo
class Analizador(threading.Thread):
    def __init__(self, cargar, gestor, config, cola, enviador, dir_tmp: str, fps: float = 3.0, cooldown_s: float = 10.0,
                 max_gap_s: float = 10.0, reloj=time.monotonic):
        super().__init__(name="analizador", daemon=True)
        self._cargar = cargar                    # función sin argumentos: devuelve el motor o lanza MotorNoDisponible
        self.gestor, self.config, self.cola, self.enviador = gestor, config, cola, enviador
        self.dir_tmp, self.fps, self.cooldown_s, self.max_gap_s = dir_tmp, fps, cooldown_s, max_gap_s
        self._reloj = reloj
        self.motor = None
        self.estado_motor = "cargando"           # cargando | listo | no disponible
        self.motivo_motor: str | None = None
        self._sesiones: dict[str, SesionCamara] = {}
        self._tiempos: deque = deque()           # cuándo se analizó cada cuadro (para los cuadros por segundo reales)
        self._avisos: dict[str, float] = {}
        self._parar = threading.Event()
        self.errores_total = 0

    def detener(self) -> None:
        self._parar.set()

    # --- el ciclo
    def run(self) -> None:
        try:
            self.motor = self._cargar()
        except MotorNoDisponible as e:
            self._sin_motor(str(e))
            return
        except Exception as e:      # noqa: BLE001
            self._sin_motor(f"{e.__class__.__name__}: {str(e)[:200]}")
            return
        if self.cooldown_s:
            self.motor.event_cooldown_s = self.cooldown_s
        shutil.rmtree(self.dir_tmp, ignore_errors=True)         # restos de una ejecución anterior
        os.makedirs(self.dir_tmp, exist_ok=True)
        self.estado_motor = "listo"
        log.info("motor de detección listo: se analiza cada cámara asignada a ~%g cuadros por segundo", self.fps)
        periodo = 1.0 / self.fps
        while not self._parar.is_set():
            t0 = self._reloj()
            self.pasada()
            self._parar.wait(max(0.0, periodo - (self._reloj() - t0)))

    def _sin_motor(self, motivo: str) -> None:
        self.estado_motor, self.motivo_motor = "no disponible", motivo
        log.warning("detección desactivada: %s. El agente sigue en modo «solo cámaras» (lee, manda su inventario y recibe su configuración)", motivo)

    def pasada(self) -> int:
        """Un turno: analiza una vez cada cámara asignada. Devuelve cuántos cuadros analizó."""
        vivas = {c["camera_id"]: c for c in self.config.camaras()}
        for cid in [k for k in self._sesiones if k not in vivas]:
            del self._sesiones[cid]
            log.info("se dejó de analizar una cámara (ya no está asignada)")
        analizados = 0
        for cid, cam in vivas.items():
            try:
                analizados += self._analizar(cid, cam)
            except Exception:       # noqa: BLE001 - una cámara con problemas no debe detener a las demás
                self.errores_total += 1
                st = self._sesiones.get(cid)
                if st:
                    st.errores += 1
                    if st.errores >= 20:                    # algo quedó mal dentro de la sesión: se empieza de cero
                        st.errores = 0
                        st.reiniciar()
                self._decir(f"error:{cid}", "falló el análisis de un cuadro", exc=True)
        return analizados

    def _analizar(self, cid: str, cam: dict) -> int:
        zona, horas = cam.get("restricted_zone"), self.config.horario_sensible()
        st = self._sesiones.get(cid)
        if st is None or st.firma() != SesionCamara.firma_de(zona, horas):
            st = SesionCamara(self.motor, os.path.join(self.dir_tmp, cid[:8]), zona, horas)
            self._sesiones[cid] = st
            log.info("análisis iniciado para la cámara «%s» (%s, %s)", cam.get("name") or cid[:8],
                     "con zona restringida" if zona else "sin zona", f"horario sensible {horas[0]}:00 a {horas[1]}:00" if horas else "horario del motor")
        cuadro, instante = self.gestor.ultimo(cam["device_key"])
        if cuadro is None or instante == st.ultimo_instante:
            return 0                                        # sin cuadro, o el mismo que ya se analizó
        ahora = self._reloj()
        if ahora - instante > EDAD_MAX_CUADRO_S:
            return 0                                        # cámara congelada: no se analiza una imagen vieja
        if st.ultimo_analisis is not None and ahora - st.ultimo_analisis > self.max_gap_s:
            st.reiniciar()                                  # estuvo sin cuadros: no se arrastra el merodeo de antes
            log.info("la cámara «%s» estuvo %.0f s sin cuadros: se reinicia su análisis", cam.get("name") or cid[:8], ahora - st.ultimo_analisis)
        st.ultimo_instante = instante
        res = st.procesar(cuadro)
        st.ultimo_analisis = self._reloj()
        st.errores = 0
        self._tiempos.append(st.ultimo_analisis)
        ev = res.get("event") if isinstance(res, dict) else None
        if ev:
            self._encolar(cid, st, ev)
        return 1

    def _encolar(self, cid: str, st: SesionCamara, ev: dict) -> None:
        payload = evento_a_payload(cid, ev)
        ruta = os.path.join(st.dir, os.path.basename(str(ev.get("image") or "sin_foto")))
        foto = foto_del_evento(ruta)
        if payload is None:
            self._decir("evento_raro", "el motor devolvió un evento que no se entiende: se ignora")
            return
        try:
            self.cola.agregar(payload, foto)
        except Exception:       # noqa: BLE001 - disco lleno, base bloqueada...
            self._decir("cola", "no se pudo guardar un evento en la cola", exc=True)
            return
        st.eventos += 1
        log.info("EVENTO %s (puntaje %s): %s", payload["level"], payload["score"], payload["reasons"])
        self.enviador.despertar()

    def _decir(self, clave: str, mensaje: str, exc: bool = False) -> None:
        ahora = time.monotonic()
        if ahora - self._avisos.get(clave, -AVISO_CADA_S - 1) >= AVISO_CADA_S:
            self._avisos[clave] = ahora
            log.warning(mensaje, exc_info=exc)

    # --- lo que muestra `vigilar`
    def estado(self) -> dict:
        ahora = self._reloj()
        while self._tiempos and self._tiempos[0] < ahora - 30:
            self._tiempos.popleft()
        ventana = min(30.0, max(1.0, ahora - self._tiempos[0])) if self._tiempos else 30.0
        return {"motor": self.estado_motor, "motivo": self.motivo_motor, "cuadros_por_s": round(len(self._tiempos) / ventana, 1),
                "camaras": [{"nombre": nombre, "nivel": st.nivel, "personas": st.personas, "puntaje": st.puntaje, "ms": round(st.ms_promedio()),
                             "analizados": st.analizados, "eventos": st.eventos} for nombre, st in self._nombres()]}

    def _nombres(self):
        nombres = {c["camera_id"]: c.get("name") or c["camera_id"][:8] for c in self.config.camaras()}
        return [(nombres.get(cid, cid[:8]), st) for cid, st in self._sesiones.items()]
