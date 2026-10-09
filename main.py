"""
main.py — Detector en vivo (cámara o video) con puntaje de amenaza.

Pipeline: capturar -> detectar personas -> (pose) -> puntuar -> responder.
Respuesta escalonada BAJO/MEDIO/ALTO. Guarda clip de evidencia y notifica.


"""
import argparse
import time
import logging
import datetime

import cv2
import numpy as np

from detector import PersonDetector
from pose_detector import PoseDetector
from threat_scoring import ThreatScorer
from actuator import build_actuator
from notifier import Notifier
from video_buffer import VideoBuffer
from settings import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guardian")

LEVEL_COLOR = {"NINGUNO": (120, 120, 120), "BAJO": (0, 200, 0),
               "MEDIO": (0, 165, 255), "ALTO": (0, 0, 255)}
LEVEL_COOLDOWN = {"BAJO": 60, "MEDIO": 45, "ALTO": 20}


def main():
    ap = argparse.ArgumentParser(description="GUARDIAN - deteccion y puntaje de amenaza")
    ap.add_argument("--source", default=None)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--no-window", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    source = args.source if args.source is not None else str(cfg.get("source", "0"))
    source = int(source) if source.isdigit() else source

    det_cfg = cfg.get("detector", {})
    detector = PersonDetector(det_cfg.get("model", "yolo11n.pt"),
                              det_cfg.get("conf", 0.5), det_cfg.get("device"))

    pose_cfg = cfg.get("pose", {})
    pose_detector = None
    if pose_cfg.get("enabled", False):
        pose_detector = PoseDetector(pose_cfg.get("model", "yolo11n-pose.pt"),
                                     pose_cfg.get("conf", 0.4), pose_cfg.get("device"))
        log.info("Deteccion de postura ACTIVADA")

    sc = cfg.get("scoring", {})
    scorer = ThreatScorer(
        weights=sc.get("weights", {}),
        thresholds=sc.get("thresholds", {"medium": 3, "high": 6}),
        restricted_zone=sc.get("restricted_zone"),
        loitering_seconds=sc.get("loitering_seconds", 15),
        group_size=sc.get("group_size", 3),
        sensitive_hours=sc.get("sensitive_hours", [22, 6]),
        persistence_frames=sc.get("persistence_frames", 5),
        window=sc.get("window", 8))
    zone = sc.get("restricted_zone") or []

    actuator = build_actuator(cfg)
    siren_s = cfg.get("actuator", {}).get("siren_seconds", 10)

    ncfg = cfg.get("notifier", {})
    events_dir = ncfg.get("events_dir", "events")
    notifier = Notifier(events_dir, ncfg.get("telegram_token"), ncfg.get("telegram_chat_id"))

    cl = cfg.get("clip", {})
    vbuf = VideoBuffer(cl.get("seconds", 15), cl.get("fps", 8), cl.get("max_width", 640)) \
        if cl.get("enabled", True) else None

    zone_name = cfg.get("zone_name", "Cuadra 01")
    last_fired = {}

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        log.error("No se pudo abrir la fuente: %s", source)
        return
    log.info("GUARDIAN iniciado. Fuente=%s  Zona=%s", source, zone_name)
    is_file = isinstance(source, str) and source.lower().endswith((".mp4", ".avi", ".mov"))

    def can_fire(level, now):
        if now - last_fired.get(level, 0) >= LEVEL_COOLDOWN.get(level, 30):
            last_fired[level] = now
            return True
        return False

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if is_file:
                    log.info("Fin del video.")
                    break
                time.sleep(0.5)
                continue

            now_ts = time.time()
            now_dt = datetime.datetime.now()
            if vbuf is not None:
                vbuf.push(frame)

            detections = detector.detect(frame)
            postures, pose_results = set(), []
            if pose_detector and detections:
                pose_results = pose_detector.analyze(frame)
                for pr in pose_results:
                    postures |= pr["postures"]
            result = scorer.evaluate(detections, now_ts, now_dt, postures)
            level = result["level"]

            for d in detections:
                x1, y1, x2, y2 = d["bbox"]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 165, 255), 2)
                cv2.putText(frame, f'{d["conf"]:.0%}', (x1, y1 - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 2)
            for pr in pose_results:
                if pr["postures"]:
                    bx1, by1, bx2, by2 = pr["bbox"]
                    cv2.putText(frame, "/".join(pr["postures"]), (bx1, by2 + 18),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            if len(zone) >= 3:
                cv2.polylines(frame, [np.array(zone, dtype=np.int32)], True, (255, 0, 0), 2)
            color = LEVEL_COLOR.get(level, (120, 120, 120))
            label = f"AMENAZA: {level} (puntaje {result['score']}) {', '.join(result['reasons'])}"
            cv2.rectangle(frame, (0, 0), (frame.shape[1], 28), color, -1)
            cv2.putText(frame, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

            if level != "NINGUNO" and can_fire(level, now_ts):
                t = now_dt.strftime("%H:%M:%S")
                best = max((d["conf"] for d in detections), default=0.0)
                meta = {"zone": zone_name, "conf": best, "time": t, "level": level,
                        "score": result["score"], "reasons": result["reasons"]}
                clip_path = None
                if vbuf is not None:
                    clip_path = vbuf.save_clip(f"{events_dir}/clip_{now_dt.strftime('%Y%m%d_%H%M%S')}.mp4")
                if level == "ALTO":
                    log.warning("AMENAZA ALTA en %s (%s) - %s", zone_name, t, ", ".join(result["reasons"]))
                    actuator.trigger(siren_s)
                    notifier.notify(frame, meta, clip_path)
                elif level == "MEDIO":
                    log.warning("Amenaza MEDIA en %s (%s) - %s", zone_name, t, ", ".join(result["reasons"]))
                    actuator.warn()
                    notifier.notify(frame, meta, clip_path)
                else:
                    log.info("Actividad BAJA en %s (%s)", zone_name, t)
                    notifier.notify(frame, meta, clip_path)

            if not args.no_window:
                cv2.imshow("GUARDIAN - prototipo (q para salir)", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        cv2.destroyAllWindows()
        log.info("GUARDIAN detenido.")


if __name__ == "__main__":
    main()
