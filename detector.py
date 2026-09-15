"""
detector.py — Detección de personas con YOLO.

Modelo 'nano' (yolo11n) por velocidad en el borde. Filtra solo la clase 'person'
(id 0 de COCO) para no gastar cómputo en otros objetos.
"""


class PersonDetector:
    PERSON_CLASS_ID = 0

    def __init__(self, model_path="yolo11n.pt", conf=0.5, device=None):
        from ultralytics import YOLO  # import diferido
        self.model = YOLO(model_path)
        self.conf = conf
        self.device = device

    def detect(self, frame):
        results = self.model.predict(frame, conf=self.conf,
                                     classes=[self.PERSON_CLASS_ID],
                                     verbose=False, device=self.device)
        detections = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                conf = float(box.conf[0])
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                detections.append({
                    "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    "conf": conf,
                    "center": (int(cx), int(cy)),
                })
        return detections
