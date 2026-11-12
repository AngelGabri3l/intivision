"""
pose_detector.py — Detección de POSTURA con YOLO-Pose.

Reconoce posturas de amenaza con geometría simple sobre los 17 puntos del cuerpo:
  - "trepar"       : muñecas sobre hombros + rodilla elevada
  - "caido"        : tronco horizontal, con hombros y caderas visibles (posible víctima)
  - "manos_arriba" : muñecas sobre la cabeza (posible asalto)

Las posturas SUMAN al puntaje de amenaza; no disparan solas. Corre solo cuando ya
hay una persona detectada.
"""
NOSE = 0
L_SHOULDER, R_SHOULDER = 5, 6
L_WRIST, R_WRIST = 9, 10
L_HIP, R_HIP = 11, 12
L_KNEE, R_KNEE = 13, 14


def classify_posture(xy, kconf, bbox, min_conf=0.3):
    postures = set()

    def g(i):
        if i < len(kconf) and kconf[i] >= min_conf:
            return (xy[i][0], xy[i][1])
        return None

    nose = g(NOSE)
    ls, rs = g(L_SHOULDER), g(R_SHOULDER)
    lw, rw = g(L_WRIST), g(R_WRIST)
    lh, rh = g(L_HIP), g(R_HIP)
    lk, rk = g(L_KNEE), g(R_KNEE)

    x1, y1, x2, y2 = bbox
    box_h = max(1, y2 - y1)
    box_w = max(1, x2 - x1)

    head_y = None
    if nose:
        head_y = nose[1]
    elif ls or rs:
        head_y = min(p[1] for p in [ls, rs] if p)

    # 1) CAÍDO: el tronco (hombros -> caderas) está más horizontal que vertical.
    #    Hace falta ver hombros Y caderas: con una cámara cercana (por ejemplo la de una
    #    laptop) solo se ve la parte alta del cuerpo y la caja sale ancha aunque la
    #    persona esté sentada o de pie. Sin caderas no se puede afirmar que esté en el suelo.
    shoulders = [p for p in (ls, rs) if p]
    hips = [p for p in (lh, rh) if p]
    if shoulders and hips:
        torso_dx = abs(sum(p[0] for p in shoulders) / len(shoulders)
                       - sum(p[0] for p in hips) / len(hips))
        torso_dy = abs(sum(p[1] for p in shoulders) / len(shoulders)
                       - sum(p[1] for p in hips) / len(hips))
        if torso_dx > torso_dy * 1.5 and torso_dx > 0.12 * max(box_w, box_h):
            postures.add("caido")

    # 2) MANOS ARRIBA: muñecas sobre el rostro (exige nariz para no confundir con trepar)
    if nose and lw and rw and lw[1] < nose[1] and rw[1] < nose[1]:
        postures.add("manos_arriba")

    # 3) TREPAR: muñecas sobre hombros + rodilla sobre cadera
    shoulders_y = [p[1] for p in [ls, rs] if p]
    hips_y = [p[1] for p in [lh, rh] if p]
    if shoulders_y and lw and rw:
        sy = sum(shoulders_y) / len(shoulders_y)
        hands_high = lw[1] < sy and rw[1] < sy
        knee_raised = False
        if hips_y:
            hy = sum(hips_y) / len(hips_y)
            for k in (lk, rk):
                if k and k[1] < hy:
                    knee_raised = True
        if hands_high and knee_raised:
            postures.add("trepar")

    return postures


class PoseDetector:
    def __init__(self, model_path="yolo11n-pose.pt", conf=0.4, device=None, imgsz=None):
        from ultralytics import YOLO
        self.model = YOLO(model_path)
        self.conf = conf
        self.device = device
        self.imgsz = imgsz            # tamaño de entrada del modelo; None = el de fábrica del modelo (640)

    def _predict(self, frame, conf):
        extra = {"imgsz": self.imgsz} if self.imgsz else {}
        return self.model.predict(frame, conf=conf, verbose=False, device=self.device, **extra)

    @staticmethod
    def _personas(results):
        """Una entrada por persona que el modelo vio: su caja, su confianza y las posturas que se le reconocen."""
        for r in results:
            if r.keypoints is None or r.boxes is None:
                continue
            kp = r.keypoints
            for i in range(len(r.boxes)):
                try:
                    xy = kp.xy[i].tolist()
                    kconf = kp.conf[i].tolist() if kp.conf is not None else [1.0] * len(xy)
                    bbox = [int(v) for v in r.boxes.xyxy[i].tolist()]
                except (IndexError, AttributeError):
                    continue
                try:
                    conf = float(r.boxes.conf[i])
                except (IndexError, AttributeError, TypeError, ValueError):
                    conf = 1.0
                yield {"bbox": bbox, "conf": conf, "postures": classify_posture(xy, kconf, bbox)}

    def analyze(self, frame):
        """Las posturas de CADA persona: [{"postures": {...}, "bbox": [x1, y1, x2, y2]}, ...]."""
        return [{"postures": p["postures"], "bbox": p["bbox"]} for p in self._personas(self._predict(frame, self.conf))]

    def detect_and_analyze(self, frame, person_conf=0.5):
        """
        Modo de modelo único: el modelo de postura hace de detector de personas. En UNA pasada devuelve (personas, posturas), con las personas en
        el mismo formato que PersonDetector.detect y las posturas como analyze. Una persona cuenta si su confianza llega a `person_conf`;
        su postura se reconoce si llega a la confianza del modelo de postura.
        """
        detections, poses = [], []
        for p in self._personas(self._predict(frame, min(self.conf, person_conf))):
            x1, y1, x2, y2 = p["bbox"]
            if p["conf"] >= self.conf:
                poses.append({"postures": p["postures"], "bbox": p["bbox"]})
            if p["conf"] >= person_conf:
                detections.append({"bbox": (x1, y1, x2, y2), "conf": p["conf"], "center": ((x1 + x2) // 2, (y1 + y2) // 2)})
        return detections, poses
