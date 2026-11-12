"""
tracker.py — Seguimiento de personas entre cuadros (H3).

Le da a cada persona un identificador que se conserva mientras siga en la escena. Es lo que permite contar el merodeo POR
PERSONA y medir la cercanía entre dos personas concretas, en vez de tratar a todos los que aparecen como uno solo.

Está pensado para analizar a pocos cuadros por segundo (1 a 3 en una mini PC), donde los modelos de movimiento clásicos
(que suponen 25 a 30 cuadros por segundo) no sirven:
  • La persona que aparece en un cuadro se busca en el siguiente a una distancia que CRECE con el tiempo transcurrido (alguien que
    camina recorre unas 0,9 alturas de su cuerpo por segundo). La distancia se mide en alturas de cuerpo, así no depende de la
    resolución ni de qué tan cerca esté la persona de la cámara.
  • Una persona NUEVA solo se confirma cuando se detecta en `min_hits` cuadros: una detección falsa de un solo cuadro no cuenta.
  • Una persona que deja de detectarse se recuerda `max_lost_s` segundos: si reaparece cerca de donde estaba, conserva su identificador.

No usa librerías externas (solo `math`): corre igual en el backend y en el agente. El motor lo usa a través de `ThreatScorer`.
"""
import math

MAX_DETECCIONES = 50            # tope de personas por cuadro: protege el costo de la asociación (N × M)
RELACION_TAMANO_MAX = 2.5       # una caja no puede ser más de 2,5 veces más alta o más baja que la de la misma persona


def _caja(det):
    """La caja de una detección como (x1, y1, x2, y2), o None si no es válida."""
    try:
        x1, y1, x2, y2 = (float(v) for v in det["bbox"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class Track:
    """Una persona seguida: su identificador, su última caja y cuántas veces se detectó."""

    __slots__ = ("id", "bbox", "cx", "cy", "h", "first_ts", "x0", "y0", "last_ts", "hits", "visible")

    def __init__(self, tid, caja, ts):
        self.id = tid
        self.first_ts = ts
        self.hits = 0
        self.visible = False
        self._poner(caja, ts, primera=True)
        self.x0, self.y0 = self.cx, self.cy             # dónde se vio por primera vez: desde ahí se cuenta el merodeo

    def _poner(self, caja, ts, primera=False):
        x1, y1, x2, y2 = caja
        altura = max(1.0, y2 - y1)
        self.bbox = (x1, y1, x2, y2)
        self.cx = (x1 + x2) / 2.0
        self.cy = (y1 + y2) / 2.0
        self.h = altura if primera else 0.5 * self.h + 0.5 * altura          # la altura se suaviza: la caja "respira" entre cuadros
        self.last_ts = ts


class PersonTracker:
    def __init__(self, max_lost_s=4.0, min_hits=2, gate_base_h=0.35, gate_speed_h=0.9, gate_max_h=2.5, tentative_ttl_s=1.5):
        self.max_lost_s = float(max_lost_s)
        self.min_hits = int(min_hits)
        self.gate_base_h = float(gate_base_h)          # distancia permitida aunque casi no pase el tiempo (ruido de la caja)
        self.gate_speed_h = float(gate_speed_h)        # alturas de cuerpo por segundo que se supone que puede recorrer una persona
        self.gate_max_h = float(gate_max_h)            # tope, para que una persona perdida mucho rato no "capture" a cualquiera
        self.tentative_ttl_s = float(tentative_ttl_s)  # una persona aún sin confirmar que no se vuelve a ver se olvida pronto
        self.tracks = {}
        self._next_id = 1
        self._ultimo = None         # cuándo fue la última actualización
        self._periodo = 0.0         # cada cuántos segundos llegan los cuadros (promedio): a 1 fps una sola detección perdida son 2 s

    def _vencido(self, t, ahora):
        sin_ver = ahora - t.last_ts
        espera = max(self.tentative_ttl_s, 2.5 * self._periodo)       # a ritmo lento, una persona sin confirmar aguanta al menos una detección perdida
        return sin_ver > self.max_lost_s or (t.hits < self.min_hits and sin_ver > espera)

    def update(self, detections, now_ts):
        """
        Asocia las detecciones del cuadro con las personas que ya se seguían y devuelve las personas CONFIRMADAS que se ven en este
        cuadro. A cada detección que se asocia se le anota su `track_id`. Las detecciones inválidas se ignoran.
        """
        if self._ultimo is not None and 0 < now_ts - self._ultimo <= 5.0:       # los saltos largos (una pausa) no cuentan como ritmo
            intervalo = now_ts - self._ultimo
            self._periodo = intervalo if self._periodo == 0.0 else 0.8 * self._periodo + 0.2 * intervalo
        self._ultimo = now_ts
        for t in self.tracks.values():
            t.visible = False
        for tid in [tid for tid, t in self.tracks.items() if self._vencido(t, now_ts)]:
            del self.tracks[tid]

        dets = []
        for d in list(detections or [])[:MAX_DETECCIONES]:
            caja = _caja(d)
            if caja is not None:
                dets.append((d, caja))

        candidatos = []
        for tid, t in self.tracks.items():
            dt = max(now_ts - t.last_ts, 1e-3)
            puerta = min(self.gate_max_h, self.gate_base_h + self.gate_speed_h * dt)
            for j, (_, caja) in enumerate(dets):
                altura = max(1.0, caja[3] - caja[1])
                if max(t.h, altura) / min(t.h, altura) > RELACION_TAMANO_MAX:
                    continue
                dist = math.hypot((caja[0] + caja[2]) / 2.0 - t.cx, (caja[1] + caja[3]) / 2.0 - t.cy) / max(t.h, altura)
                if dist > puerta:
                    continue
                candidatos.append((dist / puerta - 0.3 * iou(t.bbox, caja), tid, j))
        candidatos.sort()                                   # primero los pares más parecidos; los empates se resuelven por orden estable

        usados_t, usados_d = set(), set()
        for _, tid, j in candidatos:
            if tid in usados_t or j in usados_d:
                continue
            usados_t.add(tid)
            usados_d.add(j)
            t = self.tracks[tid]
            t._poner(dets[j][1], now_ts)
            t.hits += 1
            t.visible = True
            _anotar(dets[j][0], tid)

        for j, (d, caja) in enumerate(dets):
            if j in usados_d:
                continue
            t = Track(self._next_id, caja, now_ts)
            self._next_id += 1
            t.hits = 1
            t.visible = True
            self.tracks[t.id] = t
            _anotar(d, t.id)

        return [t for t in self.tracks.values() if t.visible and t.hits >= self.min_hits]


def _anotar(det, tid):
    try:
        det["track_id"] = tid
    except TypeError:
        pass
