"""
Cuenta: cambiar la contraseña estando dentro y recuperarla cuando se olvidó.

  • Cambiar: pide la contraseña actual (cada error cuenta para el bloqueo, igual que al entrar), aplica la política de contraseñas,
    cierra las demás sesiones (la actual sigue) y avisa por Telegram.
  • Recuperar: se pide con el correo o el teléfono. La respuesta es SIEMPRE la misma, exista o no la cuenta. Si existe y tiene Telegram
    vinculado, le llega un enlace de un solo uso que vale 30 minutos. Al usarlo se cierran TODAS sus sesiones.
  • Sin Telegram: el soporte genera el enlace con `manage.py enlace-recuperacion` y se lo entrega a la persona.
Del enlace solo se guarda su huella SHA-256.
"""
from __future__ import annotations

import collections
import hashlib
import json
import secrets
import threading
import time

from backend import auth

TOKEN_MINUTOS = 30
MAX_PEDIDOS_POR_HORA = 3            # por cuenta
IP_MAX_PEDIDOS = 10                 # por conexión...
IP_VENTANA_S = 900                  # ...cada 15 minutos
_por_ip: dict = {}
_candado = threading.Lock()


def huella(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ip_limitada(ip: str) -> bool:
    """Cuenta un pedido de esta conexión y dice si ya pasó el límite (para que nadie use esto para llenar de mensajes a otros)."""
    ahora = time.monotonic()
    with _candado:
        cola = _por_ip.setdefault(ip, collections.deque())
        while cola and ahora - cola[0] > IP_VENTANA_S:
            cola.popleft()
        cola.append(ahora)
        if len(_por_ip) > 10000:
            for k in [k for k, v in _por_ip.items() if not v or ahora - v[-1] > IP_VENTANA_S]:
                _por_ip.pop(k, None)
        return len(cola) > IP_MAX_PEDIDOS


def _problema_de_nueva(nueva: str, confirmacion: str, fila) -> str | None:
    if nueva != confirmacion:
        return "Las dos contraseñas nuevas no coinciden."
    return auth.password_issue(nueva, fila.get("email"), fila.get("phone"), fila.get("full_name"))


def _auditar(cur, user_id, community_id, accion, ip, meta=None):
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip, meta)
                   VALUES ('user', %s, %s, %s, %s, 'user', %s, %s, %s::jsonb)""",
                (str(user_id), community_id, user_id, accion, str(user_id), auth._clean_ip(ip), json.dumps(meta or {})))


def verificar_clave(cur, user_id, clave: str):
    """
    Confirma la contraseña actual para una acción delicada. Cada error cuenta para el bloqueo igual que al entrar. Devuelve (ok, mensaje).
    """
    cur.execute("""SELECT password_hash, (locked_until IS NOT NULL AND locked_until > now()) AS locked FROM users
                   WHERE id = %s AND status = 'active' FOR UPDATE""", (user_id,))
    fila = cur.fetchone()
    if fila is None:
        return False, "No se pudo confirmar tu contraseña."
    if fila["locked"]:
        return False, "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
    if not clave or len(clave) > auth.MAX_PASSWORD_CHARS or not auth.verify_password(clave, fila["password_hash"]):
        cur.execute("""UPDATE users SET failed_attempts = CASE WHEN failed_attempts + 1 >= %s THEN 0 ELSE failed_attempts + 1 END,
                              locked_until = CASE WHEN failed_attempts + 1 >= %s THEN now() + make_interval(mins => %s) ELSE locked_until END
                       WHERE id = %s""", (auth.MAX_FAILED_ATTEMPTS, auth.MAX_FAILED_ATTEMPTS, auth.LOCK_MINUTES, user_id))
        return False, "La contraseña no es correcta."
    cur.execute("UPDATE users SET failed_attempts = 0 WHERE id = %s", (user_id,))
    return True, None


# ------------------------------------------------------------------ cambiar (con la sesión abierta)

def cambiar(cur, user, actual: str, nueva: str, confirmacion: str, token_sesion: str | None, ip: str | None):
    """Devuelve (ok, mensaje, sesiones_cerradas). Un error de «contraseña actual» queda contado aunque se devuelva un mensaje."""
    cur.execute("""SELECT id, community_id, full_name, email, phone, password_hash,
                          (locked_until IS NOT NULL AND locked_until > now()) AS locked
                   FROM users WHERE id = %s AND status = 'active' FOR UPDATE""", (user["id"],))
    fila = cur.fetchone()
    if fila is None:
        return False, "No se pudo cambiar la contraseña.", 0
    if fila["locked"]:
        return False, "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.", 0
    if not actual or len(actual) > auth.MAX_PASSWORD_CHARS or not auth.verify_password(actual, fila["password_hash"]):
        cur.execute("""UPDATE users SET failed_attempts = CASE WHEN failed_attempts + 1 >= %s THEN 0 ELSE failed_attempts + 1 END,
                              locked_until = CASE WHEN failed_attempts + 1 >= %s THEN now() + make_interval(mins => %s) ELSE locked_until END
                       WHERE id = %s""", (auth.MAX_FAILED_ATTEMPTS, auth.MAX_FAILED_ATTEMPTS, auth.LOCK_MINUTES, user["id"]))
        return False, "La contraseña actual no es correcta.", 0
    problema = _problema_de_nueva(nueva, confirmacion, fila)
    if problema is None and nueva == actual:
        problema = "La contraseña nueva debe ser distinta de la actual."
    if problema:
        return False, problema, 0
    cur.execute("UPDATE users SET password_hash = %s, failed_attempts = 0, locked_until = NULL WHERE id = %s", (auth.hash_password(nueva), user["id"]))
    if token_sesion:
        cur.execute("""UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL AND token_hash <> %s""",
                    (user["id"], auth._token_hash(token_sesion)))
    else:
        cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user["id"],))
    cerradas = cur.rowcount
    _auditar(cur, user["id"], fila["community_id"], "password_change", ip, {"sesiones_cerradas": cerradas})
    return True, None, cerradas


# ------------------------------------------------------------------ recuperar (sin sesión)

def _buscar_por_identificador(cur, identificador: str):
    identificador = (identificador or "").strip()
    if not identificador or len(identificador) > auth.MAX_IDENTIFIER_CHARS:
        return None
    cur.execute("""SELECT id, community_id, full_name FROM users
                   WHERE (lower(email) = lower(%s) OR phone = %s) AND status = 'active' LIMIT 1""", (identificador, identificador))
    return cur.fetchone()


def _crear_enlace(cur, user_id, canal: str, ip: str | None) -> str:
    token = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO password_resets (user_id, token_hash, channel, requested_ip, expires_at)
                   VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))""",
                (user_id, huella(token), canal, auth._clean_ip(ip), TOKEN_MINUTOS))
    return token


def solicitar(cur, identificador: str, ip: str | None):
    """
    Crea el enlace si la cuenta existe y no pidió demasiados. Devuelve (user_id, token) para mandarlo por Telegram, o None.
    Quien llama responde igual en todos los casos.
    """
    fila = _buscar_por_identificador(cur, identificador)
    if fila is None:
        return None
    cur.execute("""SELECT count(*) AS n FROM password_resets WHERE user_id = %s AND created_at > now() - interval '1 hour'""", (fila["id"],))
    if cur.fetchone()["n"] >= MAX_PEDIDOS_POR_HORA:
        return None
    token = _crear_enlace(cur, fila["id"], "telegram", ip)
    _auditar(cur, fila["id"], fila["community_id"], "password_reset_requested", ip)
    return str(fila["id"]), token


def enlace_de_soporte(cur, user_id) -> str:
    """Para quien no tiene Telegram vinculado: el soporte genera el enlace y se lo entrega a la persona."""
    cur.execute("SELECT community_id FROM users WHERE id = %s", (user_id,))
    c = cur.fetchone()
    token = _crear_enlace(cur, user_id, "soporte", None)
    cur.execute("""INSERT INTO audit_log (actor_type, community_id, subject_user_id, action, entity, entity_id)
                   VALUES ('staff', %s, %s, 'password_reset_link_created', 'user', %s)""", (c["community_id"] if c else None, user_id, str(user_id)))
    return token


def valido(cur, token: str):
    """La fila del enlace si sirve (no usado, no vencido, cuenta activa); si no, None."""
    cur.execute("""SELECT pr.id, pr.user_id, u.community_id, u.full_name, u.email, u.phone
                   FROM password_resets pr JOIN users u ON u.id = pr.user_id
                   WHERE pr.token_hash = %s AND pr.used_at IS NULL AND pr.expires_at > now() AND u.status = 'active'""", (huella(token),))
    return cur.fetchone()


def restablecer(cur, token: str, nueva: str, confirmacion: str, ip: str | None):
    """Devuelve (ok, mensaje, user_id). Una contraseña floja NO gasta el enlace; una buena lo gasta y cierra todas las sesiones."""
    fila = valido(cur, token)
    if fila is None:
        return False, "Este enlace ya no sirve. Pide uno nuevo.", None
    problema = _problema_de_nueva(nueva, confirmacion, fila)
    if problema:
        return False, problema, None
    cur.execute("UPDATE password_resets SET used_at = now() WHERE id = %s AND used_at IS NULL RETURNING id", (fila["id"],))
    if cur.fetchone() is None:                                          # otro intento lo usó justo antes
        return False, "Este enlace ya no sirve. Pide uno nuevo.", None
    cur.execute("UPDATE users SET password_hash = %s, failed_attempts = 0, locked_until = NULL WHERE id = %s", (auth.hash_password(nueva), fila["user_id"]))
    cur.execute("UPDATE password_resets SET used_at = coalesce(used_at, now()) WHERE user_id = %s AND used_at IS NULL", (fila["user_id"],))
    cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (fila["user_id"],))
    cerradas = cur.rowcount
    _auditar(cur, fila["user_id"], fila["community_id"], "password_reset", ip, {"sesiones_cerradas": cerradas})
    return True, None, str(fila["user_id"])
