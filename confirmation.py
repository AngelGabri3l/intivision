"""
confirmation.py — Helpers anti-falsas-alarmas.

Contiene point_in_polygon (usado por el puntaje para la zona restringida) y un
confirmador simple por persistencia (heredado del prototipo base).
"""
import time
from collections import deque


def point_in_polygon(point, polygon):
    """Ray casting. Polígono vacío => todo cuenta."""
    if not polygon or len(polygon) < 3:
        return True
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        denom = (yj - yi) or 1e-9
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / denom + xi):
            inside = not inside
        j = i
    return inside


class ThreatConfirmer:
    def __init__(self, persistence_frames=8, window=12, roi_polygon=None,
                 min_box_area=0, cooldown_s=30):
        self.persistence = persistence_frames
        self.history = deque(maxlen=window)
        self.roi = roi_polygon or []
        self.min_box_area = min_box_area
        self.cooldown_s = cooldown_s
        self.last_alert = 0.0

    def _qualifies(self, det):
        x1, y1, x2, y2 = det["bbox"]
        if (x2 - x1) * (y2 - y1) < self.min_box_area:
            return False
        feet = ((x1 + x2) // 2, y2)
        return point_in_polygon(feet, self.roi)

    def update(self, detections):
        self.history.append(any(self._qualifies(d) for d in detections))
        confirmed = sum(self.history) >= self.persistence
        now = time.time()
        if confirmed and (now - self.last_alert) >= self.cooldown_s:
            self.last_alert = now
            return True
        return False
