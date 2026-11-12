"""
Revisión del administrador, «human in the loop» (H6): la parte que decide la base de datos.

Una alerta clave de una zona común no avisa de entrada a todos los vecinos: primero la ve un administrador, que la
CONFIRMA (se avisa a los vecinos y suena la sirena) o la DESCARTA (falsa alarma: no se avisa a nadie más). Si nadie
responde en el tiempo configurado, se ESCALA: se avisa a todos, diciendo que no fue verificada.

Qué entra a revisión (solo si la comunidad lo activa con settings.hitl_enabled):
    • una cámara COMÚN (las privadas siguen avisando solo a su dueño) y
    • un evento ALTO, o MEDIO con alguna señal de agresión (settings.hitl_signals, por defecto «manos arriba» y «en el suelo»).
Solo hay UNA revisión abierta por cámara: mientras un administrador no la resuelve (o no se escala), los eventos nuevos de esa
misma cámara no abren otra (así una persona que se queda 1 minuto no genera 6 mensajes con botones).

Los cambios de estado son atómicos (UPDATE ... WHERE status = 'sin verificar'): si dos administradores responden a la vez,
gana el primero. Todo queda en audit_log.

Este módulo no habla con Telegram: eso es de backend/alerts.py. Aquí solo se guarda y se consulta.
"""
from __future__ import annotations

import json
import logging
import re

from backend import db

log = logging.getLogger("hitl")

DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S = 180, 30, 3600
DEFAULT_SIGNALS = ("hands_up", "fallen")
DECISIONS = {"confirmar": "verificado", "descartar": "falsa alarma"}
PENDING_LIMIT = 100
_SIGNAL_RE = re.compile(r"^[a-z_]{1,40}$")


class ReviewError(Exception):
    """Una revisión que no se puede hacer. `status` es el código HTTP que corresponde; `code` sirve para el programa."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ qué dice la comunidad

def community_settings(cur, community_id) -> dict:
    """La configuración de la revisión de una comunidad. Cualquier valor raro se reemplaza por el de fábrica."""
    cur.execute("SELECT settings FROM communities WHERE id = %s", (str(community_id),))
    fila = cur.fetchone()
    ajustes = (fila["settings"] if fila else None) or {}
    if not isinstance(ajustes, dict):
        ajustes = {}
    tiempo = ajustes.get("hitl_timeout_s")
    if isinstance(tiempo, bool) or not isinstance(tiempo, int) or not MIN_TIMEOUT_S <= tiempo <= MAX_TIMEOUT_S:
        tiempo = DEFAULT_TIMEOUT_S
    senales = ajustes.get("hitl_signals")
    if not isinstance(senales, list) or not all(isinstance(s, str) and _SIGNAL_RE.match(s) for s in senales):
        senales = list(DEFAULT_SIGNALS)
    return {"enabled": ajustes.get("hitl_enabled") is True, "timeout_s": tiempo, "signals": senales}


def review_decision(cur, event_id, community_id, camera_id, level):
    """
    ¿Qué se hace con el aviso de este evento de una cámara común?
        None         → como siempre (la revisión está apagada, o el evento no la necesita)
        "cubierto"   → ya hay una revisión abierta de esta cámara: no se avisa otra vez
        un entero    → hay que revisarlo; es el tiempo (segundos) que se espera antes de escalar
    """
    if level not in ("MEDIO", "ALTO") or not camera_id:
        return None
    cfg = community_settings(cur, community_id)
    if not cfg["enabled"]:
        return None
    if level == "MEDIO":
        cur.execute("SELECT 1 FROM event_signals WHERE event_id = %s AND signal_code = ANY(%s) LIMIT 1", (event_id, cfg["signals"]))
        if cur.fetchone() is None:
            return None
    cur.execute("""SELECT 1 FROM events WHERE camera_id = %s AND id <> %s AND status = 'sin verificar'
                     AND escalate_at IS NOT NULL AND escalated_at IS NULL AND escalate_at > now() LIMIT 1""",
                (str(camera_id), event_id))
    if cur.fetchone():
        return "cubierto"
    return cfg["timeout_s"]


def start_review(cur, event_id, timeout_s: int) -> None:
    cur.execute("UPDATE events SET escalate_at = now() + make_interval(secs => %s) WHERE id = %s", (timeout_s, event_id))


# ------------------------------------------------------------------ revisar

def review_event(admin, event_id, decision, via: str, ip=None, on_applied=None) -> dict:
    """
    Confirma o descarta un evento que está esperando revisión. `admin` es un administrador de la comunidad del evento.

    Devuelve {"applied": True, ...datos del evento...} si ESTA llamada lo resolvió, o {"applied": False, "estado": ...} si ya
    lo había resuelto otra persona (gana la primera). Si el evento no existe, es de otra comunidad o no estaba en revisión,
    lanza ReviewError(404): se responde igual que ante algo que no existe.
    `on_applied(cur, resultado)` se ejecuta DENTRO de la misma transacción: ahí se dejan en cola los avisos, para que no se
    pierdan aunque el servidor se caiga justo después.
    """
    nuevo = DECISIONS.get(decision)
    if nuevo is None:
        raise ReviewError(422, "decision_invalida", "La decisión debe ser «confirmar» o «descartar».")
    texto = str(event_id)
    if not texto.isdigit() or len(texto) > 18:
        raise ReviewError(404, "no_encontrado", "No existe esa alerta.")
    with db.cursor() as cur:
        cur.execute("""UPDATE events SET status = %s, reviewed_by = %s, reviewed_at = now()
                       WHERE id = %s AND community_id = %s AND status = 'sin verificar' AND escalate_at IS NOT NULL
                       RETURNING id, level, camera_id::text AS camera_id, community_id::text AS community_id,
                                 escalated_at IS NOT NULL AS escalada""",
                    (nuevo, str(admin["id"]), int(texto), str(admin["community_id"])))
        fila = cur.fetchone()
        if fila is None:
            cur.execute("""SELECT status FROM events WHERE id = %s AND community_id = %s AND escalate_at IS NOT NULL""",
                        (int(texto), str(admin["community_id"])))
            existente = cur.fetchone()
            if existente is None:
                raise ReviewError(404, "no_encontrado", "No existe esa alerta.")
            return {"applied": False, "estado": existente["status"], "event_id": int(texto)}
        resultado = {"applied": True, "estado": nuevo, "decision": decision, "event_id": fila["id"], "level": fila["level"],
                     "camera_id": fila["camera_id"], "community_id": fila["community_id"], "escalada": fila["escalada"], "via": via,
                     "admin_id": str(admin["id"])}
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id, ip, meta)
                       VALUES ('user', %s, %s, 'review_event', 'event', %s, %s, %s::jsonb)""",
                    (str(admin["id"]), str(admin["community_id"]), str(fila["id"]), ip,
                     json.dumps({"decision": decision, "estado": nuevo, "via": via, "escalada": fila["escalada"]})))
        if on_applied:
            on_applied(cur, resultado)
    return resultado


# ------------------------------------------------------------------ lo que está esperando

def pending(admin) -> dict:
    """Las alertas de la comunidad que esperan revisión, de la más urgente a la menos urgente (para la pantalla de H9)."""
    with db.cursor() as cur:
        cur.execute("""
            SELECT e.id, e.level, e.score,
                   to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD"T"HH24:MI:SS') AS occurred_at,
                   GREATEST(0, CEIL(EXTRACT(EPOCH FROM (e.escalate_at - now()))))::int AS seconds_left,
                   e.escalated_at IS NOT NULL AS escalated,
                   c.id::text AS camera_id, c.name AS camera_name, c.place AS camera_place,
                   COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code)
                             FROM event_signals es JOIN signals s ON s.code = es.signal_code
                             WHERE es.event_id = e.id), '') AS reasons,
                   (SELECT m.storage_key FROM media_files m WHERE m.event_id = e.id AND m.kind = 'image'
                     ORDER BY m.created_at LIMIT 1) AS image
            FROM events e LEFT JOIN cameras c ON c.id = e.camera_id
            WHERE e.community_id = %s AND e.status = 'sin verificar' AND e.escalate_at IS NOT NULL
            ORDER BY e.escalated_at IS NOT NULL, e.escalate_at, e.id
            LIMIT %s""", (str(admin["community_id"]), PENDING_LIMIT))
        filas = [dict(r) for r in cur.fetchall()]
    for f in filas:
        f["image_url"] = f"/evidence/{f['image']}" if f["image"] else None
        del f["image"]
    return {"count": len(filas), "events": filas}


def pending_count(admin) -> int:
    """Cuántas alertas esperan revisión en la comunidad (para el número del menú). Usa el índice parcial de la migración 003."""
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM events
                       WHERE community_id = %s AND status = 'sin verificar' AND escalate_at IS NOT NULL""",
                    (str(admin["community_id"]),))
        return int(cur.fetchone()["n"])


def escalate_due(on_escalated=None, limit: int = 20) -> list:
    """
    Marca como escaladas las revisiones cuyo tiempo se cumplió y que nadie resolvió. Cada una se escala UNA sola vez.
    `on_escalated(cur, evento)` corre dentro de la misma transacción (ahí se dejan en cola los avisos a los vecinos).
    """
    with db.cursor() as cur:
        cur.execute("""
            WITH vencidas AS (
                SELECT id FROM events
                WHERE status = 'sin verificar' AND escalate_at IS NOT NULL AND escalate_at <= now() AND escalated_at IS NULL
                ORDER BY escalate_at LIMIT %s FOR UPDATE SKIP LOCKED)
            UPDATE events e SET escalated_at = now() FROM vencidas v WHERE e.id = v.id
            RETURNING e.id, e.community_id::text AS community_id, e.level, e.camera_id::text AS camera_id""", (limit,))
        filas = [dict(r) for r in cur.fetchall()]
        for f in filas:
            cur.execute("""INSERT INTO audit_log (actor_type, community_id, action, entity, entity_id, meta)
                           VALUES ('system', %s, 'escalate_event', 'event', %s, '{}'::jsonb)""", (f["community_id"], str(f["id"])))
            if on_escalated:
                on_escalated(cur, f)
    return filas
