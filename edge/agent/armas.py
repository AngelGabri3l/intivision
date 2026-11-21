"""
Detector de posibles armas (Plan Premium).

  • Corre solo en las cámaras cuyo plan lo incluye y solo cuando el motor ya vio a una persona (ahorra CPU).
  • Con un modelo entrenado para armas (agent.yaml -> premium.weapons_model, por ejemplo /data/armas.pt) reconoce las clases
    cuyo nombre indica arma de fuego (pistol, gun, rifle...) o arma blanca (knife, machete...).
  • Sin ese modelo, usa la clase «cuchillo» (knife) del modelo general YOLO: detecta armas blancas, no armas de fuego.
  • Siempre es «posible arma»: el aviso pasa por la revisión humana como cualquier otro.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("agent.armas")
FUEGO = ("gun", "pistol", "handgun", "rifle", "firearm", "revolver", "shotgun", "weapon", "pistola", "arma de fuego", "arma")
BLANCA = ("knife", "machete", "blade", "cuchillo", "navaja", "cutter")
COCO_KNIFE = 43
MODELO_GENERAL = "yolo11n.pt"


def tipo_de(nombre: str) -> str | None:
    n = (nombre or "").strip().lower().replace("_", " ").replace("-", " ")
    if any(b in n for b in BLANCA):
        return "arma blanca"
    if any(f in n for f in FUEGO):
        return "arma de fuego"
    return None


class DetectorArmas:
    def __init__(self, modelo: str | None = None, conf: float = 0.45, cargar=None):
        self.conf = conf
        self.ruta = modelo if modelo and os.path.isfile(modelo) else None
        if modelo and not self.ruta:
            log.warning("no encuentro el modelo de armas %s: se usa el modelo general (solo armas blancas)", modelo)
        self.general = self.ruta is None
        cargar = cargar or self._cargar_yolo
        self.modelo = cargar(self.ruta or MODELO_GENERAL)
        nombres = getattr(self.modelo, "names", {}) or {}
        if self.general:
            self.clases = {COCO_KNIFE: "arma blanca"}
        else:
            self.clases = {int(k): t for k, v in dict(nombres).items() if (t := tipo_de(str(v)))}
            if not self.clases:
                log.warning("el modelo de armas no tiene clases reconocibles como armas (%s)", ", ".join(map(str, dict(nombres).values()))[:200])
        log.info("detector de armas listo: %s", "modelo propio" if not self.general else "modelo general (solo armas blancas)")

    @staticmethod
    def _cargar_yolo(ruta):
        from ultralytics import YOLO  # import diferido: el agente puede correr sin el motor
        return YOLO(ruta)

    def detectar(self, cuadro) -> list[dict]:
        """Las posibles armas del cuadro: [{"tipo", "conf", "bbox"}], de la más segura a la menos."""
        if not self.clases:
            return []
        resultados = self.modelo.predict(cuadro, conf=self.conf, classes=list(self.clases), verbose=False)
        salida = []
        for r in resultados:
            for box in getattr(r, "boxes", []) or []:
                clase = int(box.cls[0]); conf = float(box.conf[0])
                if clase in self.clases and conf >= self.conf:
                    x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                    salida.append({"tipo": self.clases[clase], "conf": round(conf, 3), "bbox": (x1, y1, x2, y2)})
        return sorted(salida, key=lambda d: -d["conf"])


class ConfirmacionArmas:
    """Para no avisar por un solo cuadro: hace falta ver el arma en 2 de los últimos 3 análisis, y hay una espera entre avisos."""

    def __init__(self, ventana: int = 3, minimo: int = 2, espera_s: float = 30.0):
        self.ventana, self.minimo, self.espera_s = ventana, minimo, espera_s
        self._vistos: list[bool] = []
        self._ultimo_aviso = None

    def registrar(self, hay_arma: bool, ahora: float) -> bool:
        """True si este análisis confirma un aviso de arma."""
        self._vistos = (self._vistos + [bool(hay_arma)])[-self.ventana:]
        if sum(self._vistos) < self.minimo or not hay_arma:
            return False
        if self._ultimo_aviso is not None and ahora - self._ultimo_aviso < self.espera_s:
            return False
        self._ultimo_aviso = ahora
        self._vistos = []
        return True
