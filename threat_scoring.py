"""
threat_scoring.py — El "criterio de amenaza" (puntaje por señales combinadas).

Señales de contexto: persona estable, zona restringida, horario sensible, merodeo,
grupo. Señales de comportamiento (pose): trepar, caído, manos arriba.

Todas SUMAN a un puntaje único; el puntaje define el nivel (BAJO/MEDIO/ALTO). Ninguna
señal de pose puntúa si no hay, además, una persona presente y estable: así se
cumple la regla de que ninguna señal es gatillo único.
"""
import time
from collections import deque

from confirmation import point_in_polygon


class ThreatScorer:
    def __init__(self, weights, thresholds, restricted_zone=None,
                 loitering_seconds=15, group_size=3, sensitive_hours=(22, 6),
                 persistence_frames=5, window=8):
        self.w = weights
        self.thr = thresholds
        self.zone = restricted_zone or []
        self.loitering_seconds = loitering_seconds
        self.group_size = group_size
        self.sensitive_hours = tuple(sensitive_hours)
        self.persistence = persistence_frames
        self.history = deque(maxlen=window)
        self.presence_start = None

    def _feet_in_zone(self, det):
        x1, y1, x2, y2 = det["bbox"]
        return point_in_polygon(((x1 + x2) // 2, y2), self.zone)

    def _is_sensitive_hour(self, now_dt):
        start, end = self.sensitive_hours
        h = now_dt.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    def evaluate(self, detections, now_ts, now_dt, postures=None):
        """
        postures: conjunto como {"trepar","caido"} (de pose_detector).
        """
        people = detections
        relevant = [d for d in people if self._feet_in_zone(d)] if self.zone else people
        present = len(relevant) > 0

        self.history.append(present)
        stable = sum(self.history) >= self.persistence

        if present:
            if self.presence_start is None:
                self.presence_start = now_ts
            loiter_time = now_ts - self.presence_start
        else:
            self.presence_start = None
            loiter_time = 0

        score = 0
        reasons = []
        if stable and present:
            score += self.w["person_present"]; reasons.append("persona detectada")

            if self.zone:
                score += self.w["in_restricted_zone"]; reasons.append("en zona restringida")
            if self._is_sensitive_hour(now_dt):
                score += self.w["sensitive_hour"]; reasons.append("horario sensible")
            if loiter_time >= self.loitering_seconds:
                score += self.w["loitering"]; reasons.append(f"merodeo {int(loiter_time)}s")
            if len(relevant) >= self.group_size:
                score += self.w["group"]; reasons.append(f"grupo de {len(relevant)}")

            # Postura (pose)
            if postures:
                if "trepar" in postures:
                    score += self.w.get("climbing", 3); reasons.append("trepando")
                if "caido" in postures:
                    score += self.w.get("fallen", 3); reasons.append("persona en el suelo")
                if "manos_arriba" in postures:
                    score += self.w.get("hands_up", 3); reasons.append("posible asalto (manos arriba)")

        if score >= self.thr["high"]:
            level = "ALTO"
        elif score >= self.thr["medium"]:
            level = "MEDIO"
        elif score > 0:
            level = "BAJO"
        else:
            level = "NINGUNO"

        return {"score": score, "level": level, "reasons": reasons,
                "people": len(people), "loiter": int(loiter_time)}
