"""
threat_scoring.py — El "criterio de amenaza" (puntaje por señales combinadas).

Señales de contexto: persona estable, zona restringida, horario sensible, merodeo, grupo y cercanía entre personas.
Señales de comportamiento (pose): trepar, caído, manos arriba.

El MERODEO y la CERCANÍA se miden por persona (H3): cada persona tiene un identificador que se conserva entre cuadros (tracker.py).
  • Merodeo: UNA MISMA persona permanece casi en el mismo sitio (se mueve menos de `loitering_radius_h` alturas de su cuerpo) durante
    `loitering_seconds`, dentro de la zona de la cámara si hay una definida. Quien cruza el cuadro sin detenerse no suma.
  • Cercanía: la distancia entre los centros de DOS personas es menor que `proximity_ratio` veces su altura media, durante al menos
    `proximity_seconds` seguidos.

Todas las señales SUMAN a un puntaje único; el puntaje define el nivel (BAJO/MEDIO/ALTO). Ninguna señal de pose puntúa si no hay,
además, una persona presente y estable: así se cumple la regla de que ninguna señal es gatillo único.
"""
import math
import time  # noqa: F401  (se mantiene: otros módulos lo esperan junto con este)
from collections import deque
from itertools import combinations

from confirmation import point_in_polygon
from tracker import PersonTracker

MAX_MUESTRAS = 600              # muestras de posición que se guardan por persona (a ~1 por 0,25 s son 150 s: sobra para decidir)
SEPARACION_MUESTRAS_S = 0.25    # no se guarda más de una muestra cada tanto, aunque se analice más rápido
PAREJA_AUSENTE_S = 2.5          # si una de las dos personas no se ve, la cuenta de cercanía se guarda este tiempo (un cuadro perdido a 1 fps)


def _num(valor, defecto, minimo, maximo):
    """Un número de la configuración dentro de su rango; si falta o es absurdo, se usa el valor de fábrica."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        return defecto
    return valor if minimo <= valor <= maximo else defecto


class ThreatScorer:
    def __init__(self, weights, thresholds, restricted_zone=None,
                 loitering_seconds=15, group_size=3, sensitive_hours=(22, 6),
                 persistence_frames=5, window=8,
                 loitering_radius_h=0.8, proximity_ratio=0.6, proximity_seconds=1.5, tracking=None):
        self.w = weights
        self.thr = thresholds
        self.zone = restricted_zone or []
        self.loitering_seconds = loitering_seconds
        self.group_size = group_size
        self.sensitive_hours = tuple(sensitive_hours)
        self.persistence = persistence_frames
        self.history = deque(maxlen=window)

        self.loiter_radius_h = _num(loitering_radius_h, 0.8, 0.05, 5.0)
        self.prox_ratio = _num(proximity_ratio, 0.6, 0.1, 3.0)
        self.prox_seconds = _num(proximity_seconds, 1.5, 0.0, 60.0)
        tr = tracking if isinstance(tracking, dict) else {}
        self.tracker = PersonTracker(max_lost_s=_num(tr.get("max_lost_s"), 4.0, 0.5, 60.0),
                                     min_hits=int(_num(tr.get("min_hits"), 2, 1, 10)))
        self.loiter_gap_s = _num(tr.get("loiter_gap_s"), 2.0, 0.0, 60.0)
        self._dwell = {}        # id de la persona -> {"muestras": deque de (t, x, y), "inicio": desde cuándo está en el mismo sitio}
        self._pairs = {}        # (id, id) -> {"desde": cuándo empezaron a estar juntas, "visto": la última vez que se vieron juntas}

    def _feet_in_zone(self, det):
        x1, y1, x2, y2 = det["bbox"]
        return point_in_polygon(((x1 + x2) // 2, y2), self.zone)

    def _is_sensitive_hour(self, now_dt):
        start, end = self.sensitive_hours
        h = now_dt.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    # ------------------------------------------------------------------ merodeo por persona
    def _loiter_seconds(self, track, now_ts):
        """Cuánto lleva ESTA persona casi en el mismo sitio: el tramo más reciente cuyas posiciones caben en el radio permitido."""
        st = self._dwell.get(track.id)
        if st is None:
            # Se cuenta desde la PRIMERA vez que se vio a la persona, aunque entonces aún no estuviera confirmada. Si estuvo fuera de la zona
            # (donde no se la mide), el hueco de más abajo reinicia el conteo al entrar.
            st = self._dwell[track.id] = {"muestras": deque([(track.first_ts, track.x0, track.y0)]), "inicio": track.first_ts}
        muestras = st["muestras"]
        radio = self.loiter_radius_h * track.h
        hueco = max(self.loiter_gap_s, 2.5 * self.tracker._periodo)
        if muestras and now_ts - st.get("visto", muestras[-1][0]) > hueco:
            # No se vio a esta persona durante un rato: lo que aparece ahora puede ser OTRA que "heredó" el identificador. El merodeo no
            # puede unir dos presencias separadas por un hueco: se vuelve a contar desde cero.
            muestras.clear()
        st["visto"] = now_ts
        if muestras and now_ts - muestras[-1][0] < SEPARACION_MUESTRAS_S:
            return now_ts - st["inicio"]
        if muestras:
            mx = sum(m[1] for m in muestras) / len(muestras)
            my = sum(m[2] for m in muestras) / len(muestras)
            if math.hypot(track.cx - mx, track.cy - my) > 2 * radio:        # se fue claramente a otro lado: empieza de nuevo
                muestras.clear()
        if not muestras:
            st["inicio"] = now_ts
        muestras.append((now_ts, track.cx, track.cy))
        while len(muestras) > MAX_MUESTRAS:
            muestras.popleft()                                               # solo por memoria: "inicio" no cambia
        while len(muestras) > 1 and self._extension(muestras) > radio:
            muestras.popleft()                                               # las muestras más viejas ya no caben en el radio
            st["inicio"] = muestras[0][0]
        return now_ts - st["inicio"]

    @staticmethod
    def _extension(muestras):
        mx = sum(m[1] for m in muestras) / len(muestras)
        my = sum(m[2] for m in muestras) / len(muestras)
        return max(math.hypot(m[1] - mx, m[2] - my) for m in muestras)

    # ------------------------------------------------------------------ cercanía entre personas
    def _proximity(self, tracks, now_ts):
        """True si hay al menos una pareja que lleva `proximity_seconds` seguidos muy junta."""
        activa = False
        juntas = set()
        for a, b in combinations(tracks, 2):
            clave = (min(a.id, b.id), max(a.id, b.id))
            if math.hypot(a.cx - b.cx, a.cy - b.cy) < self.prox_ratio * (a.h + b.h) / 2.0:
                st = self._pairs.setdefault(clave, {"desde": now_ts, "visto": now_ts})
                st["visto"] = now_ts
                juntas.add(clave)
                if now_ts - st["desde"] >= self.prox_seconds:
                    activa = True
        visibles = {t.id for t in tracks}
        for clave in list(self._pairs):
            if clave in juntas:
                continue
            ausente = not (clave[0] in visibles and clave[1] in visibles)
            # Si se ven las dos y no están juntas, la cuenta se corta. Si falta una de las dos (no se detectó), la cuenta se guarda un
            # momento: un cuadro perdido no debe reiniciarla.
            if not ausente or now_ts - self._pairs[clave]["visto"] > PAREJA_AUSENTE_S:
                del self._pairs[clave]
        return activa

    # ------------------------------------------------------------------ evaluación de un cuadro
    def evaluate(self, detections, now_ts, now_dt, postures=None):
        """
        postures: conjunto como {"trepar","caido"} (de pose_detector).
        """
        people = detections
        relevant = [d for d in people if self._feet_in_zone(d)] if self.zone else people
        present = len(relevant) > 0

        self.history.append(present)
        stable = sum(self.history) >= self.persistence

        # Seguimiento: cada persona confirmada que se ve ahora, con su identificador
        visibles = self.tracker.update(people, now_ts)
        if self.zone:
            visibles = [t for t in visibles if point_in_polygon((int((t.bbox[0] + t.bbox[2]) // 2), int(t.bbox[3])), self.zone)]
        for tid in [tid for tid in self._dwell if tid not in self.tracker.tracks]:
            del self._dwell[tid]                                              # personas que ya se olvidaron
        # Quien sale de la zona deja de contar; si vuelve tras más de `loiter_gap_s` el conteo empieza de nuevo (lo decide el hueco, no un
        # borrado): así el ruido del detector justo en el borde de la zona no reinicia a quien se queda parado ahí.
        loiter_time = max([self._loiter_seconds(t, now_ts) for t in visibles], default=0)
        close = self._proximity(visibles, now_ts)

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
            if close:
                score += self.w.get("proximity", 1); reasons.append("cercanía entre personas")

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
                "people": len(people), "loiter": int(loiter_time),
                "tracks": [{"id": t.id, "bbox": [int(v) for v in t.bbox]} for t in visibles]}
