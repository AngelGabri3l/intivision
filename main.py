"""
main.py — Prueba de detección de personas con la webcam.

Uso:
    python main.py                     # webcam
    python main.py --source video.mp4  # archivo de video
"""
import argparse

import cv2

from detector import PersonDetector


def main():
    ap = argparse.ArgumentParser(description="GUARDIAN - prueba de deteccion")
    ap.add_argument("--source", default="0")
    args = ap.parse_args()
    source = int(args.source) if args.source.isdigit() else args.source

    detector = PersonDetector("yolo11n.pt", conf=0.5)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print("No se pudo abrir la fuente:", source)
        return

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        detections = detector.detect(frame)
        for d in detections:
            x1, y1, x2, y2 = d["bbox"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 165, 255), 2)
            cv2.putText(frame, f'{d["conf"]:.0%}', (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 2)
        cv2.putText(frame, f"Personas: {len(detections)}", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.imshow("GUARDIAN - prueba (q para salir)", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
