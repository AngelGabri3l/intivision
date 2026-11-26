"""
auth.py — Inicio de sesión, sesiones por cookie y roles (VIGIA-23).

- Contraseñas con bcrypt (nunca en texto plano).
- Sesión: cookie HttpOnly con un token aleatorio; en la base solo se guarda su hash (SHA-256).
- Bloqueo temporal tras varios intentos fallidos.
- Roles: 'resident' (ve lo suyo), 'community_admin' (ve lo de su comunidad), 'guard' (zonas comunes) y 'family' (lo compartido).
- Una sola función, current_user, devuelve al usuario de la sesión: todas las rutas la reutilizan.
"""
import collections
import hashlib
import ipaddress
import logging
import math
import os
import re
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import bcrypt
from fastapi import Depends, Request, WebSocket

from backend import db

log = logging.getLogger("backend")
SESSION_COOKIE = "vigia_session"


def _entorno_positivo(nombre, por_defecto):
    """Un número mayor que 0 del entorno. Si no lo es, el servidor no arranca: una política de sesión mal escrita no debe pasar en silencio."""
    crudo = os.environ.get(nombre, "").strip()
    if not crudo:
        return float(por_defecto)
    try:
        valor = float(crudo)
    except ValueError:
        valor = float("nan")
    if not (math.isfinite(valor) and valor > 0):
        raise ValueError(f"{nombre} debe ser un número mayor que 0 (recibió «{crudo}»).")
    return valor


# Cuánto dura una sesión según el rol: se cierra sola tras un rato sin actividad y, pase lo que pase, al llegar a la duración máxima.
SESSION_IDLE_MIN_ADMIN = _entorno_positivo("SESSION_IDLE_MIN_ADMIN", 30)         # administrador: minutos sin actividad
SESSION_MAX_H_ADMIN = _entorno_positivo("SESSION_MAX_H_ADMIN", 8)                # administrador: horas como máximo
SESSION_IDLE_H_RESIDENT = _entorno_positivo("SESSION_IDLE_H_RESIDENT", 12)       # vecino: horas sin actividad
SESSION_MAX_DAYS_RESIDENT = _entorno_positivo("SESSION_MAX_DAYS_RESIDENT", 7)    # vecino: días como máximo


def session_policy(role):
    """(inactividad, duración máxima) en segundos para ese rol. La inactividad nunca pasa de la duración máxima."""
    if role in ("community_admin", "guard"):            # el vigilante suele usar un equipo compartido: sesión corta
        idle, maximo = SESSION_IDLE_MIN_ADMIN * 60, SESSION_MAX_H_ADMIN * 3600
    else:
        idle, maximo = SESSION_IDLE_H_RESIDENT * 3600, SESSION_MAX_DAYS_RESIDENT * 86400
    return min(idle, maximo), maximo


MAX_FAILED_ATTEMPTS = int(os.environ.get("MAX_FAILED_ATTEMPTS", "5"))
LOCK_MINUTES = int(os.environ.get("LOCK_MINUTES", "15"))
MAX_PASSWORD_BYTES = 72          # límite de bcrypt
MIN_PASSWORD_CHARS = 10
MAX_IDENTIFIER_CHARS = 254           # largo máximo de un correo
MAX_PASSWORD_CHARS = 1024            # nada razonable pasa de esto; evita enviar textos gigantes
LOGIN_IP_MAX_FAILURES = int(os.environ.get("LOGIN_IP_MAX_FAILURES", "30"))     # intentos fallidos por IP...
LOGIN_IP_WINDOW_SECONDS = int(os.environ.get("LOGIN_IP_WINDOW_SECONDS", "900"))  # ...en esta ventana (15 min)
ROLES = ("resident", "community_admin", "guard", "family")


class NotAuthenticated(Exception):
    """No hay sesión válida. `motivo` dice por qué, si había cookie: 'expirada' (venció o pasó su máximo) o 'cerrada' (se revocó)."""

    def __init__(self, motivo=None):
        super().__init__(motivo)
        self.motivo = motivo


class Forbidden(Exception):
    """Hay sesión, pero el rol no alcanza."""


# ---------------------------------------------------------------- contraseñas

def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"La contraseña no puede pasar de {MAX_PASSWORD_BYTES} bytes.")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES],
                              password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# Hash de relleno: así tarda lo mismo comprobar un usuario que no existe y no se puede
# deducir qué cuentas existen midiendo el tiempo de respuesta.
_DUMMY_HASH = bcrypt.hashpw(b"vigia-relleno", bcrypt.gensalt()).decode("ascii")


# ------------------------------------------------------------------ inicio de sesión

def valid_login_input(identifier, password) -> bool:
    """
    Descarta, sin tocar la base, lo que no puede ser un acceso real: vacío, de otro tipo, gigante o con
    el carácter nulo (PostgreSQL no lo acepta en un texto y provocaría un error 500).
    """
    if not isinstance(identifier, str) or not isinstance(password, str):
        return False
    if not identifier.strip() or not password:
        return False
    if len(identifier) > MAX_IDENTIFIER_CHARS or len(password) > MAX_PASSWORD_CHARS:
        return False
    return "\x00" not in identifier and "\x00" not in password


def authenticate(identifier: str, password: str):
    """
    Devuelve (usuario, None) si las credenciales son correctas, o (None, motivo):
      'invalid'  -> usuario o contraseña incorrectos (mensaje genérico)
      'locked'   -> cuenta bloqueada temporalmente por demasiados intentos
    """
    if not valid_login_input(identifier, password):
        verify_password("x", _DUMMY_HASH)          # tarda lo mismo que un intento normal
        return None, "invalid"
    identifier = identifier.strip()
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, community_id, role, full_name, password_hash, status,
                      (locked_until IS NOT NULL AND locked_until > now()) AS locked
               FROM users
               WHERE lower(email) = lower(%s) OR phone = %s
               LIMIT 1""", (identifier, identifier))
        user = cur.fetchone()
        if user is None or user["status"] != "active":
            verify_password(password, _DUMMY_HASH)
            return None, "invalid"
        if user["locked"]:
            verify_password(password, _DUMMY_HASH)     # el mismo trabajo que una contraseña normal: el tiempo no delata el bloqueo
            return None, "locked"
        if verify_password(password, user["password_hash"]):
            cur.execute("""UPDATE users SET failed_attempts = 0, locked_until = NULL,
                                  last_login_at = now() WHERE id = %s""", (user["id"],))
            return user, None
        cur.execute(
            """UPDATE users
               SET failed_attempts = CASE WHEN failed_attempts + 1 >= %s THEN 0
                                          ELSE failed_attempts + 1 END,
                   locked_until    = CASE WHEN failed_attempts + 1 >= %s
                                          THEN now() + make_interval(mins => %s)
                                          ELSE locked_until END
               WHERE id = %s
               RETURNING (locked_until IS NOT NULL AND locked_until > now()) AS locked""",
            (MAX_FAILED_ATTEMPTS, MAX_FAILED_ATTEMPTS, LOCK_MINUTES, user["id"]))
        row = cur.fetchone()
        return None, ("locked" if row and row["locked"] else "invalid")


# ------------------------------------------------------------------ sesiones

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_ip(ip: Optional[str]) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        return None


def create_session(user_id, ip: Optional[str] = None, user_agent: Optional[str] = None, role: Optional[str] = None) -> str:
    """Abre una sesión. Vale lo que dice la política de su rol; cada actividad de la persona la renueva (ver renew_session)."""
    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        if role is None:
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            role = cur.fetchone()["role"]
        idle, _ = session_policy(role)
        cur.execute("DELETE FROM sessions WHERE expires_at < now() - interval '7 days'")
        cur.execute(
            """INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent)
               VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s)""",
            (user_id, _token_hash(token), idle, _clean_ip(ip),
             (user_agent or "")[:300] or None))
    return token


def user_from_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with db.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.community_id, u.role, u.full_name, s.created_at, now() AS ahora
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.revoked_at IS NULL
                 AND s.expires_at > now() AND u.status = 'active'""",
            (_token_hash(token),))
        row = cur.fetchone()
    if not row:
        return None
    _, maximo = session_policy(row["role"])
    if (row["ahora"] - row["created_at"]).total_seconds() >= maximo:
        return None                      # pasó la duración máxima de su rol, aunque siga activa
    return {k: row[k] for k in ("id", "community_id", "role", "full_name")}


def renew_session(token: Optional[str], role: str):
    """
    Renueva la inactividad de una sesión: vale otra vez `inactividad` desde ahora, sin pasar nunca de su duración máxima. Escribe solo si
    el vencimiento cambia más que un umbral (como mucho una vez por minuto por sesión); así también acota, en su siguiente pedido, las
    sesiones abiertas con una política anterior.
    """
    if not token:
        return
    idle, maximo = session_policy(role)
    umbral = min(60.0, idle / 4)
    with db.cursor() as cur:
        cur.execute(
            """UPDATE sessions SET expires_at = LEAST(created_at + make_interval(secs => %(maximo)s), now() + make_interval(secs => %(idle)s))
               WHERE token_hash = %(h)s AND revoked_at IS NULL AND expires_at > now()
                 AND abs(extract(epoch FROM (expires_at - LEAST(created_at + make_interval(secs => %(maximo)s),
                                                                now() + make_interval(secs => %(idle)s))))) > %(umbral)s""",
            {"maximo": maximo, "idle": idle, "h": _token_hash(token), "umbral": umbral})


def session_end_reason(token: Optional[str]) -> Optional[str]:
    """Por qué ya no sirve la sesión de esta cookie: 'cerrada' (se revocó) o 'expirada' (venció, pasó su máximo o ya no existe)."""
    if not token:
        return None
    with db.cursor() as cur:
        cur.execute("SELECT revoked_at IS NOT NULL AS cerrada FROM sessions WHERE token_hash = %s", (_token_hash(token),))
        row = cur.fetchone()
    return "cerrada" if row and row["cerrada"] else "expirada"


def revoke_all_sessions(user_id) -> int:
    """Cierra todas las sesiones abiertas de una persona. Devuelve cuántas eran."""
    with db.cursor() as cur:
        cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()", (user_id,))
        return cur.rowcount


def revoke_session(token: Optional[str]):
    if token:
        with db.cursor() as cur:
            cur.execute("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
                        (_token_hash(token),))


# ------------------------------------------------------------------ dependencias de FastAPI

def cuenta_como_actividad(request: Request) -> bool:
    """
    Qué pedidos renuevan la inactividad: cargar una página y cualquier acción de la persona (POST, PUT, PATCH, DELETE).
    NO la renuevan las consultas de datos que una pantalla hace sola cada pocos segundos, ni las imágenes, ni el WebSocket:
    si lo hicieran, una pestaña olvidada abierta mantendría la sesión viva para siempre.
    """
    if request.method == "GET":
        return "text/html" in request.headers.get("accept", "")
    return request.method in ("POST", "PUT", "PATCH", "DELETE")


def current_user(request: Request) -> dict:
    """El usuario de la sesión. Si no hay sesión válida, lanza NotAuthenticated (con el motivo, si la cookie existía)."""
    token = request.cookies.get(SESSION_COOKIE)
    user = user_from_token(token)
    if user is None:
        raise NotAuthenticated(session_end_reason(token))
    if cuenta_como_actividad(request):
        try:
            renew_session(token, user["role"])
        except Exception:
            log.warning("No se pudo renovar la sesión (la petición sigue).", exc_info=True)
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "community_admin":
        raise Forbidden()
    return user


def user_from_websocket(ws: WebSocket) -> Optional[dict]:
    return user_from_token(ws.cookies.get(SESSION_COOKIE))


def origin_allowed(ws: WebSocket) -> bool:
    """Defensa contra WebSockets abiertos desde otras páginas: el origen debe ser este mismo sitio."""
    origin = ws.headers.get("origin")
    if not origin:
        return True          # clientes que no son navegador (no llevan la cookie de otro sitio)
    extra = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return urlparse(origin).netloc == ws.headers.get("host") or origin in extra


def cookie_is_secure(request: Request) -> bool:
    """Tras Caddy la conexión llega por http interno; el esquema real viene en X-Forwarded-Proto."""
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https")


def set_session_cookie(response, request: Request, token: str, role: str):
    """La cookie dura lo máximo que puede durar la sesión de ese rol; la inactividad la controla el servidor."""
    response.set_cookie(SESSION_COOKIE, token, max_age=int(session_policy(role)[1]), httponly=True,
                        secure=cookie_is_secure(request), samesite="lax", path="/")


# ------------------------------------------------------------------ mini PC (edge)

def edge_from_token(token: Optional[str]) -> Optional[dict]:
    """Identifica a una mini PC por su token (se guarda solo el hash). Actualiza last_seen_at."""
    if not token:
        return None
    with db.cursor() as cur:
        cur.execute("""UPDATE edge_devices SET last_seen_at = now()
                       WHERE token_hash = %s AND status = 'active'
                       RETURNING id, community_id""", (_token_hash(token),))
        row = cur.fetchone()
    return dict(row) if row else None


def hash_token(token: str) -> str:
    return _token_hash(token)


# ------------------------------------------------------------------ límite de intentos por IP

_fallos_por_ip = {}                  # ip -> instantes de los intentos fallidos recientes
_candado = threading.Lock()


# Redes de las que sí nos fiamos al leer X-Forwarded-For: la propia máquina y las redes internas
# (la de Docker, donde está Caddy). Una lista explícita: no se usa is_private porque Python también
# cuenta como "privados" algunos rangos de documentación que NO son internos.
_REDES_DE_CONFIANZA = [ipaddress.ip_network(n) for n in
                       ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def _es_red_privada(host) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in red for red in _REDES_DE_CONFIANZA)


def client_ip(request: Request) -> str:
    """
    La IP real de quien pide. Detrás de Caddy, la conexión llega desde la red interna de Docker y la IP
    original viene en X-Forwarded-For: solo se confía en esa cabecera si quien conecta es una red privada
    (un cliente de Internet que la escriba a mano no puede aparentar otra IP).
    """
    peer = request.client.host if request.client else ""
    if _es_red_privada(peer):
        reenviada = request.headers.get("x-forwarded-for", "")
        if reenviada:
            candidata = reenviada.split(",")[-1].strip()      # Caddy agrega la IP real al final
            try:
                return str(ipaddress.ip_address(candidata))
            except ValueError:
                pass
    return peer or "desconocida"


def throttled(ip: str) -> bool:
    """¿Esta IP ya acumuló demasiados accesos fallidos en la ventana de tiempo?"""
    ahora = time.monotonic()
    with _candado:
        fallos = _fallos_por_ip.get(ip)
        if not fallos:
            return False
        while fallos and ahora - fallos[0] > LOGIN_IP_WINDOW_SECONDS:
            fallos.popleft()
        if not fallos:
            _fallos_por_ip.pop(ip, None)
            return False
        return len(fallos) >= LOGIN_IP_MAX_FAILURES


def register_failure(ip: str):
    ahora = time.monotonic()
    with _candado:
        _fallos_por_ip.setdefault(ip, collections.deque()).append(ahora)
        if len(_fallos_por_ip) > 10000:                                  # la memoria no crece sin límite
            for clave in [k for k, v in _fallos_por_ip.items() if not v or ahora - v[-1] > LOGIN_IP_WINDOW_SECONDS]:
                _fallos_por_ip.pop(clave, None)


# ------------------------------------------------------------------ política de contraseñas

COMMON_PASSWORDS = {
    "1234567890", "0123456789", "0987654321", "1234567891", "123456789a", "12345678910",
    "qwertyuiop", "abcdefghij", "asdfghjkl1", "password12", "password123", "contrasena1",
    "contrasena12", "contrasena123", "vigia12345", "vigia123456", "guardian123", "guardian1234",
    "iloveyou12", "welcome123", "admin12345", "administrador",
}


def password_issue(password: str, *contexto) -> Optional[str]:
    """Devuelve por qué una contraseña NO sirve, o None si está bien. `contexto`: correo, teléfono, nombre."""
    if len(password) < MIN_PASSWORD_CHARS:
        return f"La contraseña debe tener al menos {MIN_PASSWORD_CHARS} caracteres."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"La contraseña no puede pasar de {MAX_PASSWORD_BYTES} bytes."
    if "\x00" in password:
        return "La contraseña no puede contener caracteres nulos."
    if len(set(password)) < 5:
        return "La contraseña es demasiado repetitiva."
    bajo = password.lower()
    if bajo in COMMON_PASSWORDS or bajo.rstrip("0123456789!") in COMMON_PASSWORDS:
        return "Esa contraseña es de las más comunes: elige otra."
    palabras = set()
    for dato in contexto:
        if not dato:
            continue
        dato = str(dato).lower()
        palabras.add(dato)
        palabras.update(t for t in re.split(r"[^0-9a-zñáéíóúü]+", dato.split("@")[0]) if len(t) >= 4)
    if any(len(w) >= 4 and w in bajo for w in palabras):
        return "La contraseña no debe contener tu nombre, tu correo ni tu teléfono."
    return None
