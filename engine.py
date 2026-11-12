"""
engine.py — Pipeline de detección reutilizable.

Reúne detector de personas + pose + puntaje de amenaza en un solo objeto, y
ofrece analyze_video() para procesar un VIDEO SUBIDO (no cámara en vivo). Lo usa el
backend. Así la misma inteligencia sirve en vivo y para análisis bajo demanda.
"""
import os
import time
import threading
import datetime

import cv2

from detector import PersonDetector
from pose_detector import PoseDetector
from threat_scoring import ThreatScorer

DEFAULT_WEIGHTS = {"person_present": 1, "in_restricted_zone": 3, "sensitive_hour": 2,
                   "loitering": 2, "group": 2, "proximity": 1, "climbing": 3, "fallen": 3,
                   "hands_up": 3}


def scorer_kwargs(cfg):
    """Los parámetros del puntaje de amenaza que salen de la configuración (los usan el motor y el arnés de evaluación de clips)."""
    s = cfg.get("scoring", {})
    return dict(
        weights=s.get("weights", DEFAULT_WEIGHTS),
        thresholds=s.get("thresholds", {"medium": 3, "high": 6}),
        restricted_zone=s.get("restricted_zone"),
        loitering_seconds=s.get("loitering_seconds", 15),
        group_size=s.get("group_size", 3),
        sensitive_hours=s.get("sensitive_hours", [22, 6]),
        persistence_frames=s.get("persistence_frames", 5),
        window=s.get("window", 8),
        loitering_radius_h=s.get("loitering_radius_h", 0.8),
        proximity_ratio=s.get("proximity_ratio", 0.6),
        proximity_seconds=s.get("proximity_seconds", 1.5),
        tracking=s.get("tracking"),
        posture_hold_s=s.get("posture_hold_s"),
        posture_gap_s=s.get("posture_gap_s", 2.0),
        posture_min_ratio=s.get("posture_min_ratio", 0.65),
    )


def _imgsz(valor):
    """El tamaño de entrada de un modelo (entero entre 32 y 2048), o None para dejar el de fábrica."""
    if isinstance(valor, bool) or not isinstance(valor, int) or not 32 <= valor <= 2048:
        return None
    return valor


class GuardianEngine:
    def __init__(self, cfg):
        d = cfg.get("detector", {})
        p = cfg.get("pose", {})
        self.person_conf = d.get("conf", 0.5)
        pose_activa = bool(p.get("enabled", False))
        # Modelo único: el modelo de postura detecta a las personas y sus puntos en UNA pasada, y no se carga el detector de personas.
        self.single_model = pose_activa and bool(p.get("single_model", False))
        self.detector = (None if self.single_model else
                         PersonDetector(d.get("model", "yolo11n.pt"), self.person_conf, d.get("device"), imgsz=_imgsz(d.get("imgsz"))))
        self.pose = (PoseDetector(p.get("model", "yolo11n-pose.pt"), p.get("conf", 0.4), p.get("device"), imgsz=_imgsz(p.get("imgsz")))
                     if pose_activa else None)
        cada = p.get("every_n", 1)
        self.pose_every_n = cada if isinstance(cada, int) and not isinstance(cada, bool) and cada >= 1 else 1

        self._scorer_kwargs = scorer_kwargs(cfg)
        self.scorer = ThreatScorer(**self._scorer_kwargs)
        # Los modelos YOLO se comparten entre clientes: un candado evita inferencias simultaneas.
        self.infer_lock = threading.Lock()
        self.zone_name = cfg.get("zone_name", "Cuadra 01")
        self.sample_every = cfg.get("analysis", {}).get("sample_every", 3)
        self.event_cooldown_s = cfg.get("analysis", {}).get("event_cooldown_s", 5)

    def inferir(self, frame, tick=0):
        """
        Una pasada de los modelos sobre un cuadro. Devuelve (personas, posturas). `posturas` es la lista por persona, o None si la pose no
        se corrió en este cuadro (está apagada, no había nadie o no tocaba por pose.every_n): el puntaje conserva entonces lo que ya sabía.
        """
        if self.single_model:
            return self.pose.detect_and_analyze(frame, self.person_conf)
        detections = self.detector.detect(frame)
        poses = None
        if self.pose and detections and tick % self.pose_every_n == 0:
            poses = self.pose.analyze(frame)
        return detections, poses

    def analyze_video(self, video_path, evidence_dir, source_label=None):
        """
        Procesa un video y devuelve la lista de eventos de amenaza (MEDIO/ALTO).
        Guarda un frame de evidencia por evento en evidence_dir.
        """
        os.makedirs(evidence_dir, exist_ok=True)
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"No se pudo abrir el video: {video_path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        source_label = source_label or os.path.basename(video_path)

        events = []
        idx = 0
        last_event_vt = -1e9
        analizados = 0
        now_dt = datetime.datetime.now()  # contexto horario = momento del análisis
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if idx % self.sample_every != 0:
                continue
            video_time = idx / fps  # segundos dentro del video (sirve para merodeo)

            detections, postures = self.inferir(frame, analizados)
            analizados += 1
            result = self.scorer.evaluate(detections, video_time, now_dt, postures)

            if result["level"] in ("MEDIO", "ALTO") and (video_time - last_event_vt) >= self.event_cooldown_s:
                last_event_vt = video_time
                stamp = f"{int(video_time*1000):08d}"
                img_name = f"evt_{source_label}_{stamp}.jpg"
                img_path = os.path.join(evidence_dir, img_name)
                # dibuja cajas para la evidencia
                vis = frame.copy()
                for d in detections:
                    x1, y1, x2, y2 = d["bbox"]
                    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.imwrite(img_path, vis)
                events.append({
                    "source": source_label,
                    "video_time_s": round(video_time, 1),
                    "level": result["level"],
                    "score": result["score"],
                    "reasons": ", ".join(result["reasons"]),
                    "image": img_name,
                    "people": result["people"],
                })
        cap.release()
        return events


    def new_session(self, evidence_dir):
        """Sesion de camara en vivo: scorer propio por cliente, modelos compartidos."""
        return LiveSession(self, evidence_dir)


class LiveSession:
    """
    Estado de UN cliente conectado por camara web (merodeo, ventana de persistencia,
    cooldown de eventos). Cada frame que llega del navegador pasa por process().
    """

    def __init__(self, engine, evidence_dir):
        self.engine = engine
        self.scorer = ThreatScorer(**engine._scorer_kwargs)
        self.evidence_dir = evidence_dir
        self.last_event_ts = 0.0
        self._tick = 0               # cuántos cuadros ha analizado esta sesión (para pose.every_n)
        os.makedirs(evidence_dir, exist_ok=True)

    def process(self, frame):
        eng = self.engine
        now_ts = time.time()                 # en vivo el merodeo se mide con reloj real
        now_dt = datetime.datetime.now()
        h, w = frame.shape[:2]

        with eng.infer_lock:
            detections, postures = eng.inferir(frame, self._tick)
        self._tick += 1

        result = self.scorer.evaluate(detections, now_ts, now_dt, postures)
        level = result["level"]
        boxes = [{"bbox": [int(v) for v in d["bbox"]], "conf": float(d["conf"])}
                 for d in detections]

        event = None
        if level in ("MEDIO", "ALTO") and now_ts - self.last_event_ts >= eng.event_cooldown_s:
            self.last_event_ts = now_ts
            vis = frame.copy()
            for b in boxes:
                x1, y1, x2, y2 = b["bbox"]
                cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
            img_name = f"live_{now_dt.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
            cv2.imwrite(os.path.join(self.evidence_dir, img_name), vis)
            event = {
                "source": eng.zone_name + " (en vivo)",
                "video_time_s": None,
                "level": level,
                "score": result["score"],
                "reasons": ", ".join(result["reasons"]),
                "image": img_name,
                "people": result["people"],
            }

        return {"width": w, "height": h, "level": level, "score": result["score"],
                "reasons": result["reasons"], "people": result["people"],
                "boxes": boxes, "event": event}
