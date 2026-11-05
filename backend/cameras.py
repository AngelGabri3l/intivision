"""
Cámaras de cada usuario: registro, permisos y consultas.

Una cámara es un REGISTRO (nombre y lugar): la imagen la aporta la cámara del navegador (computadora o
celular) desde «Analizar». Cada evento queda ligado a la cámara que lo generó.
"""
import datetime
import os
import re

from backend import db

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
           c.kind, c.name, c.place, c.source_type, c.admin_can_view, c.is_active, c.last_seen_at,
           u.full_name AS owner_name,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id) AS total,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id
               AND e.occurred_at >= (date_trunc('day', now() AT TIME ZONE 'America/Lima') AT TIME ZONE 'America/Lima')) AS hoy,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id AND e.occurred_at >= now() - interval '7 days') AS semana,
           (SELECT count(*) FROM events e WHERE e.camera_id = c.id AND e.level = 'ALTO') AS altos,
           (SELECT e.level FROM events e WHERE e.camera_id = c.id ORDER BY e.id DESC LIMIT 1) AS ultimo_nivel
    FROM cameras c LEFT JOIN users u ON u.id = c.user_id
"""


def _scope(user):
    """Qué cámaras puede ver este usuario: el residente, las suyas; el administrador, las de su comunidad
    que sean zonas comunes, suyas o que su dueño no le haya ocultado."""
    if user["role"] == "community_admin":
        return ("(c.community_id = %s AND (c.kind = 'common' OR c.user_id = %s OR c.admin_can_view))",
                [user["community_id"], user["id"]])
    return "c.user_id = %s", [user["id"]]


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


def usable_cameras(user):
    """Las cámaras con las que este usuario puede transmitir o subir videos: las suyas, activas."""
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id, name FROM cameras
                       WHERE user_id = %s AND kind = 'private' AND is_active ORDER BY created_at""", (user["id"],))
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ crear y usar

def create_camera(user, name, place):
    """(cámara, None) si se creó; (None, motivo) si no."""
    name, place = clean_text(name), clean_text(place)
    problema = name_issue(name) or place_issue(place)
    if problema:
        return None, problema
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM cameras WHERE user_id = %s", (user["id"],))
        if cur.fetchone()["n"] >= MAX_CAMERAS_PER_USER:
            return None, f"Llegaste al máximo de {MAX_CAMERAS_PER_USER} cámaras."
        cur.execute("""INSERT INTO cameras (community_id, user_id, kind, name, place, source_type, admin_can_view)
                       VALUES (%s, %s, 'private', %s, %s, 'browser', true)
                       ON CONFLICT (community_id, name) DO NOTHING RETURNING id::text AS id, name""",
                    (user["community_id"], user["id"], name, place or None))
        row = cur.fetchone()
    if row is None:
        return None, "Ya existe una cámara con ese nombre en tu comunidad. Elige otro."
    return dict(row), None


def default_camera(user):
    """La primera cámara del usuario; si no tiene ninguna, se le crea «Cámara de <nombre>»."""
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id, name FROM cameras
                       WHERE user_id = %s AND kind = 'private' AND is_active ORDER BY created_at LIMIT 1""", (user["id"],))
        row = cur.fetchone()
        if row:
            return dict(row)
        base = clean_text("Cámara de " + user["full_name"])[:NAME_MAX]
        for n in range(1, 100):
            nombre = base if n == 1 else f"{base[:NAME_MAX - 4]} {n}"
            cur.execute("""INSERT INTO cameras (community_id, user_id, kind, name, source_type, admin_can_view)
                           VALUES (%s, %s, 'private', %s, 'browser', true)
                           ON CONFLICT (community_id, name) DO NOTHING RETURNING id::text AS id, name""",
                        (user["community_id"], user["id"], nombre))
            row = cur.fetchone()
            if row:
                return dict(row)
    raise RuntimeError("No se pudo crear la cámara predeterminada.")


def resolve_camera(user, camera_id):
    """
    La cámara con la que se va a transmitir o subir un video. Sin indicar ninguna, la predeterminada.
    Solo vale una cámara propia y activa; cualquier otra (ajena, inexistente, mal escrita) devuelve None.
    """
    if camera_id is None or camera_id == "":
        return default_camera(user)
    if not is_uuid(camera_id):
        return None
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id, name FROM cameras
                       WHERE id = %s AND user_id = %s AND kind = 'private' AND is_active""", (camera_id, user["id"]))
        row = cur.fetchone()
    return dict(row) if row else None


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


def touch_camera(camera_id):
    """Marca que la cámara acaba de dar señal."""
    with db.cursor() as cur:
        cur.execute("UPDATE cameras SET last_seen_at = now() WHERE id = %s", (camera_id,))


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
