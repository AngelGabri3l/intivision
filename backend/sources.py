"""
Fuentes de cámara (H4): lo que cada mini PC reporta y cómo el administrador lo asigna.

  • La mini PC manda su INVENTARIO (POST /edge/inventory): las cámaras que ve, con su estado.
    Se guardan en camera_sources. Nunca se guarda una URL ni una contraseña.
  • La mini PC pide su CONFIGURACIÓN (GET /edge/config): solo las cámaras que ya tiene asignadas.
  • El administrador ve las fuentes (GET /admin/sources) y las ASIGNA a un vecino o a una zona común.
    Reasignar archiva la cámara anterior sin borrar sus eventos.
"""
import datetime
import hashlib
import json
import logging
import os
import re

import psycopg2.errors

from backend import cameras, db

log = logging.getLogger("guardian.sources")

MAX_SOURCES = 200                       # fuentes por inventario
MAX_BODY_BYTES = 256_000                # tamaño del inventario
KINDS = ("usb", "rtsp", "onvif")
STATES = ("available", "disconnected", "error")
_FORMAT_RE = re.compile(r"^[A-Za-z0-9_ .-]{1,16}$")
DESTINOS = ("private", "common")
ONLINE_SECONDS = int(os.environ.get("EDGE_ONLINE_SECONDS", "120"))    # una mini PC sin latido por más de esto se muestra «sin señal»


class AssignError(Exception):
    """Algo impide asignar: lleva el código HTTP, el mensaje para la persona y un código para el programa."""

    def __init__(self, status, message, code):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


# ------------------------------------------------------------------ validación del inventario

def _texto(valor, maximo, minimo=0, recortar=False):
    """Texto sin caracteres de control. None si no es texto o no cabe."""
    if not isinstance(valor, str):
        return None
    limpio = "".join(ch for ch in valor if ch.isprintable()).strip()
    if recortar:
        limpio = limpio[:maximo]
    if len(limpio) < minimo or len(limpio) > maximo:
        return None
    return limpio


def _caps(valor):
    """Solo se guardan los formatos y la resolución máxima: nada más, para no almacenar datos de más."""
    if valor is None:
        return {}, None
    if not isinstance(valor, dict):
        return None, "caps debe ser un objeto."
    limpio = {}
    formatos = valor.get("formats")
    if formatos is not None:
        if not isinstance(formatos, list) or len(formatos) > 20:
            return None, "caps.formats debe ser una lista de hasta 20 formatos."
        if any(not isinstance(f, str) or not _FORMAT_RE.match(f) for f in formatos):
            return None, "caps.formats tiene un formato inválido."
        limpio["formats"] = formatos
    for clave in ("max_width", "max_height"):
        numero = valor.get(clave)
        if numero is not None:
            if isinstance(numero, bool) or not isinstance(numero, int) or not 0 <= numero <= 20000:
                return None, f"caps.{clave} debe ser un entero entre 0 y 20000."
            limpio[clave] = numero
    return limpio, None


def validate_inventory(payload):
    """(inventario limpio, None) o (None, motivo). Los campos desconocidos se ignoran."""
    if not isinstance(payload, dict):
        return None, "El inventario debe ser un objeto JSON."
    version = payload.get("agent_version")
    if version is not None:
        version = _texto(version, 40)
        if version is None:
            return None, "agent_version no es válido."
    fuentes = payload.get("sources")
    if not isinstance(fuentes, list):
        return None, "sources debe ser una lista."
    if len(fuentes) > MAX_SOURCES:
        return None, f"Se aceptan hasta {MAX_SOURCES} fuentes por inventario."
    limpias, vistas = [], set()
    for n, f in enumerate(fuentes, 1):
        if not isinstance(f, dict):
            return None, f"La fuente {n} no es un objeto."
        clave = _texto(f.get("device_key"), 300, 1)
        kind, state = f.get("kind"), f.get("state")
        label = _texto(f.get("label"), 200, 1)
        if clave is None or "://" in clave:
            return None, f"La fuente {n}: device_key inválido."
        if kind not in KINDS:
            return None, f"La fuente {n}: kind debe ser {', '.join(KINDS)}."
        if not clave.startswith(kind + ":"):
            return None, f"La fuente {n}: device_key debe empezar por «{kind}:»."
        if state not in STATES:
            return None, f"La fuente {n}: state debe ser {', '.join(STATES)}."
        if label is None or "://" in label:
            return None, f"La fuente {n}: label inválido."
        if clave in vistas:
            return None, f"La fuente {n}: device_key repetido."
        caps, motivo = _caps(f.get("caps"))
        if motivo:
            return None, f"La fuente {n}: {motivo}"
        address = f.get("address")
        if address is not None:
            address = _texto(address, 100)
            if address is None or "://" in address or "@" in address:
                return None, f"La fuente {n}: address debe ser una IP o un nombre, sin URL ni credenciales."
        detail = f.get("detail")
        if detail is not None:
            detail = _texto(detail, 300, recortar=True)
            if detail is None:
                return None, f"La fuente {n}: detail no es un texto."
        vistas.add(clave)
        limpias.append({"key": clave, "kind": kind, "label": label, "state": state, "caps": caps,
                        "address": address or None, "detail": detail or None})
    return {"agent_version": version, "sources": limpias}, None


# ------------------------------------------------------------------ inventario de la mini PC

_UPSERT = """
    INSERT INTO camera_sources (community_id, edge_device_id, device_key, kind, label, state, caps,
                                address, detail, last_seen_at, reported_at)
    VALUES (%(community)s, %(edge)s, %(key)s, %(kind)s, %(label)s, %(state)s, %(caps)s::jsonb,
            %(address)s, %(detail)s, CASE WHEN %(state)s = 'available' THEN now() END, now())
    ON CONFLICT (edge_device_id, device_key) DO UPDATE SET
        kind = EXCLUDED.kind, label = EXCLUDED.label, state = EXCLUDED.state, caps = EXCLUDED.caps,
        address = EXCLUDED.address, detail = EXCLUDED.detail, reported_at = now(),
        last_seen_at = CASE WHEN EXCLUDED.state = 'available' THEN now() ELSE camera_sources.last_seen_at END
    RETURNING (xmax = 0) AS inserted"""


def upsert_inventory(edge, inventario):
    """
    Registra o actualiza las fuentes de esta mini PC, todo en una sola operación.
    Las fuentes que ya no vienen en la lista pasan a «disconnected» (no se borran: conservan su asignación).
    Las cámaras asignadas a una fuente disponible quedan marcadas como activas ahora.
    """
    nuevas = 0
    with db.cursor() as cur:
        for f in inventario["sources"]:
            cur.execute(_UPSERT, {"community": edge["community_id"], "edge": edge["id"], "key": f["key"],
                                  "kind": f["kind"], "label": f["label"], "state": f["state"],
                                  "caps": json.dumps(f["caps"]), "address": f["address"], "detail": f["detail"]})
            nuevas += 1 if cur.fetchone()["inserted"] else 0
        cur.execute("""UPDATE camera_sources SET state = 'disconnected', reported_at = now()
                       WHERE edge_device_id = %s AND NOT (device_key = ANY(%s::text[])) AND state <> 'disconnected'""",
                    (edge["id"], [f["key"] for f in inventario["sources"]]))
        cur.execute("""UPDATE cameras c SET last_seen_at = now()
                       FROM camera_sources s
                       WHERE c.source_id = s.id AND s.edge_device_id = %s AND s.state = 'available' AND c.is_active""",
                    (edge["id"],))
        if inventario["agent_version"]:
            cur.execute("UPDATE edge_devices SET version = %s WHERE id = %s", (inventario["agent_version"], edge["id"]))
    return {"sources": len(inventario["sources"]), "new": nuevas}


# ------------------------------------------------------------------ configuración de la mini PC

def _sensitive_hours(valor):
    """[hora de inicio, hora de fin] (0 a 23), o None si la comunidad no definió un horario válido."""
    if (isinstance(valor, (list, tuple)) and len(valor) == 2
            and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in valor)):
        return [valor[0], valor[1]]
    return None


def edge_config(edge):
    """Lo que esta mini PC debe analizar: solo sus cámaras asignadas y activas, y el horario sensible."""
    with db.cursor() as cur:
        cur.execute("""SELECT c.id::text AS camera_id, s.device_key, c.name, c.kind, c.restricted_zone
                       FROM cameras c JOIN camera_sources s ON s.id = c.source_id
                       WHERE c.edge_device_id = %s AND c.community_id = %s AND c.is_active
                       ORDER BY c.created_at, c.id""", (edge["id"], edge["community_id"]))
        camaras = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT settings FROM communities WHERE id = %s", (edge["community_id"],))
        fila = cur.fetchone()
    ajustes = fila["settings"] if fila and isinstance(fila["settings"], dict) else {}
    cuerpo = {"cameras": camaras, "sensitive_hours": _sensitive_hours(ajustes.get("sensitive_hours"))}
    huella = hashlib.sha256(json.dumps(cuerpo, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    ahora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": huella, "generated_at": ahora, **cuerpo}


# ------------------------------------------------------------------ lo que ve el administrador

def _iso(valor):
    return valor.isoformat() if isinstance(valor, (datetime.datetime, datetime.date)) else valor


def list_sources(admin):
    """Las fuentes de la comunidad del administrador, con su estado y a quién están asignadas."""
    with db.cursor() as cur:
        cur.execute("""SELECT s.id::text AS id, s.device_key, s.kind, s.label, s.state, s.caps, s.address, s.detail,
                              s.first_seen_at, s.last_seen_at, d.id::text AS edge_id, d.name AS edge_name,
                              c.id::text AS camera_id, c.name AS camera_name, c.kind AS camera_kind,
                              c.is_active AS camera_active, u.id::text AS owner_id, u.full_name AS owner_name
                       FROM camera_sources s
                       JOIN edge_devices d ON d.id = s.edge_device_id
                       LEFT JOIN cameras c ON c.source_id = s.id
                       LEFT JOIN users u ON u.id = c.user_id
                       WHERE s.community_id = %s
                       ORDER BY d.name, s.label, s.device_key""", (admin["community_id"],))
        filas = [dict(r) for r in cur.fetchall()]
    for f in filas:
        f["first_seen_at"], f["last_seen_at"] = _iso(f["first_seen_at"]), _iso(f["last_seen_at"])
    return filas


def _estado_equipo(status, last_seen, segundos):
    if status == "revoked":
        return "revoked"
    if last_seen is None:
        return "pending"
    return "online" if segundos is not None and segundos <= ONLINE_SECONDS else "offline"


def list_equipos(admin):
    """
    Lo que muestra el panel «Equipos y cámaras» (H5): cada mini PC de la comunidad con su estado, versión y última señal,
    sus cámaras con a quién está asignada cada una, y las personas a quienes se puede asignar.
    """
    comunidad = admin["community_id"]
    with db.cursor() as cur:
        cur.execute("""SELECT d.id::text AS id, d.name, d.status, d.version, d.last_seen_at,
                              CASE WHEN d.last_seen_at IS NULL THEN NULL
                                   ELSE GREATEST(0, EXTRACT(EPOCH FROM (now() - d.last_seen_at)))::bigint END AS seconds_since
                       FROM edge_devices d WHERE d.community_id = %s ORDER BY lower(d.name), d.name""", (comunidad,))
        equipos = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT s.id::text AS id, s.edge_device_id::text AS edge_id, s.kind, s.label, s.state, s.caps, s.address,
                              s.last_seen_at, c.id::text AS camera_id, c.name AS camera_name, c.kind AS camera_kind,
                              c.place AS camera_place, u.id::text AS owner_id, u.full_name AS owner_name
                       FROM camera_sources s
                       LEFT JOIN cameras c ON c.source_id = s.id AND c.is_active
                       LEFT JOIN users u ON u.id = c.user_id
                       WHERE s.community_id = %s ORDER BY lower(s.label), s.device_key""", (comunidad,))
        fuentes = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT id::text AS id, full_name, role FROM users
                       WHERE community_id = %s AND status = 'active'
                       ORDER BY (role = 'community_admin'), lower(full_name) LIMIT 500""", (comunidad,))
        personas = [dict(r) for r in cur.fetchall()]
    por_equipo = {}
    for f in fuentes:
        caps = f["caps"] if isinstance(f["caps"], dict) else {}
        ancho, alto = caps.get("max_width"), caps.get("max_height")
        camara = None
        if f["camera_id"]:
            camara = {"id": f["camera_id"], "name": f["camera_name"], "kind": f["camera_kind"], "place": f["camera_place"],
                      "owner_id": f["owner_id"], "owner_name": f["owner_name"]}
        por_equipo.setdefault(f["edge_id"], []).append({
            "id": f["id"], "label": f["label"], "kind": f["kind"], "state": f["state"], "address": f["address"],
            "resolution": f"{ancho}×{alto}" if ancho and alto else None, "last_seen_at": _iso(f["last_seen_at"]),
            "camera": camara})
    for e in equipos:
        e["state"] = _estado_equipo(e["status"], e["last_seen_at"], e["seconds_since"])
        e["last_seen_at"] = _iso(e["last_seen_at"])
        e["sources"] = por_equipo.get(e["id"], [])
    return {"equipos": equipos, "personas": personas, "online_seconds": ONLINE_SECONDS,
            "limites": {"name_min": cameras.NAME_MIN, "name_max": cameras.NAME_MAX, "place_max": cameras.PLACE_MAX}}


# ------------------------------------------------------------------ asignación

def _validate_assignment(cur, admin, data):
    if not isinstance(data, dict):
        raise AssignError(422, "El cuerpo debe ser un objeto JSON.", "datos_invalidos")
    destino = data.get("destino")
    if destino not in DESTINOS:
        raise AssignError(422, "destino debe ser «private» (de un vecino) o «common» (zona común).", "datos_invalidos")
    name, place = cameras.clean_text(data.get("name")), cameras.clean_text(data.get("place"))
    problema = cameras.name_issue(name) or cameras.place_issue(place)
    if problema:
        raise AssignError(422, problema, "datos_invalidos")
    owner = None
    if destino == "private":
        owner = data.get("owner_id")
        if not cameras.is_uuid(owner):
            raise AssignError(422, "Indica el dueño de la cámara (owner_id).", "datos_invalidos")
        cur.execute("SELECT id::text AS id FROM users WHERE id = %s AND community_id = %s AND status = 'active'",
                    (owner, admin["community_id"]))
        if cur.fetchone() is None:
            raise AssignError(422, "Esa persona no existe en tu comunidad o no está activa.", "datos_invalidos")
    elif data.get("owner_id") not in (None, ""):
        raise AssignError(422, "Una cámara de zona común no tiene dueño.", "datos_invalidos")
    return {"destino": destino, "name": name, "place": place or None, "owner_id": owner}


def assign_source(admin, source_id, data, reassign=False, ip=None):
    """
    Convierte una fuente en una cámara con dueño (privada) o común. Todo ocurre en una sola transacción:
    si algo falla, no queda nada a medias (por ejemplo, no se archiva la cámara anterior sin crear la nueva).
    Una fuente de otra comunidad responde igual que una que no existe.
    Devuelve {"camera_id", "archived_camera_id"}; lanza AssignError si no se puede.
    """
    if not cameras.is_uuid(source_id):
        raise AssignError(404, "Esa fuente no existe.", "no_existe")
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT id::text AS id, edge_device_id::text AS edge_device_id, device_key, state
                           FROM camera_sources WHERE id = %s AND community_id = %s FOR UPDATE""",
                        (source_id, admin["community_id"]))
            fuente = cur.fetchone()
            if fuente is None:
                raise AssignError(404, "Esa fuente no existe.", "no_existe")
            cur.execute("SELECT id::text AS id FROM cameras WHERE source_id = %s FOR UPDATE", (source_id,))
            actual = cur.fetchone()
            if actual and not reassign:
                raise AssignError(409, "Esa fuente ya está asignada. Usa reasignar para cambiarla.", "ya_asignada")
            datos = _validate_assignment(cur, admin, data)
            archivada = None
            if actual:
                # Se archiva sin borrar nada: deja de estar activa y libera la fuente y el nombre.
                cur.execute("""UPDATE cameras SET is_active = false, source_id = NULL,
                                      name = left(name, 35) || ' (archivada ' || to_char(now(), 'YYYY-MM-DD')
                                             || ' ' || left(id::text, 8) || ')'
                               WHERE id = %s""", (actual["id"],))
                archivada = actual["id"]
            if datos["owner_id"]:
                cur.execute("SELECT count(*) AS n FROM cameras WHERE user_id = %s AND is_active", (datos["owner_id"],))
                if cur.fetchone()["n"] >= cameras.MAX_CAMERAS_PER_USER:
                    raise AssignError(422, f"Esa persona ya tiene el máximo de {cameras.MAX_CAMERAS_PER_USER} cámaras.",
                                      "limite")
            cur.execute("""INSERT INTO cameras (community_id, user_id, edge_device_id, kind, name, place, source_type,
                                                admin_can_view, source_id, last_seen_at)
                           VALUES (%s, %s, %s, %s, %s, %s, 'rtsp', true, %s,
                                   CASE WHEN %s = 'available' THEN now() END)
                           ON CONFLICT (community_id, name) DO NOTHING RETURNING id::text AS id""",
                        (admin["community_id"], datos["owner_id"], fuente["edge_device_id"], datos["destino"],
                         datos["name"], datos["place"], source_id, fuente["state"]))
            nueva = cur.fetchone()
            if nueva is None:
                raise AssignError(409, "Ya existe una cámara con ese nombre en la comunidad. Elige otro.",
                                  "nombre_duplicado")
            meta = {"camera_id": nueva["id"], "destino": datos["destino"], "device_key": fuente["device_key"],
                    "edge_device_id": fuente["edge_device_id"], "previous_camera_id": archivada}
            cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action,
                                                  entity, entity_id, ip, meta)
                           VALUES ('user', %s, %s, %s, %s, 'camera_source', %s, %s, %s::jsonb)""",
                        (admin["id"], admin["community_id"], datos["owner_id"],
                         "reassign_source" if reassign else "assign_source", source_id, ip, json.dumps(meta)))
    except psycopg2.errors.UniqueViolation:
        # Respaldo: si dos administradores asignan a la vez, el índice único de source_id deja pasar a uno solo.
        raise AssignError(409, "Esa fuente ya está asignada. Usa reasignar para cambiarla.", "ya_asignada")
    return {"camera_id": nueva["id"], "archived_camera_id": archivada,
            "_aviso": {"owner_id": datos["owner_id"], "name": datos["name"], "place": datos["place"]}}


def unassign_source(admin, source_id, ip=None):
    """
    Quita la asignación de una fuente: archiva su cámara (deja de estar activa, libera la fuente y el nombre) sin borrar
    ni un evento. La mini PC deja de analizarla en su siguiente latido. Queda en audit_log.
    Una fuente de otra comunidad responde igual que una que no existe; una que no está asignada responde 409.
    """
    if not cameras.is_uuid(source_id):
        raise AssignError(404, "Esa fuente no existe.", "no_existe")
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id, edge_device_id::text AS edge_device_id, device_key
                       FROM camera_sources WHERE id = %s AND community_id = %s FOR UPDATE""",
                    (source_id, admin["community_id"]))
        fuente = cur.fetchone()
        if fuente is None:
            raise AssignError(404, "Esa fuente no existe.", "no_existe")
        cur.execute("SELECT id::text AS id, user_id::text AS user_id FROM cameras WHERE source_id = %s FOR UPDATE", (source_id,))
        actual = cur.fetchone()
        if actual is None:
            raise AssignError(409, "Esa fuente no está asignada.", "sin_asignar")
        cur.execute("""UPDATE cameras SET is_active = false, source_id = NULL,
                              name = left(name, 35) || ' (archivada ' || to_char(now(), 'YYYY-MM-DD')
                                     || ' ' || left(id::text, 8) || ')'
                       WHERE id = %s""", (actual["id"],))
        meta = {"camera_id": actual["id"], "device_key": fuente["device_key"], "edge_device_id": fuente["edge_device_id"]}
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action,
                                              entity, entity_id, ip, meta)
                       VALUES ('user', %s, %s, %s, 'unassign_source', 'camera_source', %s, %s, %s::jsonb)""",
                    (admin["id"], admin["community_id"], actual["user_id"], source_id, ip, json.dumps(meta)))
    return {"archived_camera_id": actual["id"]}
