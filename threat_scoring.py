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
from tracker import PersonTracker, iou

MAX_MUESTRAS = 600              # muestras de posición que se guardan por persona (a ~1 por 0,25 s son 150 s: sobra para decidir)
SEPARACION_MUESTRAS_S = 0.25    # no se guarda más de una muestra cada tanto, aunque se analice más rápido
PAREJA_AUSENTE_S = 2.5          # si una de las dos personas no se ve, la cuenta de cercanía se guarda este tiempo (un cuadro perdido a 1 fps)
POSTURAS = {"caido": "fallen", "manos_arriba": "hands_up", "trepar": "climbing"}     # nombre en pose_detector -> señal del puntaje
HOLD_DEFECTO = {"fallen": 2.0, "hands_up": 1.0, "climbing": 1.5}                      # segundos que debe sostenerse cada postura


def _num(valor, defecto, minimo, maximo):
    """Un número de la configuración dentro de su rango; si falta o es absurdo, se usa el valor de fábrica."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        return defecto
    return valor if minimo <= valor <= maximo else defecto


class ThreatScorer:
    def __init__(self, weights, thresholds, restricted_zone=None,
                 loitering_seconds=15, group_size=3, sensitive_hours=(22, 6),
                 persistence_frames=5, window=8,
                 loitering_radius_h=0.8, proximity_ratio=0.6, proximity_seconds=1.5, tracking=None,
                 posture_hold_s=None, posture_gap_s=2.0, posture_min_ratio=0.65):
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
        ph = posture_hold_s if isinstance(posture_hold_s, dict) else {}
        self.posture_hold = {senal: _num(ph.get(senal), defecto, 0.0, 60.0) for senal, defecto in HOLD_DEFECTO.items()}
        self.posture_gap_s = _num(posture_gap_s, 2.0, 0.0, 60.0)
        self.posture_min_ratio = _num(posture_min_ratio, 0.65, 0.1, 1.0)
        self._posturas = {}     # id de la persona -> {postura: {"desde": cuándo empezó a sostenerla, "visto": la última vez que se la vio así}}
        self._pose_ts = None    # cuándo fue la última vez que se corrió la pose
        self._pose_periodo = 0.0  # cada cuántos segundos llega la pose (promedio): puede ser más lento que el análisis
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

    # ------------------------------------------------------------------ postura por persona
    def _emparejar_pose(self, bbox, visibles):
        """El id de la persona seguida a la que pertenece esta caja de pose, o None si no es de nadie que se esté siguiendo."""
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(x1 + y1 + x2 + y2) and x2 > x1 and y2 > y1):
            return None
        caja = (x1, y1, x2, y2)
        mejor, mejor_iou = None, 0.0
        for t in visibles:
            v = iou(t.bbox, caja)
            if v > mejor_iou:
                mejor, mejor_iou = t.id, v
        if mejor is not None and mejor_iou >= 0.3:
            return mejor
        cerca, cerca_d = None, 0.6                       # respaldo: el centro más cercano, a menos de 0,6 alturas
        for t in visibles:
            d = math.hypot((x1 + x2) / 2 - t.cx, (y1 + y2) / 2 - t.cy) / max(t.h, y2 - y1)
            if d < cerca_d:
                cerca, cerca_d = t.id, d
        return cerca

    def _actualizar_posturas(self, postures, visibles, now_ts):
        """
        Devuelve las posturas (con el nombre de pose_detector) que ALGUNA persona SOSTIENE: asignadas a una persona concreta y vistas durante
        `posture_hold_s` segundos. Un gesto de un solo cuadro, o de personas distintas en cuadros distintos, no cuenta.
          • postures = lista por persona ([{"postures": {...}, "bbox": [...]}, ...]): la pose se corrió en este cuadro.
          • postures = None: la pose NO se corrió en este cuadro; se conserva lo que ya se sabía.
          • postures = conjunto (forma antigua, una sola lista para toda la escena): cuenta al instante, sin persistencia.
        """
        if isinstance(postures, (set, frozenset)):
            return set(postures)
        if not isinstance(postures, (list, tuple)):
            postures = None                                  # cualquier otra cosa se toma como «la pose no se corrió»
        for tid in [tid for tid in self._posturas if tid not in self.tracker.tracks]:
            del self._posturas[tid]
        if postures is not None:
            if self._pose_ts is not None and 0 < now_ts - self._pose_ts <= 10.0:
                intervalo = now_ts - self._pose_ts
                self._pose_periodo = intervalo if self._pose_periodo == 0.0 else 0.8 * self._pose_periodo + 0.2 * intervalo
            self._pose_ts = now_ts
            tolerancia = max(self.posture_gap_s, 2.5 * self._pose_periodo)
            vistas = {}
            for pr in postures:
                tid = self._emparejar_pose(pr.get("bbox"), visibles) if isinstance(pr, dict) else None
                if tid is not None:
                    vistas.setdefault(tid, set()).update(n for n in (pr.get("postures") or ()) if n in POSTURAS)
            for t in visibles:
                estado = self._posturas.setdefault(t.id, {})
                ahora = vistas.get(t.id, set())
                for nombre in POSTURAS:
                    e = estado.get(nombre)
                    if e is None:
                        if nombre not in ahora:
                            continue                            # lo que nunca se ha visto no hace falta contarlo
                        e = estado[nombre] = {"muestras": deque(), "desde": None, "visto": None, "t0": now_ts}
                    m = e["muestras"]
                    if m and now_ts - m[-1][0] > tolerancia:
                        m.clear()                               # hueco largo sin información: no se puede unir con lo anterior
                        e["desde"], e["t0"] = None, now_ts
                    visto_ahora = nombre in ahora
                    m.append((now_ts, visto_ahora))
                    while m and m[0][0] < now_ts - self.posture_hold[POSTURAS[nombre]] - 1e-9:
                        m.popleft()                             # solo cuenta el último tramo del tiempo que se exige sostenerla
                    if visto_ahora:
                        e["visto"] = now_ts
                        if e["desde"] is None:
                            e["desde"] = now_ts
                    completo = now_ts - e["t0"] >= self.posture_hold[POSTURAS[nombre]] - 1e-9      # ya hay un tramo entero de observación para juzgar
                    if completo and sum(1 for _, o in m if o) < self.posture_min_ratio * len(m):
                        e["desde"] = None                       # se vio, pero no en la mayor parte del tramo: son clasificaciones sueltas, no un gesto
                    if e["desde"] is None and not any(o for _, o in m):
                        del estado[nombre]
        tolerancia = max(self.posture_gap_s, 2.5 * self._pose_periodo)
        activas = set()
        for estado in self._posturas.values():
            for nombre, e in estado.items():
                # «Sostenida» = se la VIO durante `hold` segundos seguidos (de la primera a la última vez que se reconoció, con la mayor parte de los
                # análisis de ese tramo viéndola) y sigue vigente. Vista una sola vez, o a ratos sueltos, no se vuelve activa por el paso del tiempo.
                if (e["desde"] is not None and now_ts - e["visto"] <= tolerancia
                        and e["visto"] - e["desde"] >= self.posture_hold[POSTURAS[nombre]]):
                    activas.add(nombre)
        return activas

    # ------------------------------------------------------------------ evaluación de un cuadro
    def evaluate(self, detections, now_ts, now_dt, postures=None):
        """
        postures: las posturas de CADA persona (lista de pose_detector.analyze), None si la pose no se corrió en este cuadro, o, en la forma
        antigua, un conjunto como {"trepar","caido"} para toda la escena.
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
        active = self._actualizar_posturas(postures, visibles, now_ts)

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
            if active:
                if "trepar" in active:
                    score += self.w.get("climbing", 3); reasons.append("trepando")
                if "caido" in active:
                    score += self.w.get("fallen", 3); reasons.append("persona en el suelo")
                if "manos_arriba" in active:
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
