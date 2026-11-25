"""
Cámaras de cada usuario: registro, permisos y consultas.

Una cámara es un REGISTRO (nombre y lugar): la imagen la aporta la cámara del navegador (computadora o
celular) desde «Analizar». Cada evento queda ligado a la cámara que lo generó.
"""
import datetime
import os
import re

from backend import acceso, db, planes

MAX_CAMERAS_PER_USER = int(os.environ.get("MAX_CAMERAS_PER_USER", "10"))
NAME_MIN, NAME_MAX, PLACE_MAX = 2, 60, 80
LEVELS = ("BAJO", "MEDIO", "ALTO")
SOURCE_TEXT = {"browser": "Cámara web o celular", "rtsp": "Mini PC", "file": "Video"}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PUNCTUATION = " .,'()-_#/"


def is_uuid(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


# ------------------------------------------------------------------ nombres

def clean_text(value) -> str:
    """Quita los caracteres de control y los espacios repetidos."""
    if not isinstance(value, str):
        return ""
    value = "".join(ch for ch in value if ch.isprintable())
    return re.sub(r"\s+", " ", value).strip()


def text_issue(value, label, minimum, maximum):
    if len(value) < minimum:
        return f"{label} debe tener al menos {minimum} caracteres." if minimum > 0 else None
    if len(value) > maximum:
        return f"{label} no puede pasar de {maximum} caracteres."
    if any(not (ch.isalnum() or ch in _PUNCTUATION) for ch in value):
        return f"{label} solo puede llevar letras, números, espacios y . , ' ( ) - _ # /"
    return None


def name_issue(name):
    return text_issue(name, "El nombre", NAME_MIN, NAME_MAX)


def place_issue(place):
    return text_issue(place, "El lugar", 0, PLACE_MAX) if place else None


# ------------------------------------------------------------------ permisos y consultas

_SELECT = """
    SELECT c.id::text AS id, c.user_id::text AS user_id, c.community_id::text AS community_id,
           c.kind, c.name, c.place, c.source_type, c.admin_can_view, c.is_active, c.last_seen_at, c.ai_enabled,
           u.full_name AS owner_name,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id) AS total,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id
               AND e.occurred_at >= (date_trunc('day', now() AT TIME ZONE 'America/Lima') AT TIME ZONE 'America/Lima')) AS hoy,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id AND e.occurred_at >= now() - interval '7 days') AS semana,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id AND e.level = 'ALTO') AS altos,
           (SELECT e.level FROM events e WHERE e.camera_id = c.id ORDER BY e.id DESC LIMIT 1) AS ultimo_nivel,
           """ + planes.sql_es_premium("c") + """ AS premium
    FROM cameras c LEFT JOIN users u ON u.id = c.user_id
"""


def _scope(user):
    """Qué cámaras puede ver este usuario (las reglas de cada rol están en acceso.py)."""
    return acceso.posicional(acceso.camaras(user, "c"), user)


def list_cameras(user):
    scope, args = _scope(user)
    sql = _SELECT + " WHERE " + scope + " ORDER BY c.kind, u.full_name NULLS FIRST, c.name"
    with db.cursor() as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def get_camera(user, camera_id):
    """La cámara, si existe Y este usuario puede verla. En cualquier otro caso, None (no se distingue)."""
    if not is_uuid(camera_id):
        return None
    scope, args = _scope(user)
    sql = _SELECT + " WHERE c.id = %s AND " + scope
    with db.cursor() as cur:
        cur.execute(sql, [camera_id] + args)
        row = cur.fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ crear y usar


def edge_camera(edge, camera_id):
    """Una cámara que este equipo (mini PC) tiene asignada, o None."""
    if not is_uuid(camera_id):
        return None
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id FROM cameras
                       WHERE id = %s AND community_id = %s AND edge_device_id = %s AND is_active""",
                    (camera_id, edge["community_id"], edge["id"]))
        row = cur.fetchone()
    return dict(row) if row else None


def set_admin_visibility(user, camera_id, visible):
    """Solo el dueño decide si el administrador puede ver su cámara privada."""
    if not is_uuid(camera_id):
        return False
    with db.cursor() as cur:
        cur.execute("""UPDATE cameras SET admin_can_view = %s
                       WHERE id = %s AND user_id = %s AND kind = 'private' RETURNING id""",
                    (bool(visible), camera_id, user["id"]))
        return cur.fetchone() is not None


# ------------------------------------------------------------------ texto para la pantalla

def ago(moment):
    """«hace 5 min», «hace 3 h»... a partir de una fecha con zona horaria."""
    if moment is None:
        return "sin señal todavía"
    seconds = (datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds()
    if seconds < 90:
        return "hace un momento"
    if seconds < 3600:
        return f"hace {int(seconds // 60)} min"
    if seconds < 86400:
        return f"hace {int(seconds // 3600)} h"
    days = int(seconds // 86400)
    return f"hace {days} día" + ("" if days == 1 else "s")
