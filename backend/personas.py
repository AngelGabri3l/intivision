"""
Gestión de personas de una comunidad.

  Administrador  invita vecinos, administradores y vigilantes; suspende y reactiva; cambia el rol; genera un enlace para cambiar la
                 contraseña; cierra sus sesiones; da de baja. Nunca sobre sí mismo (eso lo hace desde su propio menú) y siempre
                 queda al menos un administrador activo.
  Vecino         invita familiares (hasta MAX_FAMILIARES) y decide qué cámaras privadas suyas ve cada uno; puede quitarlos.

Invitar crea un enlace de un solo uso que vale INVITACION_HORAS; se guarda solo su huella. Quien invita lo comparte (WhatsApp,
mensaje...). Al abrirlo, la persona elige su contraseña, acepta el aviso de privacidad y su cuenta queda activa.
Todo queda en audit_log.
"""
from __future__ import annotations

import json
import re
import secrets

from backend import auth, cuenta

INVITACION_HORAS = 72
MAX_FAMILIARES = 5
ROLES_QUE_INVITA_EL_ADMIN = ("resident", "community_admin", "guard")
_CORREO = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_TELEFONO = re.compile(r"^\+?[0-9]{7,15}$")


class ErrorPersonas(Exception):
    def __init__(self, mensaje, codigo="invalido", status=400):
        super().__init__(mensaje)
        self.mensaje, self.codigo, self.status = mensaje, codigo, status


def _auditar(cur, actor, accion, sujeto=None, meta=None):
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, meta)
                   VALUES ('user', %s, %s, %s, %s, 'user', %s, %s::jsonb)""",
                (str(actor["id"]), actor["community_id"], sujeto, accion, str(sujeto or actor["id"]), json.dumps(meta or {})))


def _limpiar_contacto(email, phone):
    email = (email or "").strip().lower() or None
    phone = re.sub(r"[\s-]", "", phone or "") or None
    if email and (len(email) > 254 or not _CORREO.match(email)):
        raise ErrorPersonas("El correo no es válido.")
    if phone and not _TELEFONO.match(phone):
        raise ErrorPersonas("El teléfono debe tener entre 7 y 15 dígitos (puede empezar con +).")
    if not email and not phone:
        raise ErrorPersonas("Indica un correo o un teléfono.")
    return email, phone


def _contacto_libre(cur, community_id, email, phone):
    cur.execute("""SELECT 1 FROM users WHERE (%s::text IS NOT NULL AND lower(email) = %s) OR (%s::text IS NOT NULL AND phone = %s)""",
                (email, email, phone, phone))
    if cur.fetchone():
        raise ErrorPersonas("Ya existe una cuenta con ese correo o teléfono.", "duplicado", 409)
    cur.execute("""SELECT 1 FROM invitations WHERE community_id = %s AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()
                     AND ((%s::text IS NOT NULL AND lower(email) = %s) OR (%s::text IS NOT NULL AND phone = %s))""",
                (community_id, email, email, phone, phone))
    if cur.fetchone():
        raise ErrorPersonas("Ya hay una invitación pendiente para ese contacto: reenvíala o revócala.", "duplicado", 409)


def _camaras_propias(cur, dueno_id, ids) -> list[str]:
    ids = [i for i in (ids or []) if isinstance(i, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", i)]
    if not ids:
        return []
    cur.execute("""SELECT id::text AS id FROM cameras WHERE id = ANY(%s::uuid[]) AND user_id = %s AND kind = 'private' AND is_active""",
                (ids, dueno_id))
    propias = {r["id"] for r in cur.fetchall()}
    if len(propias) != len(set(ids)):
        raise ErrorPersonas("Solo puedes compartir tus propias cámaras privadas.", "prohibido", 403)
    return sorted(propias)


# ------------------------------------------------------------------ invitaciones

def invitar(cur, actor, nombre, email, phone, rol, camaras=None):
    """Crea la invitación. Devuelve (id, token). El token se muestra una sola vez a quien invita."""
    nombre = " ".join((nombre or "").split())[:120]
    if not nombre:
        raise ErrorPersonas("Escribe el nombre de la persona.")
    email, phone = _limpiar_contacto(email, phone)
    sponsor, ids = None, []
    if rol == "family":
        if actor["role"] not in ("resident", "community_admin"):
            raise ErrorPersonas("Tu cuenta no puede invitar familiares.", "prohibido", 403)
        cur.execute("""SELECT (SELECT count(*) FROM users WHERE sponsor_id = %(u)s AND role = 'family' AND status = 'active')
                            + (SELECT count(*) FROM invitations WHERE sponsor_id = %(u)s AND accepted_at IS NULL AND revoked_at IS NULL
                                                                 AND expires_at > now()) AS n""", {"u": actor["id"]})
        if cur.fetchone()["n"] >= MAX_FAMILIARES:
            raise ErrorPersonas(f"Puedes tener hasta {MAX_FAMILIARES} familiares (incluidas las invitaciones pendientes).", "limite", 422)
        sponsor, ids = str(actor["id"]), _camaras_propias(cur, actor["id"], camaras)
    else:
        if actor["role"] != "community_admin":
            raise ErrorPersonas("Solo el administrador invita vecinos, administradores y vigilantes.", "prohibido", 403)
        if rol not in ROLES_QUE_INVITA_EL_ADMIN:
            raise ErrorPersonas("Rol no válido.")
    _contacto_libre(cur, actor["community_id"], email, phone)
    token = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO invitations (community_id, invited_by, role, full_name, email, phone, sponsor_id, camera_ids, token_hash, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, now() + make_interval(hours => %s)) RETURNING id::text AS id""",
                (actor["community_id"], actor["id"], rol, nombre, email, phone, sponsor, ids, cuenta.huella(token), INVITACION_HORAS))
    inv = cur.fetchone()["id"]
    _auditar(cur, actor, "invite_created", meta={"rol": rol, "invitacion": inv, "camaras": len(ids)})
    return inv, token


def _invitacion_de(cur, actor, inv_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", inv_id or ""):
        return None
    cur.execute("""SELECT * FROM invitations WHERE id = %s AND community_id = %s AND accepted_at IS NULL AND revoked_at IS NULL""",
                (inv_id, actor["community_id"]))
    inv = cur.fetchone()
    if inv is None:
        return None
    es_suya = inv["sponsor_id"] is not None and str(inv["sponsor_id"]) == str(actor["id"])
    if actor["role"] == "community_admin" and inv["role"] != "family":
        return inv
    return inv if es_suya else None


def revocar(cur, actor, inv_id) -> bool:
    inv = _invitacion_de(cur, actor, inv_id)
    if inv is None:
        return False
    cur.execute("UPDATE invitations SET revoked_at = now() WHERE id = %s", (inv["id"],))
    _auditar(cur, actor, "invite_revoked", meta={"invitacion": str(inv["id"])})
    return True


def reenviar(cur, actor, inv_id) -> str | None:
    """Anula la invitación y crea otra con los mismos datos (para cuando el enlace se perdió o venció). Devuelve el token nuevo."""
    inv = _invitacion_de(cur, actor, inv_id)
    if inv is None:
        return None
    cur.execute("UPDATE invitations SET revoked_at = now() WHERE id = %s", (inv["id"],))
    token = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO invitations (community_id, invited_by, role, full_name, email, phone, sponsor_id, camera_ids, token_hash, expires_at)
                   SELECT community_id, %s, role, full_name, email, phone, sponsor_id, camera_ids, %s, now() + make_interval(hours => %s)
                   FROM invitations WHERE id = %s""", (actor["id"], cuenta.huella(token), INVITACION_HORAS, inv["id"]))
    _auditar(cur, actor, "invite_resent", meta={"invitacion": str(inv["id"])})
    return token


def pendientes(cur, actor) -> list[dict]:
    if actor["role"] == "community_admin":
        cur.execute("""SELECT i.id::text AS id, i.role, i.full_name, i.email, i.phone, i.expires_at, i.expires_at < now() AS vencida,
                              u.full_name AS invitado_por, s.full_name AS vecino
                       FROM invitations i LEFT JOIN users u ON u.id = i.invited_by LEFT JOIN users s ON s.id = i.sponsor_id
                       WHERE i.community_id = %s AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.role <> 'family'
                       ORDER BY i.created_at DESC LIMIT 100""", (actor["community_id"],))
    else:
        cur.execute("""SELECT i.id::text AS id, i.role, i.full_name, i.email, i.phone, i.expires_at, i.expires_at < now() AS vencida,
                              NULL AS invitado_por, NULL AS vecino
                       FROM invitations i WHERE i.sponsor_id = %s AND i.accepted_at IS NULL AND i.revoked_at IS NULL
                       ORDER BY i.created_at DESC LIMIT 20""", (actor["id"],))
    return [dict(r) for r in cur.fetchall()]


def invitacion_valida(cur, token):
    cur.execute("""SELECT i.*, c.name AS comunidad, u.full_name AS invitado_por FROM invitations i
                   JOIN communities c ON c.id = i.community_id LEFT JOIN users u ON u.id = i.invited_by
                   WHERE i.token_hash = %s AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now()""", (cuenta.huella(token),))
    inv = cur.fetchone()
    if inv and inv["sponsor_id"]:
        cur.execute("SELECT 1 FROM users WHERE id = %s AND status = 'active'", (inv["sponsor_id"],))
        if cur.fetchone() is None:
            return None                                  # el vecino que lo invitó ya no está activo
    return inv


def aceptar(cur, token, clave, confirmacion, acepta, ip=None):
    """Crea la cuenta desde la invitación. Devuelve el usuario creado (id, community_id, role). Lanza ErrorPersonas."""
    inv = invitacion_valida(cur, token)
    if inv is None:
        raise ErrorPersonas("Esta invitación ya no sirve. Pide una nueva a quien te invitó.", "invalida", 404)
    if not acepta:
        raise ErrorPersonas("Para crear tu cuenta debes aceptar el aviso de privacidad.", "privacidad", 422)
    if clave != confirmacion:
        raise ErrorPersonas("Las dos contraseñas no coinciden.", "clave", 422)
    problema = auth.password_issue(clave, inv["email"], inv["phone"], inv["full_name"])
    if problema:
        raise ErrorPersonas(problema, "clave", 422)
    cur.execute("UPDATE invitations SET accepted_at = now() WHERE id = %s AND accepted_at IS NULL RETURNING id", (inv["id"],))
    if cur.fetchone() is None:
        raise ErrorPersonas("Esta invitación ya se usó.", "invalida", 404)
    cur.execute("SAVEPOINT crear_usuario")
    try:
        cur.execute("""INSERT INTO users (community_id, role, full_name, email, phone, password_hash, sponsor_id, invited_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text AS id, community_id::text AS community_id, role""",
                    (inv["community_id"], inv["role"], inv["full_name"], inv["email"], inv["phone"], auth.hash_password(clave),
                     inv["sponsor_id"], inv["invited_by"]))
    except Exception as e:                                # noqa: BLE001 - correo o teléfono tomados entretanto
        cur.execute("ROLLBACK TO SAVEPOINT crear_usuario")
        raise ErrorPersonas("Ya existe una cuenta con ese correo o teléfono.", "duplicado", 409) from e
    nuevo = dict(cur.fetchone())
    cur.execute("UPDATE invitations SET accepted_user_id = %s WHERE id = %s", (nuevo["id"], inv["id"]))
    if inv["role"] == "family":
        cur.execute("""INSERT INTO camera_shares (camera_id, user_id, granted_by)
                       SELECT c.id, %s, i.sponsor_id FROM invitations i JOIN cameras c ON c.id = ANY(i.camera_ids)
                       WHERE i.id = %s AND c.user_id = i.sponsor_id AND c.is_active
                       ON CONFLICT DO NOTHING""", (nuevo["id"], inv["id"]))
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip, meta)
                   VALUES ('user', %s, %s, %s, 'invite_accepted', 'user', %s, %s, %s::jsonb)""",
                (nuevo["id"], nuevo["community_id"], nuevo["id"], nuevo["id"], auth._clean_ip(ip),
                 json.dumps({"rol": inv["role"], "acepta_privacidad": True})))
    return nuevo


# ------------------------------------------------------------------ lo que hace el administrador

def listar(cur, admin) -> list[dict]:
    from backend import planes
    cur.execute(f"""SELECT u.id::text AS id, u.full_name, u.role, u.status, u.email, u.phone, u.last_login_at, u.created_at,
                           s.full_name AS vecino,
                           (SELECT count(*) FROM cameras c WHERE c.user_id = u.id AND c.is_active) AS camaras,
                           (SELECT count(*) FROM users f WHERE f.sponsor_id = u.id AND f.role = 'family' AND f.status = 'active') AS familiares,
                           EXISTS (SELECT 1 FROM notification_channels ch WHERE ch.user_id = u.id AND ch.is_active AND ch.chat_id IS NOT NULL) AS telegram,
                           {planes.sql_usuario_premium('u')} AS premium
                    FROM users u LEFT JOIN users s ON s.id = u.sponsor_id
                    WHERE u.community_id = %s AND u.status <> 'canceled'
                    ORDER BY CASE u.role WHEN 'community_admin' THEN 0 WHEN 'resident' THEN 1 WHEN 'guard' THEN 2 ELSE 3 END,
                             lower(u.full_name)""", (admin["community_id"],))
    return [dict(r) for r in cur.fetchall()]


def _objetivo(cur, admin, uid, permitir_si_mismo=False):
    if admin["role"] != "community_admin":
        raise ErrorPersonas("Solo el administrador gestiona personas.", "prohibido", 403)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uid or ""):
        raise ErrorPersonas("No existe esa persona.", "no_existe", 404)
    cur.execute("""SELECT id::text AS id, full_name, role, status, community_id::text AS community_id FROM users
                   WHERE id = %s AND community_id = %s AND status <> 'canceled' FOR UPDATE""", (uid, admin["community_id"]))
    u = cur.fetchone()
    if u is None:
        raise ErrorPersonas("No existe esa persona en tu comunidad.", "no_existe", 404)
    if not permitir_si_mismo and u["id"] == str(admin["id"]):
        raise ErrorPersonas("Eso lo cambias desde tu propio menú, no desde aquí.", "a_si_mismo", 422)
    return dict(u)


def _queda_otro_admin(cur, admin, sin):
    cur.execute("""SELECT count(*) AS n FROM users WHERE community_id = %s AND role = 'community_admin' AND status = 'active' AND id <> %s""",
                (admin["community_id"], sin))
    return cur.fetchone()["n"] > 0


def cambiar_estado(cur, admin, uid, activo: bool) -> str:
    u = _objetivo(cur, admin, uid)
    if not activo and u["role"] == "community_admin" and not _queda_otro_admin(cur, admin, u["id"]):
        raise ErrorPersonas("Debe quedar al menos un administrador activo.", "ultimo_admin", 422)
    cur.execute("UPDATE users SET status = %s WHERE id = %s", ("active" if activo else "suspended", u["id"]))
    if not activo:
        cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (u["id"],))
    _auditar(cur, admin, "user_reactivated" if activo else "user_suspended", u["id"])
    return u["full_name"]


def cambiar_rol(cur, admin, uid, rol) -> str:
    if rol not in ROLES_QUE_INVITA_EL_ADMIN:
        raise ErrorPersonas("Rol no válido.")
    u = _objetivo(cur, admin, uid)
    if u["role"] == "family":
        raise ErrorPersonas("El rol de un familiar no se cambia: depende de su vecino.", "familiar", 422)
    if u["role"] == "community_admin" and rol != "community_admin" and not _queda_otro_admin(cur, admin, u["id"]):
        raise ErrorPersonas("Debe quedar al menos un administrador activo.", "ultimo_admin", 422)
    if rol == "guard":
        cur.execute("SELECT count(*) AS n FROM cameras WHERE user_id = %s AND is_active", (u["id"],))
        if cur.fetchone()["n"]:
            raise ErrorPersonas("Esa persona tiene cámaras privadas: reasígnalas en «Equipos» antes de hacerla vigilante.", "tiene_camaras", 422)
        cur.execute("SELECT count(*) AS n FROM users WHERE sponsor_id = %s AND status = 'active'", (u["id"],))
        if cur.fetchone()["n"]:
            raise ErrorPersonas("Esa persona tiene familiares activos: quítalos antes de hacerla vigilante.", "tiene_familiares", 422)
    cur.execute("UPDATE users SET role = %s WHERE id = %s", (rol, u["id"]))
    cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (u["id"],))   # su sesión toma el rol nuevo
    _auditar(cur, admin, "user_role_changed", u["id"], {"antes": u["role"], "ahora": rol})
    return u["full_name"]


def enlace_contrasena(cur, admin, uid) -> tuple[str, str]:
    u = _objetivo(cur, admin, uid)
    if u["status"] != "active":
        raise ErrorPersonas("Reactiva la cuenta antes de generar el enlace.", "suspendida", 422)
    token = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO password_resets (user_id, token_hash, channel, expires_at)
                   VALUES (%s, %s, 'soporte', now() + make_interval(mins => %s))""", (u["id"], cuenta.huella(token), cuenta.TOKEN_MINUTOS))
    _auditar(cur, admin, "password_reset_link_created", u["id"])
    return u["full_name"], token


def cerrar_sesiones(cur, admin, uid) -> tuple[str, int]:
    u = _objetivo(cur, admin, uid)
    cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (u["id"],))
    n = cur.rowcount
    _auditar(cur, admin, "user_sessions_closed", u["id"], {"sesiones": n})
    return u["full_name"], n


def dar_de_baja(cur, admin, uid):
    from backend import suscripciones
    u = _objetivo(cur, admin, uid)
    if u["role"] == "community_admin" and not _queda_otro_admin(cur, admin, u["id"]):
        raise ErrorPersonas("Debe quedar al menos un administrador activo.", "ultimo_admin", 422)
    info = suscripciones.baja_efectiva(cur, u["id"], "por el administrador")
    _auditar(cur, admin, "user_offboarded_by_admin", u["id"])
    return u["full_name"], info


# ------------------------------------------------------------------ familiares (lo que hace el vecino)

def mis_familiares(cur, vecino) -> list[dict]:
    cur.execute("""SELECT u.id::text AS id, u.full_name, u.email, u.phone, u.last_login_at,
                          coalesce(array_agg(sh.camera_id::text) FILTER (WHERE sh.camera_id IS NOT NULL), '{}') AS camaras
                   FROM users u LEFT JOIN camera_shares sh ON sh.user_id = u.id
                   WHERE u.sponsor_id = %s AND u.role = 'family' AND u.status = 'active'
                   GROUP BY u.id ORDER BY lower(u.full_name)""", (vecino["id"],))
    return [dict(r) for r in cur.fetchall()]


def mis_camaras(cur, vecino) -> list[dict]:
    cur.execute("""SELECT id::text AS id, name FROM cameras WHERE user_id = %s AND kind = 'private' AND is_active ORDER BY name""", (vecino["id"],))
    return [dict(r) for r in cur.fetchall()]


def _familiar_de(cur, vecino, fid):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", fid or ""):
        return None
    cur.execute("""SELECT id::text AS id, full_name FROM users WHERE id = %s AND sponsor_id = %s AND role = 'family' AND status = 'active'""",
                (fid, vecino["id"]))
    return cur.fetchone()


def compartir(cur, vecino, fid, camaras) -> bool:
    f = _familiar_de(cur, vecino, fid)
    if f is None:
        return False
    ids = _camaras_propias(cur, vecino["id"], camaras)
    cur.execute("DELETE FROM camera_shares WHERE user_id = %s", (f["id"],))
    for cid in ids:
        cur.execute("INSERT INTO camera_shares (camera_id, user_id, granted_by) VALUES (%s, %s, %s)", (cid, f["id"], vecino["id"]))
    _auditar(cur, vecino, "family_cameras_changed", f["id"], {"camaras": len(ids)})
    return True


def quitar_familiar(cur, vecino, fid) -> str | None:
    f = _familiar_de(cur, vecino, fid)
    if f is None:
        return None
    cur.execute("UPDATE users SET status = 'canceled' WHERE id = %s", (f["id"],))
    cur.execute("DELETE FROM camera_shares WHERE user_id = %s", (f["id"],))
    cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (f["id"],))
    cur.execute("DELETE FROM notification_channels WHERE user_id = %s", (f["id"],))
    _auditar(cur, vecino, "family_removed", f["id"])
    return f["full_name"]
