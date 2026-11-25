import json
import os
import re
import sys
import asyncio
import threading
import unicodedata
import logging
import datetime
import html
import secrets
from typing import Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.pool
from fastapi import FastAPI, Request, WebSocket, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response, StreamingResponse

# Permite importar los módulos del proyecto (engine, etc.) desde la carpeta padre.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db, auth, cameras, pages, alerts, sources, ingest_media, live, pwa  # noqa: E402
from backend import hitl, dashboard, mapa, planes, evidencia_premium, suscripciones, mercadopago, cuenta, acceso, personas, descripciones  # noqa: E402
from backend import asistente, comunicados  # noqa: E402
from backend.pages import fill  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "backend", "data")
EVIDENCE_DIR = os.path.join(BASE, "events")  # compartido con el detector
live.configure(EVIDENCE_DIR)
CONFIG_PATH = os.path.join(BASE, "config.yaml")
TEMPLATES_DIR = os.path.join(BASE, "backend", "templates")

os.makedirs(EVIDENCE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backend")

# /docs, /redoc y /openapi.json mostrarían todo el mapa de la API a cualquiera: apagados salvo ENABLE_DOCS=1.
_docs = os.environ.get("ENABLE_DOCS") == "1"
app = FastAPI(title="GUARDIAN Backend", docs_url="/docs" if _docs else None,
              redoc_url="/redoc" if _docs else None, openapi_url="/openapi.json" if _docs else None)
_dispatcher = None   # actuador (sirena por MQTT), creado una vez


def load_cfg():
    """config.yaml + credenciales del entorno / .env (ver settings.py)."""
    from settings import load_config
    return load_config(CONFIG_PATH)


# ---------------------- seguridad: utilidades ----------------------

MENSAJE_ACCESO = "Usuario o contraseña incorrectos."      # el MISMO texto para todo fallo: no revela qué cuentas existen
NIVELES = ("BAJO", "MEDIO", "ALTO")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


EVENT_MAX_AGE = datetime.timedelta(hours=48)       # un evento más viejo que esto se guarda con la hora del servidor
EVENT_MAX_AHEAD = datetime.timedelta(minutes=5)    # y uno «del futuro» también (el reloj de la mini PC está mal)


def parse_event_time(valor):
    """
    La hora del evento según la mini PC (ISO 8601). Devuelve (fecha_y_hora_UTC | None, motivo | None).
    Si no viene, o viene fuera de [ahora - 48 h, ahora + 5 min], devuelve None: se usará la hora del servidor.
    """
    if valor is None:
        return None, None
    if not isinstance(valor, str) or len(valor) > 40:
        return None, "occurred_at debe ser una fecha y hora ISO 8601."
    try:
        momento = datetime.datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "occurred_at debe ser una fecha y hora ISO 8601."
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=datetime.timezone.utc)
    ahora = datetime.datetime.now(datetime.timezone.utc)
    if momento > ahora + EVENT_MAX_AHEAD or momento < ahora - EVENT_MAX_AGE:
        return None, None
    return momento.astimezone(datetime.timezone.utc), None


def validate_event(e):
    """Revisa un evento recibido de otro equipo. Devuelve (evento_limpio, None) o (None, motivo)."""
    if not isinstance(e, dict):
        return None, "El cuerpo debe ser un objeto JSON."
    if e.get("level") not in NIVELES:
        return None, "level debe ser BAJO, MEDIO o ALTO."
    out = {"level": e["level"]}
    for campo, tope in (("score", 100), ("people", 1000)):
        valor = e.get(campo, 0)
        if isinstance(valor, bool) or not isinstance(valor, int) or not 0 <= valor <= tope:
            return None, f"{campo} debe ser un entero entre 0 y {tope}."
        out[campo] = valor
    motivos = e.get("reasons")
    if isinstance(motivos, list):
        if not all(isinstance(x, str) for x in motivos):
            return None, "reasons debe ser texto o una lista de textos."
        motivos = ", ".join(motivos)
    if motivos is not None and (not isinstance(motivos, str) or len(motivos) > 500):
        return None, "reasons debe ser texto de hasta 500 caracteres."
    out["reasons"] = motivos
    imagen = e.get("image")
    if imagen is not None and (not isinstance(imagen, str) or not IMAGE_NAME_RE.match(imagen)):
        return None, "image debe ser un nombre de archivo simple (letras, números, punto, guion)."
    out["image"] = imagen
    camara = e.get("camera_id")
    if camara is not None and not cameras.is_uuid(camara):
        return None, "camera_id no es un identificador válido."
    out["camera_id"] = camara
    edge_id = e.get("edge_event_id")
    if edge_id is not None and not cameras.is_uuid(edge_id):
        return None, "edge_event_id no es un identificador válido."
    out["edge_event_id"] = edge_id.lower() if edge_id else None
    momento, motivo = parse_event_time(e.get("occurred_at"))
    if motivo:
        return None, motivo
    out["occurred_at"] = momento
    return out, None


def content_security_policy(nonce, host=None, secure=True):
    """Política del navegador: solo el script de la página (con su clave de un solo uso), nada de terceros.
    El WebSocket de la imagen en vivo es del mismo sitio; se nombra su dirección además de 'self' porque algunos Safari
    antiguos no consideran que 'self' cubra a ws:// y wss://."""
    conexion = "connect-src 'self'"
    if host and re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?", host):
        conexion += f" {'wss' if secure else 'ws'}://{host}"
    return "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "manifest-src 'self'",
        "worker-src 'self'",
        conexion,
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ])


def cross_site_request(request: Request) -> bool:
    """
    ¿Es una petición que CAMBIA algo y viene de otra página? Los navegadores mandan Origin en los envíos de
    formularios; si no coincide con este sitio, se bloquea (CSRF). Las mini PC no son navegadores: no mandan
    Origin y se identifican con su token, así que no les afecta.
    """
    if request.method not in UNSAFE_METHODS:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        permitidos = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
        return not (urlparse(origin).netloc == request.headers.get("host") or origin in permitidos)
    sitio = request.headers.get("sec-fetch-site")
    return sitio is not None and sitio not in ("same-origin", "none")


def apply_security_headers(request: Request, response):
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=(self), payment=(), usb=()"      # geolocation: solo nuestras páginas (ubicar una cámara con el GPS)
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    # Las fotos pueden guardarse un rato en el navegador de su dueño y los íconos de la PWA en el de cualquiera; todo lo demás, nunca.
    ruta = request.url.path
    h["Cache-Control"] = ("private, max-age=600" if ruta.startswith("/evidence/")
                          else "private, max-age=86400" if ruta.startswith("/mapa/teselas/")
                          else "public, max-age=86400" if ruta.startswith(("/icons/", "/static/leaflet/")) else "no-store")
    if auth.cookie_is_secure(request):
        h["Strict-Transport-Security"] = "max-age=31536000"
    if h.get("content-type", "").startswith("text/html"):
        h["Content-Security-Policy"] = content_security_policy(request.state.csp_nonce, request.headers.get("host"), auth.cookie_is_secure(request))
    elif ruta == "/sw.js":
        # el service worker hereda esta política: necesita pedir cosas al mismo sitio (para mostrar «sin conexión»)
        h["Content-Security-Policy"] = "default-src 'none'; connect-src 'self'; frame-ancestors 'none'"
    else:
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"


# ---------------------- eventos en PostgreSQL ----------------------

# Texto del motor de amenaza -> código del catálogo `signals` de la base.
REASON_CODES = [
    ("persona detectada", "person_present"),
    ("en zona restringida", "in_restricted_zone"),
    ("horario sensible", "sensitive_hour"),
    ("merodeo", "loitering"),
    ("grupo de", "group"),
    ("cercanía entre personas", "proximity"),
    ("trepando", "climbing"),
    ("persona en el suelo", "fallen"),
    ("posible asalto", "hands_up"),
    ("posible arma", "weapon"),
]
DETAIL_CODES = {"loitering", "group", "weapon"}          # estos llevan detalle ("merodeo 18s", "grupo de 3")


def _sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


_PREFIJOS = [(_sin_tildes(prefijo), codigo) for prefijo, codigo in REASON_CODES]      # se compara sin tildes: «cercanía» o «cercania» dan lo mismo


def parse_reasons(reasons):
    """'persona detectada, merodeo 18s' -> [('person_present', None), ('loitering', 'merodeo 18s')]"""
    if isinstance(reasons, str):
        items = [r.strip() for r in reasons.split(", ")]
    else:
        items = [str(r).strip() for r in (reasons or [])]
    out = []
    for item in items:
        low = _sin_tildes(item).lower()
        for prefix, code in _PREFIJOS:
            if low.startswith(prefix):
                out.append((code, item if code in DETAIL_CODES else None))
                break
    return out


def insert_event(e, user=None, community_id=None, source_kind="browser", camera_id=None, notify=True,
                 occurred_at=None, edge_event_id=None):
    """
    Guarda un evento con sus motivos (event_signals) y su imagen (media_files). Si viene `user`,
    el evento es de ese usuario y de su comunidad; si no (mini PC), solo de la comunidad.
    Si es MEDIO o ALTO y `notify`, deja en la cola los avisos por Telegram de quien corresponda.

    `occurred_at`: la hora del evento (si no viene, la del servidor).
    `edge_event_id`: el identificador que le pone la mini PC. Si ya existe un evento con ese identificador en la
    comunidad, no se guarda nada (ni motivos, ni imagen, ni avisos) y devuelve None: es un reenvío.
    """
    owner = user["id"] if user else None
    community = user["community_id"] if user else community_id
    image = os.path.basename(e["image"]) if e.get("image") else None
    columnas, extra, conflicto = "", "", ""
    valores = [community, owner, camera_id, source_kind, e.get("level"), int(e.get("score") or 0),
               int(e.get("people") or 0), e.get("status", "sin verificar"), e.get("video_time_s"), occurred_at]
    if edge_event_id:
        # Solo los eventos de una mini PC usan esta columna y este índice: el resto de los flujos no dependen de ellos.
        columnas, extra = ", edge_event_id", ", %s"
        conflicto = " ON CONFLICT (community_id, edge_event_id) WHERE edge_event_id IS NOT NULL DO NOTHING"
        valores.append(edge_event_id)
    with db.cursor() as cur:
        cur.execute(
            f"""INSERT INTO events (community_id, user_id, camera_id, source, level, score,
                                    people_count, status, video_time_s, occurred_at{columnas})
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()){extra}){conflicto} RETURNING id""",
            valores)
        fila = cur.fetchone()
        if fila is None:
            return None
        event_id = fila["id"]
        for code, detail in parse_reasons(e.get("reasons")):
            # Un motivo que todavía no esté en el catálogo `signals` (por ejemplo, la migración 004 aún no se aplicó) NO debe tirar el evento
            # entero: simplemente no queda registrado como señal.
            cur.execute("""INSERT INTO event_signals (event_id, signal_code, detail)
                           SELECT %s, %s, %s WHERE EXISTS (SELECT 1 FROM signals WHERE code = %s)
                           ON CONFLICT DO NOTHING""", (event_id, code, detail, code))
        if image:
            try:
                size = os.path.getsize(os.path.join(EVIDENCE_DIR, image))
            except OSError:
                size = 0
            dias = int(planes.de_camara(cur, camera_id).get("retention_days") or 7) if camera_id else None
            cur.execute(
                """INSERT INTO media_files (community_id, user_id, camera_id, event_id, kind,
                                            storage_key, size_bytes, expires_at)
                   VALUES (%s,%s,%s,%s,'image',%s,%s, CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END)
                   ON CONFLICT (storage_key) DO NOTHING""",
                (community, owner, camera_id, event_id, image, size, dias, dias or 0))
        if camera_id:
            cur.execute("UPDATE cameras SET last_seen_at = now() WHERE id = %s", (camera_id,))
        if notify:
            alerts.enqueue(cur, event_id, community, camera_id, owner, e.get("level"))
        descripciones.marcar_si_corresponde(cur, event_id, camera_id, e.get("level"), bool(image))   # IA: solo si el plan lo incluye
    return event_id


_EVENTS_SELECT = """
    SELECT e.id,
           to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD"T"HH24:MI:SS') AS created_at,
           e.source, e.video_time_s, e.level, e.score,
           COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code)
                     FROM event_signals es JOIN signals s ON s.code = es.signal_code
                     WHERE es.event_id = e.id), '') AS reasons,
           (SELECT m.storage_key FROM media_files m
             WHERE m.event_id = e.id AND m.kind = 'image'
             ORDER BY m.created_at LIMIT 1) AS image,
           (SELECT m.storage_key FROM media_files m WHERE m.event_id = e.id AND m.kind = 'clip' LIMIT 1) AS clip,
           e.ai_description, e.ai_sees_people,
           (c.id IS NOT NULL AND """ + planes.sql_es_premium("c") + """) AS premium,
           e.people_count AS people, e.status,
           c.name AS camera_name,
           COALESCE(cu.full_name, eu.full_name) AS owner_name
    FROM events e
    LEFT JOIN cameras c ON c.id = e.camera_id
    LEFT JOIN users cu ON cu.id = c.user_id
    LEFT JOIN users eu ON eu.id = e.user_id
    WHERE """


def list_events(user, limit=100, camera_id=None, level=None, desde=None, hasta=None, texto=None):
    """
    Residente: los eventos suyos o de sus cámaras. Administrador: los de su comunidad, salvo los de cámaras
    que su dueño le oculta. Opcional: una sola cámara, un nivel o un rango de fechas (hora de Lima).
    """
    limit = max(1, min(int(limit), 500))
    alcance, args = acceso.posicional(acceso.eventos(user, "e", "c"), user)        # qué eventos ve cada rol: acceso.py
    condiciones = [alcance]
    if texto:                                     # búsqueda en las descripciones con IA (Premium), en español
        condiciones.append("to_tsvector('spanish', coalesce(e.ai_description, '')) @@ plainto_tsquery('spanish', %s)")
        args.append(texto)
    if camera_id:
        condiciones.append("e.camera_id = %s")
        args.append(camera_id)
    if level:
        condiciones.append("e.level = %s")
        args.append(level)
    if desde:
        condiciones.append("e.occurred_at >= (%s::date)::timestamp AT TIME ZONE 'America/Lima'")
        args.append(desde)
    if hasta:
        condiciones.append("e.occurred_at < ((%s::date) + 1)::timestamp AT TIME ZONE 'America/Lima'")
        args.append(hasta)
    sql = _EVENTS_SELECT + " AND ".join(condiciones) + " ORDER BY e.id DESC LIMIT %s"
    args.append(limit)
    with db.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    for r in rows:
        if r["video_time_s"] is not None:
            r["video_time_s"] = float(r["video_time_s"])
    return rows


# ---------------------- alertas (actuador + Telegram) ----------------------

def get_dispatcher():
    """Crea una sola vez el actuador (sirena por MQTT o simulada). Los avisos por Telegram van por backend/alerts.py."""
    global _dispatcher
    if _dispatcher is None:
        from actuator import build_actuator
        cfg = load_cfg()
        _dispatcher = {
            "actuator": build_actuator(cfg),
            "siren_s": cfg.get("actuator", {}).get("siren_seconds", 10),
            "enabled": cfg.get("analysis", {}).get("dispatch_alerts", True),
        }
    return _dispatcher


def trigger_siren():
    """La sirena que suena cuando un administrador confirma una alerta (H6). Usa el mismo actuador que las demás alertas."""
    d = get_dispatcher()
    if not d["enabled"]:
        return
    log.warning("SIRENA | activada porque un administrador confirmó una alerta")
    d["actuator"].trigger(d["siren_s"])


# ---------------------- errores ----------------------

@app.exception_handler(psycopg2.OperationalError)
@app.exception_handler(psycopg2.pool.PoolError)
async def _base_no_disponible(request: Request, exc):
    # La base está apagada o reiniciándose: se avisa con claridad y la app se recupera sola.
    log.error("Base de datos no disponible: %s", exc)
    return JSONResponse(status_code=503,
                        content={"error": "El servicio no está disponible. Inténtalo en unos minutos."})


@app.exception_handler(auth.NotAuthenticated)
async def _no_autenticado(request: Request, exc):
    # La página principal lleva al login; la API responde 401 (la página lo interpreta).
    pagina = request.url.path in ("/", "/admin") or request.url.path.startswith(("/cameras", "/alerts", "/admin/pendientes", "/admin/equipos", "/admin/mapa"))
    quiere_html = request.method == "GET" and pagina and "text/html" in request.headers.get("accept", "")
    motivo = exc.motivo if getattr(exc, "motivo", None) in LOGIN_NOTICES else None
    if quiere_html:
        respuesta = RedirectResponse(f"/login?m={motivo}" if motivo else "/login", status_code=303)
        if motivo:
            respuesta.delete_cookie(auth.SESSION_COOKIE, path="/")        # la cookie ya no sirve: no se queda dando vueltas
        return respuesta
    return JSONResponse(status_code=401, content={"error": "No has iniciado sesión.", **({"motivo": motivo} if motivo else {})})


@app.exception_handler(auth.Forbidden)
async def _sin_permiso(request: Request, exc):
    return JSONResponse(status_code=403, content={"error": "No tienes permiso para esto."})


# ---------------------- inicio y cierre de sesión ----------------------

def render_login(message=None, status_code=200, tipo="error", identifier=""):
    with open(os.path.join(TEMPLATES_DIR, "login.html"), encoding="utf-8") as fh:
        template = fh.read()
    bloque = ""
    if message:
        etiqueta = {"alerta": "Aviso", "ok": "Listo"}.get(tipo, "Error")
        bloque = (f'<p class="estado" data-tipo="{tipo}" role="alert"><span class="estado__tag">{etiqueta}</span>'
                  f'<span>{html.escape(message)}</span></p>')
    # Lo que escribió el usuario se devuelve escapado y sin caracteres de control.
    visible = "".join(ch for ch in (identifier or "") if ch.isprintable())[:auth.MAX_IDENTIFIER_CHARS]
    return HTMLResponse(fill(template, error=bloque, identifier=html.escape(visible, quote=True)),
                        status_code=status_code, headers={"Cache-Control": "no-store"})


# Los únicos mensajes que `/login?m=...` puede mostrar: texto fijo del servidor, nunca lo que venga en la dirección.
LOGIN_NOTICES = {
    "expirada": ("Tu sesión terminó por inactividad. Inicia sesión de nuevo.", "alerta"),
    "cerrada": ("Tu sesión se cerró desde otro dispositivo. Inicia sesión de nuevo.", "alerta"),
    "todas": ("Cerraste sesión en todos tus dispositivos.", "ok"),
    "clave_cambiada": ("Tu contraseña se cambió. Inicia sesión con la nueva.", "ok"),
    "baja": ("Te diste de baja de INTIVISION: tu cuenta se cerró y tus cámaras quedaron sin asignar. Gracias por usar el servicio.", "ok"),
}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, m: str = ""):
    try:
        ya_inicio = auth.user_from_token(request.cookies.get(auth.SESSION_COOKIE))
    except psycopg2.OperationalError:
        ya_inicio = None        # sin base no se puede comprobar; igual se muestra el formulario
    if ya_inicio:
        return RedirectResponse("/", status_code=303)
    aviso = LOGIN_NOTICES.get(m)
    return render_login(aviso[0], 200, aviso[1]) if aviso else render_login()


@app.post("/login")
def login_submit(request: Request, identifier: str = Form(""), password: str = Form("")):
    ip = auth.client_ip(request)
    if auth.throttled(ip):
        log.warning("Demasiados accesos fallidos desde %s: se le pide esperar.", ip)
        return render_login("Demasiados intentos desde esta conexión. Espera unos minutos e inténtalo de nuevo.",
                            429, "alerta", identifier)
    try:
        user, motivo = auth.authenticate(identifier, password)
    except psycopg2.OperationalError:
        return render_login("El servicio no está disponible. Inténtalo en unos minutos.", 503, "error", identifier)
    if motivo:
        auth.register_failure(ip)
        # Mismo mensaje y mismo código para "no existe", "clave mala" y "cuenta bloqueada": así nadie
        # puede averiguar qué cuentas existen probando nombres.
        log.warning("Acceso fallido desde %s (%s).", ip, "cuenta bloqueada" if motivo == "locked" else "credenciales")
        return render_login(MENSAJE_ACCESO, 401, "error", identifier)
    token = auth.create_session(user["id"], ip, request.headers.get("user-agent"), role=user["role"])
    log.info("Inicio de sesión de %s desde %s.", user["id"], ip)
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, request, token, user["role"])
    return response


@app.post("/session/ping")
def session_ping(user: dict = Depends(auth.current_user)):
    """El navegador avisa que la persona sigue ahí (clic, tecla, toque): ser un POST es lo que renueva su inactividad."""
    return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})


# ---------------------- contraseña: cambiar (con sesión) y recuperar (sin sesión) ----------------------

CUENTA_NOTICES = {"ok": ("ok", "Contraseña cambiada. Cerramos tus sesiones en los demás dispositivos.")}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,80}")
_PAGINA_PRIVADA = {"Cache-Control": "no-store"}            # la política de referencia (same-origin) la pone el filtro de seguridad de todas las páginas


def _avisar_por_telegram(user_id, texto):
    threading.Thread(target=alerts.notify_user, args=(user_id, texto), daemon=True).start()


def _cuenta_response(request, user, notice=None, status=200):
    return HTMLResponse(pwa.inject_sesion(pages.render_cuenta_contrasena(user, notice), request.state.csp_nonce), status_code=status, headers=_PAGINA_PRIVADA)


@app.get("/cuenta/contrasena", response_class=HTMLResponse)
def cuenta_contrasena_page(request: Request, m: str = "", user: dict = Depends(auth.current_user)):
    return _cuenta_response(request, user, CUENTA_NOTICES.get(m))


@app.post("/cuenta/contrasena", response_class=HTMLResponse)
def cuenta_contrasena_guardar(request: Request, actual: str = Form(""), nueva: str = Form(""), confirmacion: str = Form(""),
                              user: dict = Depends(auth.current_user)):
    """Cambia la contraseña: pide la actual (los errores cuentan para el bloqueo), cierra las demás sesiones y avisa por Telegram."""
    ip = auth.client_ip(request)
    if auth.throttled(ip):
        return _cuenta_response(request, user, ("alerta", "Demasiados intentos desde esta conexión. Espera unos minutos."), 429)
    with db.cursor() as cur:
        ok, mensaje, _cerradas = cuenta.cambiar(cur, user, actual, nueva, confirmacion, request.cookies.get(auth.SESSION_COOKIE), ip)
    if not ok:
        if "actual no es correcta" in (mensaje or ""):
            auth.register_failure(ip)
        return _cuenta_response(request, user, ("error", mensaje), 400)
    _avisar_por_telegram(user["id"], "🔒 Tu contraseña de INTIVISION se cambió. Si no fuiste tú, avisa de inmediato al administrador de tu comunidad.")
    return RedirectResponse("/cuenta/contrasena?m=ok", status_code=303)


@app.get("/recuperar", response_class=HTMLResponse)
def recuperar_page():
    return HTMLResponse(pages.render_recuperar("pedir"), headers=_PAGINA_PRIVADA)


@app.post("/recuperar", response_class=HTMLResponse)
def recuperar_pedir(request: Request, identifier: str = Form("")):
    """
    Pide el enlace de recuperación. La respuesta es la MISMA exista o no la cuenta, tenga o no Telegram: así nadie puede averiguar
    qué cuentas existen. Hay un límite por conexión y otro por cuenta.
    """
    ip = auth.client_ip(request)
    if cuenta.ip_limitada(ip):
        return HTMLResponse(pages.render_recuperar("pedir", "Demasiados pedidos desde esta conexión. Espera unos minutos.", identifier=identifier[:254]),
                            status_code=429, headers=_PAGINA_PRIVADA)
    try:
        with db.cursor() as cur:
            resultado = cuenta.solicitar(cur, identifier, ip)
    except psycopg2.OperationalError:
        return HTMLResponse(pages.render_recuperar("pedir", "El servicio no está disponible. Inténtalo en unos minutos."), status_code=503, headers=_PAGINA_PRIVADA)
    if resultado:
        user_id, token = resultado
        _avisar_por_telegram(user_id, f"🔑 Pediste cambiar tu contraseña de INTIVISION. Abre este enlace (vale {cuenta.TOKEN_MINUTOS} minutos y se usa una sola vez):\n"
                                      f"{_base_publica(request)}/recuperar/{token}\nSi no fuiste tú, ignora este mensaje.")
    return HTMLResponse(pages.render_recuperar("enviado"), headers=_PAGINA_PRIVADA)


@app.get("/recuperar/{token}", response_class=HTMLResponse)
def recuperar_con_enlace(token: str):
    if not _TOKEN_RE.fullmatch(token):
        return HTMLResponse(pages.render_recuperar("invalido"), status_code=404, headers=_PAGINA_PRIVADA)
    with db.cursor() as cur:
        fila = cuenta.valido(cur, token)
    if fila is None:
        return HTMLResponse(pages.render_recuperar("invalido"), status_code=404, headers=_PAGINA_PRIVADA)
    return HTMLResponse(pages.render_recuperar("nueva", token=token), headers=_PAGINA_PRIVADA)


@app.post("/recuperar/{token}", response_class=HTMLResponse)
def recuperar_guardar(token: str, request: Request, nueva: str = Form(""), confirmacion: str = Form("")):
    if not _TOKEN_RE.fullmatch(token):
        return HTMLResponse(pages.render_recuperar("invalido"), status_code=404, headers=_PAGINA_PRIVADA)
    with db.cursor() as cur:
        ok, mensaje, user_id = cuenta.restablecer(cur, token, nueva, confirmacion, auth.client_ip(request))
    if not ok:
        if "ya no sirve" in (mensaje or ""):
            return HTMLResponse(pages.render_recuperar("invalido"), status_code=400, headers=_PAGINA_PRIVADA)
        return HTMLResponse(pages.render_recuperar("nueva", mensaje, token=token), status_code=422, headers=_PAGINA_PRIVADA)
    _avisar_por_telegram(user_id, "🔒 Tu contraseña de INTIVISION se cambió y cerramos tus sesiones. Si no fuiste tú, avisa de inmediato al administrador de tu comunidad.")
    return RedirectResponse("/login?m=clave_cambiada", status_code=303)


# ---------------------- personas (administrador), familia (vecino) e invitaciones ----------------------

PERSONAS_NOTICES = {
    "suspendida": ("ok", "Cuenta suspendida: se cerraron sus sesiones."), "reactivada": ("ok", "Cuenta reactivada."),
    "rol": ("ok", "Rol cambiado. Sus sesiones se cerraron para que entre con el rol nuevo."), "sesiones": ("ok", "Se cerraron sus sesiones."),
    "baja": ("ok", "La persona quedó dada de baja: sus cámaras están sin asignar en «Equipos»."), "revocada": ("ok", "Invitación revocada."),
    "confirmacion": ("error", "Para dar de baja escribe exactamente BAJA."), "no_existe": ("error", "No existe esa invitación o persona."),
    "familiar_quitado": ("ok", "Familiar quitado: ya no ve tus cámaras."), "camaras": ("ok", "Cámaras compartidas actualizadas."),
}


def _enlace_invitacion(request, nombre, token, telefono):
    telefono = re.sub(r"\D", "", telefono or "") or None              # wa.me usa solo dígitos, con el código de país
    return {"titulo": f"Invitación para {nombre}", "url": f"{_base_publica(request)}/invitacion/{token}", "telefono": telefono,
            "mensaje": "Te invito a INTIVISION. Crea tu cuenta aquí:", "vigencia": f"Vale {personas.INVITACION_HORAS} horas y se usa una sola vez."}


def _personas_response(request, user, notice=None, enlace=None, status=200):
    with db.cursor() as cur:
        lista, pend = personas.listar(cur, user), personas.pendientes(cur, user)
    return HTMLResponse(pwa.inject(pages.render_personas(user, lista, pend, notice, enlace), request.state.csp_nonce),
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/admin/personas", response_class=HTMLResponse)
def personas_page(request: Request, m: str = "", user: dict = Depends(auth.require_admin)):
    return _personas_response(request, user, PERSONAS_NOTICES.get(m))


@app.post("/admin/personas/invitar", response_class=HTMLResponse)
def personas_invitar(request: Request, nombre: str = Form(""), email: str = Form(""), phone: str = Form(""), rol: str = Form("resident"),
                     user: dict = Depends(auth.require_admin)):
    if rol not in personas.ROLES_QUE_INVITA_EL_ADMIN:
        return _personas_response(request, user, ("error", "Rol no válido. Los familiares los invita cada vecino desde «Mi familia»."), status=400)
    try:
        with db.cursor() as cur:
            _, token = personas.invitar(cur, user, nombre, email, phone, rol)
    except personas.ErrorPersonas as e:
        return _personas_response(request, user, ("error", e.mensaje), status=e.status)
    return _personas_response(request, user, ("ok", "Invitación creada."), _enlace_invitacion(request, " ".join(nombre.split()), token, phone.strip() or None))


@app.post("/admin/personas/invitaciones/{inv_id}/{accion}", response_class=HTMLResponse)
def personas_invitacion(inv_id: str, accion: str, request: Request, user: dict = Depends(auth.require_admin)):
    return _invitacion_accion(request, user, inv_id, accion, "/admin/personas", _personas_response)


def _invitacion_accion(request, user, inv_id, accion, volver, responder):
    with db.cursor() as cur:
        if accion == "revocar":
            ok = personas.revocar(cur, user, inv_id)
            return RedirectResponse(f"{volver}?m={'revocada' if ok else 'no_existe'}", status_code=303)
        if accion == "reenviar":
            cur.execute("SELECT full_name, phone FROM invitations WHERE id::text = %s", (inv_id,))
            datos = cur.fetchone()
            token = personas.reenviar(cur, user, inv_id)
    if accion != "reenviar" or token is None:
        return RedirectResponse(f"{volver}?m=no_existe", status_code=303)
    return responder(request, user, ("ok", "Nuevo enlace creado; el anterior ya no sirve."), _enlace_invitacion(request, datos["full_name"], token, datos["phone"]))


@app.post("/admin/personas/{uid}/{accion}", response_class=HTMLResponse)
def personas_accion(uid: str, accion: str, request: Request, rol: str = Form(""), confirmacion: str = Form(""),
                    user: dict = Depends(auth.require_admin)):
    """Suspender, reactivar, cambiar el rol, enlace de contraseña, cerrar sesiones o dar de baja a una persona de la comunidad."""
    try:
        with db.cursor() as cur:
            if accion in ("suspender", "reactivar"):
                personas.cambiar_estado(cur, user, uid, accion == "reactivar")
                destino = "suspendida" if accion == "suspender" else "reactivada"
            elif accion == "rol":
                personas.cambiar_rol(cur, user, uid, rol)
                destino = "rol"
            elif accion == "sesiones":
                personas.cerrar_sesiones(cur, user, uid)
                destino = "sesiones"
            elif accion == "enlace":
                nombre, token = personas.enlace_contrasena(cur, user, uid)
                destino = None
            elif accion == "baja":
                if confirmacion.strip().upper() != "BAJA":
                    return RedirectResponse("/admin/personas?m=confirmacion", status_code=303)
                _, info = personas.dar_de_baja(cur, user, uid)
                destino = "baja"
            else:
                return RedirectResponse("/admin/personas?m=no_existe", status_code=303)
    except personas.ErrorPersonas as e:
        return _personas_response(request, user, ("error", e.mensaje), status=e.status)
    if accion == "baja":
        threading.Thread(target=suscripciones.enviar_avisos, args=(suscripciones.avisos_de_baja(info),), daemon=True).start()
    if destino is None:
        enlace = {"titulo": f"Enlace para que {nombre} cambie su contraseña", "url": f"{_base_publica(request)}/recuperar/{token}", "telefono": None,
                  "mensaje": "Para cambiar tu contraseña de INTIVISION abre:", "vigencia": f"Vale {cuenta.TOKEN_MINUTOS} minutos y se usa una sola vez."}
        return _personas_response(request, user, ("ok", "Enlace creado."), enlace)
    return RedirectResponse(f"/admin/personas?m={destino}", status_code=303)


def _familia_response(request, user, notice=None, enlace=None, status=200):
    with db.cursor() as cur:
        fam, cams, pend = personas.mis_familiares(cur, user), personas.mis_camaras(cur, user), personas.pendientes(cur, dict(user, role="resident"))
    return HTMLResponse(pwa.inject(pages.render_familia(user, fam, cams, pend, personas.MAX_FAMILIARES, notice, enlace), request.state.csp_nonce),
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/familia", response_class=HTMLResponse)
def familia_page(request: Request, m: str = "", user: dict = Depends(auth.current_user)):
    if not acceso.tiene_plan(user):
        return RedirectResponse("/cameras", status_code=303)
    return _familia_response(request, user, PERSONAS_NOTICES.get(m))


@app.post("/familia/invitar", response_class=HTMLResponse)
def familia_invitar(request: Request, nombre: str = Form(""), email: str = Form(""), phone: str = Form(""), camaras: list[str] = Form([]),
                    user: dict = Depends(auth.current_user)):
    if not acceso.tiene_plan(user):
        return RedirectResponse("/cameras", status_code=303)
    try:
        with db.cursor() as cur:
            _, token = personas.invitar(cur, user, nombre, email, phone, "family", camaras)
    except personas.ErrorPersonas as e:
        return _familia_response(request, user, ("error", e.mensaje), status=e.status)
    return _familia_response(request, user, ("ok", "Invitación creada."), _enlace_invitacion(request, " ".join(nombre.split()), token, phone.strip() or None))


@app.post("/familia/invitaciones/{inv_id}/{accion}", response_class=HTMLResponse)
def familia_invitacion(inv_id: str, accion: str, request: Request, user: dict = Depends(auth.current_user)):
    if not acceso.tiene_plan(user):
        return RedirectResponse("/cameras", status_code=303)
    return _invitacion_accion(request, dict(user, role="resident"), inv_id, accion, "/familia",
                              lambda req, u, notice, enlace: _familia_response(req, user, notice, enlace))


@app.post("/familia/{fid}/{accion}", response_class=HTMLResponse)
def familia_accion(fid: str, accion: str, request: Request, camaras: list[str] = Form([]), user: dict = Depends(auth.current_user)):
    if not acceso.tiene_plan(user):
        return RedirectResponse("/cameras", status_code=303)
    try:
        with db.cursor() as cur:
            if accion == "camaras":
                ok = personas.compartir(cur, user, fid, camaras)
                destino = "camaras"
            elif accion == "quitar":
                ok = personas.quitar_familiar(cur, user, fid) is not None
                destino = "familiar_quitado"
            else:
                ok, destino = False, None
    except personas.ErrorPersonas as e:
        return _familia_response(request, user, ("error", e.mensaje), status=e.status)
    return RedirectResponse(f"/familia?m={destino if ok else 'no_existe'}", status_code=303)


@app.get("/invitacion/{token}", response_class=HTMLResponse)
def invitacion_page(token: str):
    inv = None
    if _TOKEN_RE.fullmatch(token):
        with db.cursor() as cur:
            inv = personas.invitacion_valida(cur, token)
    return HTMLResponse(pages.render_invitacion(inv, token=token), status_code=200 if inv else 404, headers=_PAGINA_PRIVADA)


@app.post("/invitacion/{token}", response_class=HTMLResponse)
def invitacion_aceptar(token: str, request: Request, nueva: str = Form(""), confirmacion: str = Form(""), acepta: str = Form("")):
    """Crea la cuenta desde la invitación, abre su sesión y lo lleva a vincular Telegram."""
    if not _TOKEN_RE.fullmatch(token):
        return HTMLResponse(pages.render_invitacion(None), status_code=404, headers=_PAGINA_PRIVADA)
    ip = auth.client_ip(request)
    try:
        with db.cursor() as cur:
            nuevo = personas.aceptar(cur, token, nueva, confirmacion, acepta == "1", ip)
    except personas.ErrorPersonas as e:
        if e.codigo == "invalida":
            return HTMLResponse(pages.render_invitacion(None), status_code=404, headers=_PAGINA_PRIVADA)
        with db.cursor() as cur:
            inv = personas.invitacion_valida(cur, token)
        return HTMLResponse(pages.render_invitacion(inv, e.mensaje, token), status_code=e.status, headers=_PAGINA_PRIVADA)
    sesion = auth.create_session(nuevo["id"], ip, request.headers.get("user-agent"), role=nuevo["role"])
    response = RedirectResponse("/alerts?m=bienvenida", status_code=303)
    auth.set_session_cookie(response, request, sesion, nuevo["role"])
    return response


# ---------------------- comunicados vecinales y «Pregúntale a tus cámaras» ----------------------

COMUNICADOS_NOTICES = {"vacio": ("error", "Escribe primero la idea del comunicado.")}


def _comunicados_response(request, user, notice=None, borrador=None, status=200):
    with db.cursor() as cur:
        lista = comunicados.listar(cur, user)
        alcances = {a: comunicados.alcance(cur, user, a) for a in comunicados.AUDIENCIAS} if user["role"] == "community_admin" else None
    return HTMLResponse(pwa.inject(pages.render_comunicados(user, lista, notice, borrador, alcances, descripciones.configurada()),
                                   request.state.csp_nonce), status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/comunicados", response_class=HTMLResponse)
def comunicados_page(request: Request, m: str = "", p: str = "", t: str = "", user: dict = Depends(auth.current_user)):
    notice = COMUNICADOS_NOTICES.get(m)
    if m == "enviado" and p.isdigit() and t.isdigit():
        notice = ("ok", f"Comunicado enviado: le llega por Telegram a {int(t)} de {int(p)} personas, y todas lo ven aquí.")
    return _comunicados_response(request, user, notice)


@app.post("/comunicados/redactar", response_class=HTMLResponse)
def comunicados_redactar(request: Request, idea: str = Form(""), user: dict = Depends(auth.require_admin)):
    """La IA propone el comunicado; el administrador lo revisa y edita antes de enviarlo (nunca se envía solo)."""
    try:
        b = comunicados.redactar(idea)
    except comunicados.ErrorComunicado as e:
        return _comunicados_response(request, user, ("error", str(e)), {"idea": idea[:1500]}, status=422)
    return _comunicados_response(request, user, ("ok", "Borrador listo: revísalo y edítalo antes de enviar."),
                                 {"idea": idea[:1500], "titulo": b["titulo"], "mensaje": b["mensaje"], "ai": True})


@app.post("/comunicados/enviar", response_class=HTMLResponse)
def comunicados_enviar(request: Request, titulo: str = Form(""), mensaje: str = Form(""), audiencia: str = Form("todos"), ai: str = Form("0"),
                       user: dict = Depends(auth.require_admin)):
    try:
        with db.cursor() as cur:
            r = comunicados.publicar(cur, user, titulo, mensaje, audiencia, ai == "1")
    except comunicados.ErrorComunicado as e:
        return _comunicados_response(request, user, ("error", str(e)),
                                     {"titulo": titulo[:120], "mensaje": mensaje[:1500], "audiencia": audiencia, "ai": ai == "1"}, status=422)
    return RedirectResponse(f"/comunicados?m=enviado&p={r['personas']}&t={r['telegram']}", status_code=303)


def _asistente_response(request, user, notice=None, pregunta="", respuesta=None, status=200):
    with db.cursor() as cur:
        limite, usadas = asistente.cupo(cur, user)
        previas = asistente.recientes(cur, user)
    return HTMLResponse(pwa.inject(pages.render_asistente(user, limite, usadas, previas, notice, pregunta, respuesta), request.state.csp_nonce),
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/asistente", response_class=HTMLResponse)
def asistente_page(request: Request, user: dict = Depends(auth.current_user)):
    return _asistente_response(request, user)


@app.post("/asistente", response_class=HTMLResponse)
def asistente_preguntar(request: Request, pregunta: str = Form(""), ejemplo: str = Form(""), user: dict = Depends(auth.current_user)):
    """Responde con datos de solo lectura y los permisos de la persona (acceso.py). No cambia nada."""
    pregunta = (ejemplo or pregunta)[:500]
    with db.cursor() as cur:
        ok, texto = asistente.responder(cur, user, pregunta, "web")
    if not ok:
        return _asistente_response(request, user, ("alerta", texto), pregunta, status=200)
    return _asistente_response(request, user, None, pregunta, texto)


@app.post("/logout/todos")
def logout_all(request: Request, user: dict = Depends(auth.current_user)):
    """Cierra la sesión de la persona en todos sus dispositivos (también en este). Queda en audit_log, solo con cuántas eran."""
    cerradas = auth.revoke_all_sessions(user["id"])
    with db.cursor() as cur:
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip, meta)
                       VALUES ('user', %s, %s, %s, 'logout_all', 'user', %s, %s, %s::jsonb)""",
                    (user["id"], user["community_id"], user["id"], str(user["id"]), auth._clean_ip(auth.client_ip(request)),
                     json.dumps({"sesiones": cerradas})))
    response = RedirectResponse("/login?m=todas", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------------------- API ----------------------


@app.middleware("http")
async def seguridad(request: Request, call_next):
    request.state.csp_nonce = secrets.token_urlsafe(16)        # clave de un solo uso para el script de cada página
    if cross_site_request(request):
        log.warning("Petición %s %s bloqueada: viene de otro sitio.", request.method, request.url.path)
        response = JSONResponse(status_code=403, content={"error": "Petición bloqueada: viene de otro sitio."})
    else:
        response = await call_next(request)
    apply_security_headers(request, response)
    return response


@app.on_event("startup")
def startup():
    log.warning("Backend GUARDIAN iniciado. Preparando actuador y notificador...")
    db.dsn()   # si falta DATABASE_URL, el arranque se detiene con un mensaje claro
    try:
        db.check()
        log.warning("Conexión a PostgreSQL correcta.")
    except psycopg2.OperationalError as ex:
        # La base puede arrancar después: se reintenta en cada petición.
        log.error("Todavía no se puede usar la base de datos: %s", ex)
    get_dispatcher()
    cfg = load_cfg()
    alerts.configure(cfg.get("notifier", {}).get("telegram_token"), EVIDENCE_DIR, os.environ.get("PUBLIC_BASE_URL", ""))
    alerts.set_siren(trigger_siren)
    alerts.start(polling=os.environ.get("TELEGRAM_POLLING", "1") != "0")
    threading.Thread(target=comunicados.ciclo_forever, args=(_parar_suscripciones,), name="comunicados", daemon=True).start()
    if descripciones.configurada():
        threading.Thread(target=descripciones.ciclo_forever, args=(_parar_suscripciones, EVIDENCE_DIR), name="descripciones-ia", daemon=True).start()
    if os.environ.get("SUSCRIPCIONES_CICLO", "1") != "0":
        threading.Thread(target=suscripciones.ciclo_forever, args=(_parar_suscripciones,), name="suscripciones", daemon=True).start()


_parar_suscripciones = threading.Event()


@app.on_event("shutdown")
def shutdown():
    alerts.stop()
    _parar_suscripciones.set()


@app.get("/health")
def health():
    try:
        db.check()
        return {"status": "ok", "db": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degradado", "db": "error"})


# ---------------------- revisión del administrador (H6) ----------------------

@app.get("/admin/review/pending")
async def admin_review_pending(user: dict = Depends(auth.require_admin)):
    """Las alertas de la comunidad que esperan que un administrador las confirme o las descarte (la usa la pantalla de H9)."""
    return JSONResponse(content=await asyncio.to_thread(hitl.pending, user))


@app.post("/admin/review/{event_id}")
async def admin_review(event_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """Confirma o descarta una alerta: {"decision": "confirmar" | "descartar"}. Tiene el mismo efecto que los botones de Telegram."""
    cuerpo = await _read_limited(request, 2_000)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "La solicitud es demasiado grande."})
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "El cuerpo no es un JSON válido."})
    decision = datos.get("decision") if isinstance(datos, dict) else None
    try:
        res = await asyncio.to_thread(hitl.review_event, user, event_id, decision, "web",
                                      auth._clean_ip(auth.client_ip(request)), alerts.on_review_applied)
    except hitl.ReviewError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message, "code": ex.code})
    if not res["applied"]:
        return JSONResponse(status_code=409, content={"ok": False, "estado": res["estado"], "code": "ya_revisada",
                                                      "error": "Esta alerta ya fue revisada."})
    threading.Thread(target=alerts.after_review, args=(res, user), name="revision-envio", daemon=True).start()
    return JSONResponse(content={"ok": True, "estado": res["estado"]})


@app.get("/admin/pendientes", response_class=HTMLResponse)
def pending_page(request: Request, user: dict = Depends(auth.require_admin)):
    """La pantalla «Pendientes de revisión» (H9). Es solo para administradores y usa la API de /admin/review."""
    nonce = request.state.csp_nonce
    return HTMLResponse(pwa.inject(pages.render_pending(user, nonce), nonce))


# ---------------------- imagen en vivo de la mini PC (H7) y PWA ----------------------

@app.websocket("/edge/ws")
async def edge_ws(ws: WebSocket):
    """La mini PC abre y mantiene esta conexión; por aquí se le pide imagen y por aquí la envía (ver backend/live.py)."""
    await live.handle_edge(ws)


@app.websocket("/ws/camera/{camera_id}")
async def camera_ws(ws: WebSocket, camera_id: str):
    """El celular de quien tiene permiso mira la cámara en vivo."""
    await live.handle_viewer(ws, camera_id)


@app.get("/cameras/{camera_id}/ultima.jpg")
def camera_last_image(camera_id: str, user: dict = Depends(auth.current_user)):
    """La última imagen de la cámara (también es el plan B del visor: una foto cada 2 segundos)."""
    return live.last_image_response(user, camera_id)


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return Response(content=json.dumps(pwa.manifest(), ensure_ascii=False), media_type="application/manifest+json")


@app.get("/sw.js")
def pwa_service_worker():
    return Response(content=pwa.SERVICE_WORKER, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/offline.html", response_class=HTMLResponse)
def pwa_offline():
    return HTMLResponse(pwa.OFFLINE_HTML)


@app.get("/icons/{nombre}")
def pwa_icon(nombre: str):
    datos = pwa.icono(nombre)
    if datos is None:
        return JSONResponse(status_code=404, content={"error": "no encontrado"})
    return Response(content=datos, media_type="image/png")


# ---------------------- CÁMARA EN VIVO ----------------------


INGEST_MAX_JSON = 100_000                                           # evento sin foto
INGEST_MAX_FORM = ingest_media.MAX_PHOTO_BYTES + 150_000            # foto + evento + estructura del formulario


def _replay(cuerpo: bytes):
    """Una función `receive` que entrega un cuerpo que ya leímos, para poder analizarlo como formulario."""
    enviado = False

    async def receive():
        nonlocal enviado
        if enviado:
            return {"type": "http.disconnect"}
        enviado = True
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return receive


def _edge_event_id_of(community_id, edge_event_id):
    with db.cursor() as cur:
        cur.execute("SELECT id FROM events WHERE community_id = %s AND edge_event_id = %s",
                    (community_id, edge_event_id))
        fila = cur.fetchone()
    return fila["id"] if fila else None


@app.post("/ingest")
async def ingest(request: Request):
    """
    Recibe un evento de una mini PC. Se identifica con 'Authorization: Bearer <token>'.

    Dos formas de enviarlo:
      • application/json: el evento solo (como siempre).
      • multipart/form-data: el campo «evento» (el mismo JSON) y el campo «foto» (un JPEG de hasta 500 KB).
        El nombre de la foto lo pone el servidor.
    Si el evento trae edge_event_id y ya existe, responde 200 con duplicate=true y no guarda nada más.
    """
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    edge = await asyncio.to_thread(auth.edge_from_token, token)
    if edge is None:
        return JSONResponse(status_code=401, content={"error": "Token de la mini PC inválido."})
    es_formulario = request.headers.get("content-type", "").lower().startswith("multipart/form-data")
    cuerpo = await _read_limited(request, INGEST_MAX_FORM if es_formulario else INGEST_MAX_JSON)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "El evento o la foto es demasiado grande."})

    foto = None
    if es_formulario:
        try:
            formulario = await Request(request.scope, _replay(cuerpo)).form()
        except Exception:                                              # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "El formulario no es válido."})
        try:
            crudo = formulario.get("evento")
            if crudo is not None and not isinstance(crudo, str):       # llegó como archivo
                crudo = (await crudo.read()).decode("utf-8", "replace")
            archivo = formulario.get("foto")
            if archivo is not None:
                if isinstance(archivo, str):
                    return JSONResponse(status_code=415, content={"error": "La foto debe enviarse como archivo."})
                foto = await archivo.read()
        finally:
            await formulario.close()
        if crudo is None:
            return JSONResponse(status_code=422, content={"error": "Falta el campo «evento»."})
    else:
        crudo = cuerpo.decode("utf-8", "replace")
    try:
        e = json.loads(crudo)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "El cuerpo no es un JSON válido."})
    if es_formulario and isinstance(e, dict):
        e.pop("image", None)                                           # el nombre de la foto lo pone el servidor
    limpio, motivo = validate_event(e)
    if motivo:
        return JSONResponse(status_code=422, content={"error": motivo})
    if foto is not None:
        try:
            await asyncio.to_thread(ingest_media.validate_photo, foto)
        except ingest_media.PhotoError as ex:
            return JSONResponse(status_code=ex.status, content={"error": ex.message})
    camera_id = None
    if limpio.get("camera_id"):
        camara = await asyncio.to_thread(cameras.edge_camera, edge, limpio["camera_id"])
        if camara is None:
            return JSONResponse(status_code=422, content={"error": "Esa cámara no está asignada a este equipo."})
        camera_id = camara["id"]
    edge_event_id = limpio.get("edge_event_id")
    if edge_event_id:                                                  # un reenvío: no se escribe la foto de nuevo
        existente = await asyncio.to_thread(_edge_event_id_of, edge["community_id"], edge_event_id)
        if existente:
            return {"ok": True, "event_id": existente, "duplicate": True}
    nombre = None
    if foto is not None:
        nombre = await asyncio.to_thread(ingest_media.save_photo, foto, EVIDENCE_DIR)
        limpio["image"] = nombre
    try:
        event_id = await asyncio.to_thread(
            insert_event, limpio, None, edge["community_id"], "edge", camera_id, True,
            limpio.get("occurred_at"), edge_event_id)
    except BaseException:
        if nombre:
            ingest_media.remove_photo(EVIDENCE_DIR, nombre)            # no queda una foto sin evento
        raise
    if event_id is None:                                               # otro envío igual llegó primero
        if nombre:
            ingest_media.remove_photo(EVIDENCE_DIR, nombre)
        existente = await asyncio.to_thread(_edge_event_id_of, edge["community_id"], edge_event_id)
        return {"ok": True, "event_id": existente, "duplicate": True}
    return {"ok": True, "event_id": event_id, "duplicate": False}


# ---------------------- descarga de evidencia (Plan Premium) ----------------------

@app.get("/events/{event_id}/evidencia.zip")
def event_evidence_zip(event_id: str, request: Request, user: dict = Depends(auth.current_user)):
    """La foto, el clip y la ficha del evento con sus huellas SHA-256, para una denuncia. Solo cámaras Premium; queda en audit_log."""
    if not event_id.isdigit() or len(event_id) > 18:
        return JSONResponse(status_code=404, content={"error": "No encontrado."})
    with db.cursor() as cur:
        estado, contenido, nombre = evidencia_premium.zip_evidencia(
            cur, user, int(event_id), EVIDENCE_DIR, auth._clean_ip(request.client.host if request.client else None))
    if estado != 200:
        return JSONResponse(status_code=estado, content={"error": contenido})
    return Response(content=contenido, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"', "Cache-Control": "no-store"})


# ---------------------- clip de video del evento (Plan Premium) ----------------------

CLIP_MAX_FORM = ingest_media.MAX_CLIP_BYTES + 20_000                   # clip + campos + estructura del formulario
_EDGE_EVENT_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")   # el mismo formato que /ingest


def _guardar_clip(edge, edge_event_id, datos, duracion):
    """
    Guarda el clip de un evento ya recibido. Devuelve (código HTTP, cuerpo).
    409 si el evento todavía no llegó (la mini PC reintenta), 422 si la cámara no tiene clips en su plan.
    """
    with db.cursor() as cur:
        cur.execute("""SELECT id, camera_id::text AS camera_id FROM events WHERE community_id = %s AND edge_event_id = %s""",
                    (edge["community_id"], edge_event_id))
        ev = cur.fetchone()
        if ev is None:
            return 409, {"error": "El evento de este clip todavía no llegó al servidor."}
        plan = planes.de_camara(cur, ev["camera_id"]) if ev["camera_id"] else {"highlight_clips": False}
        if not plan.get("highlight_clips") or not plan.get("clip_seconds"):
            return 422, {"error": "Esta cámara no tiene clips en su plan."}
        cur.execute("SELECT storage_key FROM media_files WHERE event_id = %s AND kind = 'clip'", (ev["id"],))
        previo = cur.fetchone()
        if previo:
            return 200, {"ok": True, "event_id": ev["id"], "duplicate": True}
    nombre, huella = ingest_media.save_clip(datos, EVIDENCE_DIR)
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO media_files (community_id, camera_id, event_id, kind, is_highlight, storage_key, size_bytes,
                                                   duration_s, sha256, expires_at)
                           VALUES (%s, %s, %s, 'clip', true, %s, %s, %s, %s, now() + make_interval(days => %s))
                           ON CONFLICT DO NOTHING RETURNING id""",
                        (edge["community_id"], ev["camera_id"], ev["id"], nombre, len(datos), duracion, huella,
                         int(plan.get("retention_days") or 7)))
            if cur.fetchone() is None:                                 # otro envío del mismo clip llegó primero
                ingest_media.remove_photo(EVIDENCE_DIR, nombre)
                return 200, {"ok": True, "event_id": ev["id"], "duplicate": True}
            cur.execute("""UPDATE alert_deliveries SET clip_status = 'pendiente'
                           WHERE event_id = %s AND status IN ('pending', 'sent') AND clip_status IS NULL""", (ev["id"],))
    except BaseException:
        ingest_media.remove_photo(EVIDENCE_DIR, nombre)
        raise
    return 200, {"ok": True, "event_id": ev["id"], "duplicate": False}


@app.post("/ingest/clip")
async def ingest_clip(request: Request):
    """
    Recibe el clip de video (MP4) de un evento que la mini PC ya envió. Solo para cámaras con clips en su plan (Premium).
    multipart/form-data: «edge_event_id» (el del evento), «duration_s» (opcional) y «clip» (el archivo, hasta 20 MB).
    """
    edge = await asyncio.to_thread(auth.edge_from_token, _bearer(request))
    if edge is None:
        return JSONResponse(status_code=401, content={"error": "Token de la mini PC inválido."})
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return JSONResponse(status_code=415, content={"error": "El clip se envía como multipart/form-data."})
    cuerpo = await _read_limited(request, CLIP_MAX_FORM)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "El clip es demasiado grande."})
    try:
        formulario = await Request(request.scope, _replay(cuerpo)).form()
    except Exception:                                                  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "El formulario no es válido."})
    try:
        eid, dur, archivo = formulario.get("edge_event_id"), formulario.get("duration_s"), formulario.get("clip")
        if not isinstance(eid, str) or not _EDGE_EVENT_RE.match(eid):
            return JSONResponse(status_code=422, content={"error": "Falta el campo «edge_event_id» o no es válido."})
        if archivo is None or isinstance(archivo, str):
            return JSONResponse(status_code=422, content={"error": "Falta el archivo «clip»."})
        datos = await archivo.read()
    finally:
        await formulario.close()
    try:
        ingest_media.validate_clip(datos)
    except ingest_media.ClipError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message})
    estado, respuesta = await asyncio.to_thread(_guardar_clip, edge, eid.lower(), datos, ingest_media.clip_duration(dur if isinstance(dur, str) else None))
    return JSONResponse(status_code=estado, content=respuesta)


# ---------------------- fuentes de cámara (H4) ----------------------

def _bearer(request: Request):
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


async def _read_limited(request: Request, limit: int):
    """El cuerpo de la petición, o None si pasa del límite (se corta sin leerlo completo)."""
    try:
        if int(request.headers.get("content-length", "0")) > limit:
            return None
    except ValueError:
        pass
    cuerpo = bytearray()
    async for trozo in request.stream():
        cuerpo += trozo
        if len(cuerpo) > limit:
            return None
    return bytes(cuerpo)


@app.post("/edge/inventory")
async def edge_inventory(request: Request):
    """La mini PC reporta las cámaras que ve. Se identifica con 'Authorization: Bearer <token>'."""
    edge = await asyncio.to_thread(auth.edge_from_token, _bearer(request))
    if edge is None:
        return JSONResponse(status_code=401, content={"error": "Token de la mini PC inválido."})
    cuerpo = await _read_limited(request, sources.MAX_BODY_BYTES)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "El inventario es demasiado grande."})
    try:
        payload = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "El cuerpo no es un JSON válido."})
    limpio, motivo = sources.validate_inventory(payload)
    if motivo:
        return JSONResponse(status_code=422, content={"error": motivo})
    resultado = await asyncio.to_thread(sources.upsert_inventory, edge, limpio)
    return {"ok": True, **resultado}


@app.get("/edge/config")
async def edge_config(request: Request):
    """La configuración de la mini PC: solo sus cámaras asignadas, el horario sensible y las zonas."""
    edge = await asyncio.to_thread(auth.edge_from_token, _bearer(request))
    if edge is None:
        return JSONResponse(status_code=401, content={"error": "Token de la mini PC inválido."})
    return await asyncio.to_thread(sources.edge_config, edge)


@app.get("/admin/sources")
def admin_sources(user: dict = Depends(auth.require_admin)):
    return {"sources": sources.list_sources(user)}


async def _assign(request: Request, source_id: str, user: dict, reassign: bool):
    cuerpo = await _read_limited(request, 10_000)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "La solicitud es demasiado grande."})
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "El cuerpo no es un JSON válido."})
    try:
        resultado = await asyncio.to_thread(sources.assign_source, user, source_id, datos, reassign,
                                            auth._clean_ip(auth.client_ip(request)))
    except sources.AssignError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message, "code": ex.code})
    aviso = resultado.pop("_aviso", None)
    if aviso and aviso["owner_id"] and aviso["owner_id"] != str(user["id"]):
        lugar = f" ({aviso['place']})" if aviso["place"] else ""
        texto = f"INTIVISION: el administrador te asignó la cámara «{aviso['name']}»{lugar}. Ya aparece en tu lista de Cámaras."
        threading.Thread(target=alerts.notify_user, args=(aviso["owner_id"], texto), name="aviso-asignacion", daemon=True).start()
    anterior = (aviso or {}).get("anterior")      # quien tenía la cámara antes: ya no la ve, así que se le dice (salvo que siga siendo suya)
    if anterior and anterior["owner_id"] and anterior["owner_id"] != str(user["id"]) and anterior["owner_id"] != aviso["owner_id"]:
        texto = f"INTIVISION: la cámara «{anterior['name']}» ya no está asignada a ti. Quedó archivada y su historial se conserva."
        threading.Thread(target=alerts.notify_user, args=(anterior["owner_id"], texto), name="aviso-asignacion", daemon=True).start()
    return JSONResponse(status_code=201, content=resultado)


@app.post("/admin/sources/{source_id}/assign")
async def admin_assign_source(source_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """Convierte una fuente en una cámara de un vecino o de una zona común. Una fuente ya asignada responde 409."""
    return await _assign(request, source_id, user, reassign=False)


@app.post("/admin/sources/{source_id}/reassign")
async def admin_reassign_source(source_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """Cambia la asignación: archiva la cámara anterior (sin borrar sus eventos) y crea la nueva."""
    return await _assign(request, source_id, user, reassign=True)


@app.post("/admin/sources/{source_id}/unassign")
async def admin_unassign_source(source_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """Quita la asignación: archiva la cámara sin borrar sus eventos. Una fuente sin asignar responde 409."""
    try:
        resultado = await asyncio.to_thread(sources.unassign_source, user, source_id, auth._clean_ip(auth.client_ip(request)))
    except sources.AssignError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message, "code": ex.code})
    aviso = resultado.pop("_aviso", None)
    if aviso and aviso["owner_id"] and aviso["owner_id"] != str(user["id"]):
        texto = f"INTIVISION: el administrador quitó la asignación de tu cámara «{aviso['name']}». Quedó archivada y su historial se conserva."
        threading.Thread(target=alerts.notify_user, args=(aviso["owner_id"], texto), name="aviso-asignacion", daemon=True).start()
    return JSONResponse(content=resultado)


@app.post("/admin/sources/{source_id}/identify")
async def admin_identify_source(source_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """
    «Identificar» (H5): le pide a la mini PC una imagen de una cámara SIN asignar durante unos segundos, para saber cuál es.
    Solo se puede con una fuente sin asignar, disponible y de una mini PC conectada. Queda en audit_log.
    """
    try:
        info = await asyncio.to_thread(sources.identify_info, user, source_id)
    except sources.AssignError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message, "code": ex.code})
    if info["asignada"]:
        return JSONResponse(status_code=409, content={"error": "Esa cámara ya está asignada: solo se puede identificar una sin asignar.", "code": "ya_asignada"})
    if info["state"] != "available":
        return JSONResponse(status_code=409, content={"error": "La cámara está desconectada: no se puede identificar.", "code": "desconectada"})
    if not live.hub.rate_ok("identify:" + str(user["id"])):
        return JSONResponse(status_code=429, content={"error": "Pediste demasiadas imágenes seguidas. Espera un momento.", "code": "limite"})
    estado = await live.hub.start_identify(info["id"], info["edge_id"], info["device_key"])
    if estado == "sin_conexion":
        return JSONResponse(status_code=409, content={"error": "La mini PC no está conectada en este momento.", "code": "equipo_sin_conexion"})
    if estado == "ocupado":
        return JSONResponse(status_code=429, content={"error": "Esa mini PC ya está identificando otras cámaras. Espera unos segundos.", "code": "ocupado"})
    await asyncio.to_thread(sources.audit_identify, user, info, auth._clean_ip(auth.client_ip(request)))
    return JSONResponse(status_code=202, content={"seconds": live.IDENTIFY_S})


@app.get("/admin/sources/{source_id}/identificar.jpg")
async def admin_identify_image(source_id: str, user: dict = Depends(auth.require_admin)):
    """La última imagen de la cámara que se está identificando. 404 mientras todavía no llega la primera."""
    try:
        info = await asyncio.to_thread(sources.identify_info, user, source_id)
    except sources.AssignError as ex:
        return JSONResponse(status_code=ex.status, content={"error": ex.message, "code": ex.code})
    if info["asignada"]:
        return JSONResponse(status_code=409, content={"error": "Esa cámara ya está asignada.", "code": "ya_asignada"})
    jpeg = live.hub.identify_frame(info["id"])
    if jpeg is None:
        return JSONResponse(status_code=404, content={"error": "Todavía no hay imagen.", "code": "sin_imagen"})
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/admin/equipos/datos")
async def admin_equipos_datos(user: dict = Depends(auth.require_admin)):
    """Los equipos con sus cámaras y las personas a quienes se pueden asignar (lo usa la pantalla «Equipos y cámaras»)."""
    return JSONResponse(content=await asyncio.to_thread(sources.list_equipos, user))


@app.get("/admin/equipos", response_class=HTMLResponse)
def equipos_page(request: Request, user: dict = Depends(auth.require_admin)):
    """La pantalla «Equipos y cámaras» (H5): solo para administradores."""
    nonce = request.state.csp_nonce
    return HTMLResponse(pwa.inject(pages.render_equipos(user, nonce), nonce))


# ---------------------- panel de incidentes (H8) ----------------------

def _panel_error(e):
    return JSONResponse(status_code=e.status, content={"error": e.message, "code": e.code})


@app.get("/admin/panel/datos")
async def admin_panel_datos(desde: Optional[str] = None, hasta: Optional[str] = None, camara: Optional[str] = None,
                            falsas: bool = False, user: dict = Depends(auth.require_admin)):
    """Las tarjetas, los gráficos, el ranking, las señales y los equipos del panel (ver backend/dashboard.py para las reglas)."""
    try:
        return JSONResponse(content=await asyncio.to_thread(dashboard.panel, user, desde, hasta, camara, falsas))
    except dashboard.DashboardError as e:
        return _panel_error(e)


@app.post("/admin/panel/horario")
async def admin_panel_horario(request: Request, user: dict = Depends(auth.require_admin)):
    """Aplica el horario sensible sugerido (la hora de inicio que el administrador vio). Solo JSON: un formulario de otro sitio no puede mandarlo."""
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return JSONResponse(status_code=415, content={"error": "Se esperaba JSON.", "code": "tipo_no_admitido"})
    try:
        cuerpo = await request.json()
    except ValueError:
        cuerpo = None
    if not isinstance(cuerpo, dict):
        return JSONResponse(status_code=422, content={"error": "El cuerpo debe ser un objeto JSON con la hora de inicio.", "code": "cuerpo_invalido"})
    try:
        return JSONResponse(content=await asyncio.to_thread(dashboard.aplicar_horario, user, cuerpo.get("inicio"), auth._clean_ip(auth.client_ip(request))))
    except dashboard.DashboardError as e:
        return _panel_error(e)


@app.get("/admin/panel/exportar.csv")
async def admin_panel_exportar(request: Request, desde: Optional[str] = None, hasta: Optional[str] = None,
                               camara: Optional[str] = None, user: dict = Depends(auth.require_admin)):
    """Los eventos del rango en CSV para Excel. Queda en audit_log quién exportó, qué rango y cuántas filas."""
    try:
        f = await asyncio.to_thread(dashboard.leer_filtros, user, desde, hasta, camara)
        dashboard.comprobar_espera(user)
        filas = await asyncio.to_thread(dashboard.contar_exportacion, user, f)
        await asyncio.to_thread(dashboard.auditar_exportacion, user, f, min(filas, dashboard.LIMITE_CSV), auth._clean_ip(auth.client_ip(request)))
    except dashboard.DashboardError as e:
        return _panel_error(e)
    return StreamingResponse(dashboard.generar_csv(user, f), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{dashboard.nombre_archivo(f)}"'})


@app.get("/admin", response_class=HTMLResponse)
def panel_page(request: Request, user: dict = Depends(auth.require_admin)):
    """El panel de incidentes (H8): solo para administradores. Los datos los pide el navegador a /admin/panel/datos."""
    nonce = request.state.csp_nonce
    return HTMLResponse(pwa.inject(pages.render_panel(user, nonce), nonce))


# ---------------------- mapa de calor por cámara y ubicación de las cámaras (H8, fase D) ----------------------

_LEAFLET = {"leaflet.js": "application/javascript; charset=utf-8", "leaflet.css": "text/css; charset=utf-8"}
_STATIC_LEAFLET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "leaflet")


@app.get("/static/leaflet/{archivo}")
def static_leaflet(archivo: str):
    """Leaflet se sirve desde aquí y no desde un CDN: la política de seguridad no permite scripts de terceros. Solo estos dos archivos."""
    tipo = _LEAFLET.get(archivo)
    if tipo is None:
        return JSONResponse(status_code=404, content={"error": "No existe."})
    return FileResponse(os.path.join(_STATIC_LEAFLET, archivo), media_type=tipo)


def _mapa_error(e):
    return JSONResponse(status_code=e.status, content={"error": e.message, "code": e.code})


async def _cuerpo_objeto(request: Request):
    """El cuerpo JSON como diccionario, o la respuesta de error. Solo JSON: un formulario de otro sitio no puede mandarlo."""
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return None, JSONResponse(status_code=415, content={"error": "Se esperaba JSON.", "code": "tipo_no_admitido"})
    try:
        cuerpo = await request.json()
    except ValueError:
        cuerpo = None
    if not isinstance(cuerpo, dict):
        return None, JSONResponse(status_code=422, content={"error": "El cuerpo debe ser un objeto JSON.", "code": "cuerpo_invalido"})
    return cuerpo, None


@app.get("/mapa/teselas/{z}/{x}/{y}.png")
async def mapa_tesela(z: int, x: int, y: int, user: dict = Depends(auth.current_user)):
    """Las imágenes del mapa, por nuestro servidor y con caché (ver backend/mapa.py). Las pide cualquier persona con sesión: los dueños
    también eligen la ubicación de su cámara sobre el mapa."""
    try:
        datos = await asyncio.to_thread(mapa.obtener_tesela, str(user["id"]), z, x, y)
    except mapa.MapaError as e:
        return _mapa_error(e)
    return Response(content=datos, media_type="image/png")


@app.get("/admin/mapa", response_class=HTMLResponse)
def mapa_page(request: Request, user: dict = Depends(auth.require_admin)):
    """El mapa de calor por cámara (H8): solo para administradores."""
    nonce = request.state.csp_nonce
    return HTMLResponse(pwa.inject(pages.render_mapa(user, nonce), nonce))


@app.get("/admin/mapa/datos")
async def admin_mapa_datos(desde: Optional[str] = None, hasta: Optional[str] = None, modo: str = "incidentes",
                           user: dict = Depends(auth.require_admin)):
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.datos_mapa, user, desde, hasta, modo))
    except (mapa.MapaError, dashboard.DashboardError) as e:
        return _mapa_error(e)


@app.post("/admin/camaras/{camera_id}/ubicacion")
async def admin_camara_ubicacion(camera_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    """El administrador ubica una cámara de ZONA COMÚN (las privadas las ubica su dueño)."""
    cuerpo, error = await _cuerpo_objeto(request)
    if error:
        return error
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.fijar_ubicacion, user, camera_id, cuerpo, auth._clean_ip(auth.client_ip(request)), "admin"))
    except mapa.MapaError as e:
        return _mapa_error(e)


@app.post("/admin/camaras/{camera_id}/ubicacion/quitar")
async def admin_camara_ubicacion_quitar(camera_id: str, request: Request, user: dict = Depends(auth.require_admin)):
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.quitar_ubicacion, user, camera_id, auth._clean_ip(auth.client_ip(request)), "admin"))
    except mapa.MapaError as e:
        return _mapa_error(e)


@app.post("/cameras/{camera_id}/ubicacion")
async def camera_ubicacion(camera_id: str, request: Request, user: dict = Depends(auth.current_user)):
    """El dueño de una cámara privada fija su ubicación y decide si el administrador puede verla en el mapa (apagado por defecto)."""
    cuerpo, error = await _cuerpo_objeto(request)
    if error:
        return error
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.fijar_ubicacion, user, camera_id, cuerpo, auth._clean_ip(auth.client_ip(request)), "dueno"))
    except mapa.MapaError as e:
        return _mapa_error(e)


@app.post("/cameras/{camera_id}/ubicacion/quitar")
async def camera_ubicacion_quitar(camera_id: str, request: Request, user: dict = Depends(auth.current_user)):
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.quitar_ubicacion, user, camera_id, auth._clean_ip(auth.client_ip(request)), "dueno"))
    except mapa.MapaError as e:
        return _mapa_error(e)


@app.post("/cameras/{camera_id}/ubicacion/mapa")
async def camera_ubicacion_mapa(camera_id: str, request: Request, user: dict = Depends(auth.current_user)):
    """El dueño da o retira el permiso de mostrar su cámara en el mapa del administrador."""
    cuerpo, error = await _cuerpo_objeto(request)
    if error:
        return error
    try:
        return JSONResponse(content=await asyncio.to_thread(mapa.cambiar_visibilidad_mapa, user, camera_id, cuerpo.get("visible"), auth._clean_ip(auth.client_ip(request))))
    except mapa.MapaError as e:
        return _mapa_error(e)


@app.get("/events")
def events(limit: int = 100, user: dict = Depends(auth.current_user)):
    return list_events(user, limit)


@app.get("/evidence/{name}")
def evidence(name: str, request: Request, user: dict = Depends(auth.current_user)):
    """Entrega una imagen solo a su dueño o al administrador de su comunidad."""
    if not IMAGE_NAME_RE.match(name):
        return JSONResponse(status_code=404, content={"error": "no encontrado"})   # nombres raros ni se buscan
    safe = name
    with db.cursor() as cur:
        alcance, args = acceso.posicional(acceso.evidencias(user, "m", "c"), user)     # quién ve qué: acceso.py
        cur.execute(f"""SELECT m.user_id, c.user_id AS camera_owner FROM media_files m LEFT JOIN cameras c ON c.id = m.camera_id
                        WHERE m.storage_key = %s AND {alcance}""", [safe] + args)
        row = cur.fetchone()
        path = os.path.join(EVIDENCE_DIR, safe)
        if row is None or not os.path.exists(path):
            return JSONResponse(status_code=404, content={"error": "no encontrado"})
        dueno = row.get("camera_owner") or row["user_id"]     # de quién es la imagen: el dueño de la cámara o quien la subió
        if user["role"] == "community_admin" and dueno is not None and str(dueno) != str(user["id"]):
            # Queda registro de que el administrador abrió una imagen de otra persona. Las de una zona común no tienen dueño:
            # se miran todo el día desde «Pendientes» y no hay a quién anotar.
            cur.execute(
                """INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id,
                                          action, entity, entity_id, ip)
                   VALUES ('user', %s, %s, %s, 'view_evidence', 'media_file', %s, %s)""",
                (user["id"], user["community_id"], dueno, safe,
                 auth._clean_ip(request.client.host if request.client else None)))
    return FileResponse(path)


# ---------------------- cámaras ----------------------

def _parse_date(text):
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _camera_not_found(user):
    # La misma respuesta para «no existe» y «no es tuya»: no se revela qué cámaras hay.
    return HTMLResponse(pages.render_cameras(user, cameras.list_cameras(user),
                                             message="Esa cámara no existe o no tienes acceso."), status_code=404)


@app.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request, user: dict = Depends(auth.current_user)):
    return HTMLResponse(pwa.inject(pages.render_cameras(user, cameras.list_cameras(user)), request.state.csp_nonce))


@app.get("/")
def home(user: dict = Depends(auth.current_user)):
    """La puerta de entrada: el administrador va al panel y el vecino a sus cámaras. Sin sesión, el manejador de errores lo lleva al inicio de sesión."""
    return RedirectResponse("/admin" if user["role"] == "community_admin" else "/cameras", status_code=303)


@app.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_page(camera_id: str, request: Request, nivel: str = "", desde: str = "", hasta: str = "",
                limite: str = "12", q: str = "", user: dict = Depends(auth.current_user)):
    cam = cameras.get_camera(user, camera_id)
    if cam is None:
        return _camera_not_found(user)
    nivel = nivel.upper() if nivel.upper() in cameras.LEVELS else ""
    d_desde, d_hasta = _parse_date(desde), _parse_date(hasta)
    aviso = "Una fecha no es válida y se ignoró." if (desde and not d_desde) or (hasta and not d_hasta) else None
    limite = max(12, min(int(limite), 200)) if limite.isascii() and limite.isdigit() else 12
    texto = " ".join(q.split())[:80] if cam.get("premium") else ""          # buscar en las descripciones es del Plan Premium
    rows = list_events(user, limite + 1, camera_id=cam["id"], level=nivel or None, desde=d_desde, hasta=d_hasta, texto=texto or None)
    if user["role"] == "community_admin" and cam["kind"] == "private" and cam["user_id"] != user["id"]:
        with db.cursor() as cur:        # el administrador abrió la cámara de otra persona: queda registrado
            cur.execute(
                """INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip)
                   VALUES ('user', %s, %s, %s, 'view_camera', 'camera', %s, %s)""",
                (user["id"], user["community_id"], cam["user_id"], cam["id"], auth._clean_ip(auth.client_ip(request))))
    return HTMLResponse(pwa.inject(pages.render_camera(
        user, cam, rows[:limite], nivel, desde if d_desde else "", hasta if d_hasta else "", limite,
        len(rows) > limite, aviso, nonce=request.state.csp_nonce, ubicacion=mapa.ficha_ubicacion(user, cam), texto=texto), request.state.csp_nonce))


@app.post("/cameras/{camera_id}/ia")
def camera_ia(camera_id: str, activa: str = Form(""), user: dict = Depends(auth.current_user)):
    """Activa o desactiva la descripción con IA en una cámara: su dueño, o el administrador si es zona común. Queda en la auditoría."""
    cam = cameras.get_camera(user, camera_id)
    if cam is None:
        return _camera_not_found(user)
    puede = (cam["kind"] == "private" and cam["user_id"] == user["id"]) or (cam["kind"] == "common" and user["role"] == "community_admin")
    if puede:
        with db.cursor() as cur:
            cur.execute("UPDATE cameras SET ai_enabled = %s WHERE id = %s", (activa == "1", cam["id"]))
            cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id, meta)
                           VALUES ('user', %s, %s, 'camera_ai_toggle', 'camera', %s, %s::jsonb)""",
                        (str(user["id"]), user["community_id"], str(cam["id"]), json.dumps({"activa": activa == "1"})))
    return RedirectResponse(f"/cameras/{cam['id']}", status_code=303)


@app.post("/cameras/{camera_id}/visibilidad")
def camera_visibility(camera_id: str, visible: str = Form(""), user: dict = Depends(auth.current_user)):
    if not cameras.set_admin_visibility(user, camera_id, visible == "1"):
        return _camera_not_found(user)
    return RedirectResponse(f"/cameras/{camera_id}", status_code=303)


# ---------------------- alertas por Telegram ----------------------

ALERT_NOTICES = {
    "guardado": ("ok", "Preferencias guardadas."),
    "bienvenida": ("ok", "¡Bienvenido a INTIVISION! Vincula tu Telegram para recibir las alertas al instante."),
    "probado": ("ok", "Te mandamos un mensaje de prueba. Revisa tu Telegram."),
    "desvinculado": ("ok", "Telegram desvinculado. Ya no recibirás alertas."),
    "espera": ("alerta", "Espera unos segundos antes de pedir otra prueba."),
    "fallo": ("error", "Telegram no pudo entregar el mensaje. Si bloqueaste el bot, desbloquéalo y vincula de nuevo."),
    "no_vinculado": ("error", "Primero vincula tu Telegram."),
}


def _alerts_response(request, user, notice=None, link=None, status=200):
    return HTMLResponse(pwa.inject_sesion(pages.render_alerts(user, alerts.get_channel(user["id"]), alerts.available(),
                                                              alerts.recent_deliveries(user["id"]), notice=notice, link=link),
                                          request.state.csp_nonce),
                        status_code=status)


# ---------------------- Mi plan ----------------------

PLAN_NOTICES = {
    "pagado": ("ok", "Pago recibido. Ya tienes el Plan Premium: tu mini PC lo aplica en unos 30 segundos."),
    "ya_premium": ("info", "Ya tienes el Plan Premium."),
    "prohibido": ("error", "Solo el administrador puede pagar el plan de la comunidad."),
    "sesion": ("alerta", "La sesión de pago venció o ya se usó. Empieza de nuevo."),
    "no_autorizado": ("error", "El pago no se autorizó en tu banco. No se cobró nada."),
    "cancelado": ("ok", "Listo: al final del período vuelves al Plan Base. Hasta entonces sigues con Premium."),
    "reactivado": ("ok", "Listo: sigues con el Plan Premium y se renovará normalmente."),
    "comunidad_cancelado": ("ok", "Listo: al final del período la comunidad vuelve al Plan Base."),
    "comunidad_reactivado": ("ok", "Listo: la comunidad sigue con el Plan Premium."),
    "baja_programada": ("alerta", "Tu baja quedó programada para el final del período pagado. Puedes deshacerla hasta entonces."),
    "confirmacion": ("error", "Para darte de baja escribe exactamente DAR DE BAJA."),
    "unico_admin": ("error", "Eres el único administrador de la comunidad: antes de irte, otra persona debe ser administradora."),
    "sin_cambios": ("info", "No había nada que cambiar."),
    "mp": ("error", "No pudimos comunicarnos con Mercado Pago. No se cambió nada; intenta de nuevo en unos minutos."),
    "sin_correo": ("error", "Para pagar con Mercado Pago tu cuenta necesita un correo electrónico. Pídele al administrador que lo agregue."),
    "mp_vuelta": ("info", "Estamos confirmando tu pago con Mercado Pago. En unos segundos verás tu Plan Premium; actualiza la página."),
}
_SIN_CACHE = {"Cache-Control": "no-store"}


@app.post("/plan/checkout")
def plan_checkout(scope: str = Form(""), user: dict = Depends(auth.current_user)):
    """Abre una sesión de pago (como Stripe Checkout) y lleva a la página de pago."""
    try:
        with db.cursor() as cur:
            sid = suscripciones.crear_sesion(cur, user, scope)
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)
    if suscripciones.pasarela() == "mercadopago":
        return RedirectResponse(f"/pagos/mercadopago/ir/{sid}", status_code=303)
    return RedirectResponse(f"/pagos/checkout/{sid}", status_code=303)


def _base_publica(request) -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


@app.get("/pagos/mercadopago/ir/{sid}", response_class=HTMLResponse)
def mercadopago_ir(sid: str, request: Request, user: dict = Depends(auth.current_user)):
    """Crea la suscripción en Mercado Pago y lleva a su página de pago (con un enlace, no con el formulario: la política de
    contenido solo deja enviar formularios a este sitio)."""
    try:
        with db.cursor() as cur:
            s = suscripciones.sesion(cur, user, sid)
            if s is None or s["status"] != "open":
                return RedirectResponse("/plan?m=sesion", status_code=303)
            if s.get("provider_ref"):
                url = mercadopago.obtener(s["provider_ref"]).get("init_point")
            else:
                url = suscripciones.iniciar_mercadopago(cur, user, sid, _base_publica(request) + "/pagos/mercadopago/vuelta")
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)
    except mercadopago.ErrorMP:
        return RedirectResponse("/plan?m=mp", status_code=303)
    if not url or not url.startswith("https://"):
        return RedirectResponse("/plan?m=mp", status_code=303)
    return HTMLResponse(pages.render_mp_ir(s, url), headers=_SIN_CACHE)


@app.get("/pagos/mercadopago/vuelta")
def mercadopago_vuelta(preapproval_id: str = "", user: dict = Depends(auth.current_user)):
    """Mercado Pago devuelve aquí a la persona. Si trae el id de la suscripción, se sincroniza en el acto (sin esperar el webhook)."""
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", preapproval_id or ""):
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1 FROM checkout_sessions WHERE provider_ref = %s AND user_id = %s", (preapproval_id, user["id"]))
                if cur.fetchone() and suscripciones.sincronizar_suscripcion(cur, preapproval_id) == "activada":
                    return RedirectResponse("/plan?m=pagado", status_code=303)
        except mercadopago.ErrorMP:
            pass
    return RedirectResponse("/plan?m=mp_vuelta", status_code=303)


@app.post("/pagos/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    """Avisos de Mercado Pago, con la firma x-signature verificada. Tipos: subscription_preapproval y subscription_authorized_payment."""
    cuerpo = await _read_limited(request, 65_536)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "Aviso demasiado grande."})
    try:
        datos = json.loads(cuerpo or b"{}")
    except ValueError:
        datos = {}
    data_id = request.query_params.get("data.id") or str((datos.get("data") or {}).get("id") or "")
    tipo = request.query_params.get("type") or datos.get("type") or ""
    if not mercadopago.verificar_firma(request.headers.get("x-signature", ""), request.headers.get("x-request-id", ""), data_id):
        return JSONResponse(status_code=401, content={"error": "Firma no válida."})
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data_id):
        return {"ok": True, "resultado": "ignorado"}

    def aplicar():
        with db.cursor() as cur:
            if tipo == "subscription_preapproval":
                return suscripciones.sincronizar_suscripcion(cur, data_id)
            if tipo == "subscription_authorized_payment":
                return suscripciones.registrar_cuota(cur, data_id)
            return "ignorado"
    try:
        resultado = await asyncio.to_thread(aplicar)
    except mercadopago.ErrorMP:
        return JSONResponse(status_code=502, content={"error": "No se pudo consultar a Mercado Pago; reintentar."})
    return {"ok": True, "resultado": resultado}


@app.get("/pagos/checkout/{sid}", response_class=HTMLResponse)
def checkout_page(sid: str, user: dict = Depends(auth.current_user)):
    with db.cursor() as cur:
        s = suscripciones.sesion(cur, user, sid)
    if s is None or s["status"] != "open":
        return RedirectResponse("/plan?m=sesion", status_code=303)
    return HTMLResponse(pages.render_checkout(s), headers=_SIN_CACHE)


@app.post("/pagos/checkout/{sid}", response_class=HTMLResponse)
def checkout_pay(sid: str, numero: str = Form(""), vence: str = Form(""), cvc: str = Form(""), nombre: str = Form(""),
                 user: dict = Depends(auth.current_user)):
    """Procesa la tarjeta. El número nunca se guarda ni se registra; un rechazo queda en el historial de pagos."""
    error = resultado = None
    with db.cursor() as cur:
        try:
            resultado = suscripciones.pagar(cur, user, sid, numero, vence, cvc, nombre)
        except suscripciones.ErrorPago as e:
            error = e
        s = suscripciones.sesion(cur, user, sid)
    if error is not None:
        if error.codigo == "sesion" or s is None:
            return RedirectResponse("/plan?m=sesion", status_code=303)
        return HTMLResponse(pages.render_checkout(s, error=error.mensaje, nombre=nombre[:80]), status_code=402 if error.codigo != "formulario" else 422,
                            headers=_SIN_CACHE)
    tipo, dato = resultado
    if tipo == "3ds":
        return RedirectResponse(f"/pagos/3ds/{dato}", status_code=303)
    _avisar_pago(user, dato)
    return RedirectResponse("/plan?m=pagado", status_code=303)


def _avisar_pago(user, pago):
    texto = (f"✅ Pago recibido: {suscripciones.soles(pago['amount_cents'])} · comprobante {pago['receipt_number']}. "
             "Ya tienes el Plan Premium (clip de video, posibles armas y más).")
    threading.Thread(target=alerts.notify_user, args=(user["id"], texto), daemon=True).start()


def _pago_3ds(cur, user, pid):
    cur.execute("SELECT * FROM payments WHERE id = %s AND user_id = %s AND status = 'requires_action'", (pid, user["id"]))
    fila = cur.fetchone()
    return dict(fila) if fila else None


@app.post("/plan/cancelar")
def plan_cancelar(user: dict = Depends(auth.current_user)):
    try:
        with db.cursor() as cur:
            ok = suscripciones.cancelar_premium(cur, user)
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)

    return RedirectResponse(f"/plan?m={'cancelado' if ok else 'sin_cambios'}", status_code=303)


@app.post("/plan/reactivar")
def plan_reactivar(user: dict = Depends(auth.current_user)):
    try:
        with db.cursor() as cur:
            ok = suscripciones.reactivar(cur, user)
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)

    return RedirectResponse(f"/plan?m={'reactivado' if ok else 'sin_cambios'}", status_code=303)


@app.post("/plan/comunidad/{accion}")
def plan_comunidad(accion: str, user: dict = Depends(auth.current_user)):
    if accion not in ("cancelar", "reactivar") or user["role"] != "community_admin":
        return RedirectResponse("/plan?m=prohibido", status_code=303)
    try:
        with db.cursor() as cur:
            ok = suscripciones.cancelar_comunidad(cur, user, cancelar=accion == "cancelar")
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)
    destino = "comunidad_cancelado" if accion == "cancelar" else "comunidad_reactivado"
    return RedirectResponse(f"/plan?m={destino if ok else 'sin_cambios'}", status_code=303)


@app.post("/plan/baja")
def plan_baja(request: Request, confirmacion: str = Form(""), user: dict = Depends(auth.current_user)):
    """Dejar el servicio: con Premium pagado, al final del período; si no, en el acto (y se cierra la sesión)."""
    if confirmacion.strip().upper() != "DAR DE BAJA":
        return RedirectResponse("/plan?m=confirmacion", status_code=303)
    try:
        with db.cursor() as cur:
            cuando, info = suscripciones.dar_de_baja(cur, user)
    except suscripciones.ErrorPago as e:
        return RedirectResponse(f"/plan?m={e.codigo}", status_code=303)
    if cuando == "programada":
        return RedirectResponse("/plan?m=baja_programada", status_code=303)
    threading.Thread(target=suscripciones.enviar_avisos, args=(suscripciones.avisos_de_baja(info),), daemon=True).start()
    response = RedirectResponse("/login?m=baja", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/pagos/3ds/{pid}", response_class=HTMLResponse)
def tres_ds_page(pid: str, user: dict = Depends(auth.current_user)):
    with db.cursor() as cur:
        pago = _pago_3ds(cur, user, pid)
    if pago is None:
        return RedirectResponse("/plan?m=sesion", status_code=303)
    return HTMLResponse(pages.render_3ds(pid, pago), headers=_SIN_CACHE)


@app.post("/pagos/3ds/{pid}")
def tres_ds_decide(pid: str, decision: str = Form(""), user: dict = Depends(auth.current_user)):
    with db.cursor() as cur:
        pago = _pago_3ds(cur, user, pid)
        if pago is None:
            return RedirectResponse("/plan?m=sesion", status_code=303)
        if decision == "autorizar":
            confirmado = suscripciones.confirmar(cur, pid)
        else:
            suscripciones.fallar(cur, pid, "authentication_failed", "No se autorizó el pago en el banco.")
            confirmado = None
    if confirmado is None:
        return RedirectResponse("/plan?m=no_autorizado", status_code=303)
    _avisar_pago(user, confirmado)
    return RedirectResponse("/plan?m=pagado", status_code=303)


@app.post("/pagos/webhook")
async def pagos_webhook(request: Request):
    """Eventos de la pasarela (como los webhooks de Stripe): firma HMAC en «Pasarela-Signature», cada evento se aplica una sola vez."""
    cuerpo = await _read_limited(request, 65_536)
    if cuerpo is None:
        return JSONResponse(status_code=413, content={"error": "Evento demasiado grande."})
    try:
        def aplicar():
            with db.cursor() as cur:
                return suscripciones.procesar_evento(cur, cuerpo, request.headers.get("pasarela-signature", ""))
        resultado = await asyncio.to_thread(aplicar)
    except suscripciones.ErrorPago:
        return JSONResponse(status_code=400, content={"error": "Firma no válida."})
    except (ValueError, KeyError):
        return JSONResponse(status_code=400, content={"error": "Evento no válido."})
    return {"ok": True, "resultado": resultado}


@app.get("/pagos/comprobante/{pid}", response_class=HTMLResponse)
def comprobante_page(pid: str, user: dict = Depends(auth.current_user)):
    with db.cursor() as cur:
        c = suscripciones.comprobante(cur, user, pid)
    if c is None:
        return JSONResponse(status_code=404, content={"error": "No encontrado."})
    return HTMLResponse(pages.render_comprobante(c), headers=_SIN_CACHE)


def _plan_response(request, user, notice=None, status=200):
    with db.cursor() as cur:
        est = suscripciones.estado(cur, user)
    return HTMLResponse(pwa.inject_sesion(pages.render_plan(user, est, notice=notice), request.state.csp_nonce), status_code=status)


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, m: str = "", user: dict = Depends(auth.current_user)):
    if not acceso.tiene_plan(user):                    # vigilantes y familiares no pagan ni cambian planes
        return RedirectResponse("/cameras", status_code=303)
    return _plan_response(request, user, notice=PLAN_NOTICES.get(m))


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, m: str = "", user: dict = Depends(auth.current_user)):
    return _alerts_response(request, user, notice=ALERT_NOTICES.get(m))


@app.post("/alerts/link", response_class=HTMLResponse)
def alerts_link(request: Request, user: dict = Depends(auth.current_user)):
    link, problema = alerts.create_link(user)
    if problema:
        return _alerts_response(request, user, notice=("error", problema), status=429 if "demasiados" in problema else 503)
    return _alerts_response(request, user, link=link)


@app.post("/alerts/unlink")
def alerts_unlink(user: dict = Depends(auth.current_user)):
    alerts.unlink(user["id"])
    return RedirectResponse("/alerts?m=desvinculado", status_code=303)


@app.post("/alerts/settings")
def alerts_settings(request: Request, min_level: str = Form("MEDIO"), active: str = Form(""), user: dict = Depends(auth.current_user)):
    if min_level not in alerts.LEVELS:
        return _alerts_response(request, user, notice=("error", "Nivel no válido."), status=400)
    if not alerts.set_preferences(user["id"], min_level, active == "1"):
        return _alerts_response(request, user, notice=ALERT_NOTICES["no_vinculado"], status=400)
    return RedirectResponse("/alerts?m=guardado", status_code=303)


@app.post("/alerts/test")
def alerts_test(user: dict = Depends(auth.current_user)):
    ok, motivo = alerts.send_test(user)
    return RedirectResponse("/alerts?m=" + ("probado" if ok else motivo), status_code=303)


