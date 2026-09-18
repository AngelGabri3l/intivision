"""
main.py — Detector en vivo GUARDIAN.

La fuente de video y el modelo se configuran en config.yaml.

Ejemplos:
    python main.py                       # usa config.yaml
    python main.py --source video.mp4    # analizar un archivo
    python main.py --source "rtsp://usuario:clave@ip:554/stream1"
    python main.py --no-window           # sin ventana
"""
import argparse
import time
import logging

import cv2
import yaml

from detector import PersonDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guardian")


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("No se encontro %s; uso valores por defecto.", path)
        return {}


def main():
    ap = argparse.ArgumentParser(description="GUARDIAN - deteccion de personas")
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

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        log.error("No se pudo abrir la fuente: %s", source)
        return
    log.info("GUARDIAN iniciado. Fuente=%s", source)
    is_file = isinstance(source, str) and source.lower().endswith((".mp4", ".avi", ".mov"))

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if is_file:
                    log.info("Fin del video.")
                    break
                time.sleep(0.5)   # la camara RTSP perdio un frame: reintentar
                continue

            detections = detector.detect(frame)
            for d in detections:
                x1, y1, x2, y2 = d["bbox"]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 165, 255), 2)
                cv2.putText(frame, f'{d["conf"]:.0%}', (x1, y1 - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 2)

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
