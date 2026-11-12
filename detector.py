class PersonDetector:
    PERSON_CLASS_ID = 0

    def __init__(self, model_path="yolo11n.pt", conf=0.5, device=None, imgsz=None):
        from ultralytics import YOLO  # import diferido
        self.model = YOLO(model_path)
        self.conf = conf
        self.device = device
        self.imgsz = imgsz            # tamaño de entrada del modelo; None = el de fábrica del modelo (640)

    def detect(self, frame):
        extra = {"imgsz": self.imgsz} if self.imgsz else {}
        results = self.model.predict(frame, conf=self.conf,
                                     classes=[self.PERSON_CLASS_ID],
                                     verbose=False, device=self.device, **extra)
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
