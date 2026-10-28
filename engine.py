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
                   "loitering": 2, "group": 2, "climbing": 3, "fallen": 3,
                   "hands_up": 3}


class GuardianEngine:
    def __init__(self, cfg):
        d = cfg.get("detector", {})
        self.detector = PersonDetector(d.get("model", "yolo11n.pt"),
                                       d.get("conf", 0.5), d.get("device"))

        p = cfg.get("pose", {})
        self.pose = (PoseDetector(p.get("model", "yolo11n-pose.pt"),
                                  p.get("conf", 0.4), p.get("device"))
                     if p.get("enabled", False) else None)

        s = cfg.get("scoring", {})
        self._scorer_kwargs = dict(
            weights=s.get("weights", DEFAULT_WEIGHTS),
            thresholds=s.get("thresholds", {"medium": 3, "high": 6}),
            restricted_zone=s.get("restricted_zone"),
            loitering_seconds=s.get("loitering_seconds", 15),
            group_size=s.get("group_size", 3),
            sensitive_hours=s.get("sensitive_hours", [22, 6]),
            persistence_frames=s.get("persistence_frames", 5),
            window=s.get("window", 8),
        )
        self.scorer = ThreatScorer(**self._scorer_kwargs)
        # Los modelos YOLO se comparten entre clientes: un candado evita inferencias simultaneas.
        self.infer_lock = threading.Lock()
        self.zone_name = cfg.get("zone_name", "Cuadra 01")
        self.sample_every = cfg.get("analysis", {}).get("sample_every", 3)
        self.event_cooldown_s = cfg.get("analysis", {}).get("event_cooldown_s", 5)

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
        now_dt = datetime.datetime.now()  # contexto horario = momento del análisis
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if idx % self.sample_every != 0:
                continue
            video_time = idx / fps  # segundos dentro del video (sirve para merodeo)

            detections = self.detector.detect(frame)
            postures = set()
            if self.pose and detections:
                for pr in self.pose.analyze(frame):
                    postures |= pr["postures"]
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
        os.makedirs(evidence_dir, exist_ok=True)

    def process(self, frame):
        eng = self.engine
        now_ts = time.time()                 # en vivo el merodeo se mide con reloj real
        now_dt = datetime.datetime.now()
        h, w = frame.shape[:2]

        with eng.infer_lock:
            detections = eng.detector.detect(frame)
            postures = set()
            if eng.pose and detections:
                for pr in eng.pose.analyze(frame):
                    postures |= pr["postures"]

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
