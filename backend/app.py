import os
import re
import sys
import asyncio
import time
import tempfile
import logging
import datetime
import html
import secrets
from urllib.parse import urlparse

import cv2
import numpy as np
import psycopg2
import psycopg2.pool
from fastapi import (FastAPI, UploadFile, File, Request, WebSocket,
                     WebSocketDisconnect, Depends, Form)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse

# Permite importar los módulos del proyecto (engine, etc.) desde la carpeta padre.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db, auth, cameras, pages, alerts  # noqa: E402
from backend.pages import fill, SOURCE_LABELS  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "backend", "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
EVIDENCE_DIR = os.path.join(BASE, "events")  # compartido con el detector
CONFIG_PATH = os.path.join(BASE, "config.yaml")
TEMPLATES_DIR = os.path.join(BASE, "backend", "templates")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))   # igual que el límite de la página
TMP_PREFIX = "vigia_upload_"                                    # nombre de los videos en análisis

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backend")

# /docs, /redoc y /openapi.json mostrarían todo el mapa de la API a cualquiera: apagados salvo ENABLE_DOCS=1.
_docs = os.environ.get("ENABLE_DOCS") == "1"
app = FastAPI(title="GUARDIAN Backend", docs_url="/docs" if _docs else None,
              redoc_url="/redoc" if _docs else None, openapi_url="/openapi.json" if _docs else None)
_engine = None       # se crea en el primer análisis (carga modelos; puede tardar)
_dispatcher = None   # actuador (sirena por MQTT), creado una vez


def load_cfg():
    """config.yaml + credenciales del entorno / .env (ver settings.py)."""
    from settings import load_config
    return load_config(CONFIG_PATH)


def get_engine():
    global _engine
    if _engine is None:
        from engine import GuardianEngine  # import diferido (carga modelos)
        _engine = GuardianEngine(load_cfg())
    return _engine


# ---------------------- seguridad: utilidades ----------------------

MENSAJE_ACCESO = "Usuario o contraseña incorrectos."      # el MISMO texto para todo fallo: no revela qué cuentas existen
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MAX_LABEL_CHARS = 80
NIVELES = ("BAJO", "MEDIO", "ALTO")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def clean_filename(name):
    """Nombre seguro a partir del que manda el navegador: sin carpetas, sin símbolos raros, de largo acotado."""
    base = os.path.basename((name or "video").replace("\\", "/")).replace(" ", "_")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = re.sub(r"\.{2,}", ".", base).strip("._-") or "video"
    return base[-MAX_LABEL_CHARS:]                          # se conserva el final para no perder la extensión


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
    return out, None


def content_security_policy(nonce):
    """Política del navegador: solo el script de la página (con su clave de un solo uso), nada de terceros."""
    return "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "connect-src 'self'",
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
    h["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    # Las fotos pueden guardarse un rato en el navegador de su dueño; todo lo demás, nunca.
    h["Cache-Control"] = "private, max-age=600" if request.url.path.startswith("/evidence/") else "no-store"
    if auth.cookie_is_secure(request):
        h["Strict-Transport-Security"] = "max-age=31536000"
    if h.get("content-type", "").startswith("text/html"):
        h["Content-Security-Policy"] = content_security_policy(request.state.csp_nonce)
    else:
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"


# ---------------------- videos subidos (temporales) ----------------------# ---------------------- videos subidos (temporales) ----------------------

def clean_leftovers():
    """Si el servidor se cayó en pleno análisis, quedan videos a medias: se borran al arrancar."""
    borrados = 0
    for name in os.listdir(UPLOAD_DIR):
        if name.startswith(TMP_PREFIX):
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
                borrados += 1
            except OSError:
                pass
    return borrados


def upload_too_big(request: Request) -> bool:
    """Rechaza una subida enorme ANTES de recibirla, según la cabecera Content-Length."""
    try:
        declarado = int(request.headers.get("content-length", "0"))
    except ValueError:
        return False
    return declarado > (MAX_UPLOAD_MB + 1) * 1024 * 1024          # +1 MB de margen de la cabecera


# ---------------------- eventos en PostgreSQL ----------------------

# Texto del motor de amenaza -> código del catálogo `signals` de la base.
REASON_CODES = [
    ("persona detectada", "person_present"),
    ("en zona restringida", "in_restricted_zone"),
    ("horario sensible", "sensitive_hour"),
    ("merodeo", "loitering"),
    ("grupo de", "group"),
    ("trepando", "climbing"),
    ("persona en el suelo", "fallen"),
    ("posible asalto", "hands_up"),
]
DETAIL_CODES = {"loitering", "group"}          # estos llevan detalle ("merodeo 18s", "grupo de 3")


def parse_reasons(reasons):
    """'persona detectada, merodeo 18s' -> [('person_present', None), ('loitering', 'merodeo 18s')]"""
    if isinstance(reasons, str):
        items = [r.strip() for r in reasons.split(", ")]
    else:
        items = [str(r).strip() for r in (reasons or [])]
    out = []
    for item in items:
        low = item.lower()
        for prefix, code in REASON_CODES:
            if low.startswith(prefix):
                out.append((code, item if code in DETAIL_CODES else None))
                break
    return out


def insert_event(e, user=None, community_id=None, source_kind="browser", camera_id=None, notify=True):
    """
    Guarda un evento con sus motivos (event_signals) y su imagen (media_files). Si viene `user`,
    el evento es de ese usuario y de su comunidad; si no (mini PC), solo de la comunidad.
    Si es MEDIO o ALTO y `notify`, deja en la cola los avisos por Telegram de quien corresponda.
    """
    owner = user["id"] if user else None
    community = user["community_id"] if user else community_id
    image = os.path.basename(e["image"]) if e.get("image") else None
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO events (community_id, user_id, camera_id, source, level, score,
                                   people_count, status, video_time_s, occurred_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) RETURNING id""",
            (community, owner, camera_id, source_kind, e.get("level"), int(e.get("score") or 0),
             int(e.get("people") or 0), e.get("status", "sin verificar"), e.get("video_time_s")))
        event_id = cur.fetchone()["id"]
        for code, detail in parse_reasons(e.get("reasons")):
            cur.execute("""INSERT INTO event_signals (event_id, signal_code, detail)
                           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (event_id, code, detail))
        if image:
            try:
                size = os.path.getsize(os.path.join(EVIDENCE_DIR, image))
            except OSError:
                size = 0
            cur.execute(
                """INSERT INTO media_files (community_id, user_id, camera_id, event_id, kind,
                                            storage_key, size_bytes)
                   VALUES (%s,%s,%s,%s,'image',%s,%s) ON CONFLICT (storage_key) DO NOTHING""",
                (community, owner, camera_id, event_id, image, size))
        if camera_id:
            cur.execute("UPDATE cameras SET last_seen_at = now() WHERE id = %s", (camera_id,))
        if notify:
            alerts.enqueue(cur, event_id, community, camera_id, owner, e.get("level"))
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
           e.people_count AS people, e.status,
           c.name AS camera_name,
           COALESCE(cu.full_name, eu.full_name) AS owner_name
    FROM events e
    LEFT JOIN cameras c ON c.id = e.camera_id
    LEFT JOIN users cu ON cu.id = c.user_id
    LEFT JOIN users eu ON eu.id = e.user_id
    WHERE """


def list_events(user, limit=100, camera_id=None, level=None, desde=None, hasta=None):
    """
    Residente: los eventos suyos o de sus cámaras. Administrador: los de su comunidad, salvo los de cámaras
    que su dueño le oculta. Opcional: una sola cámara, un nivel o un rango de fechas (hora de Lima).
    """
    limit = max(1, min(int(limit), 500))
    if user["role"] == "community_admin":
        condiciones = ["e.community_id = %s AND (c.id IS NULL OR c.admin_can_view OR c.user_id = %s)"]
        args = [user["community_id"], user["id"]]
    else:
        condiciones = ["(e.user_id = %s OR c.user_id = %s)"]
        args = [user["id"], user["id"]]
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


def dispatch_alerts(events, source):
    """
    Sirena de un video analizado: una orden MQTT por evento (WARN para MEDIO, ON para ALTO), para verla en Wokwi.
    Se ejecuta en un hilo (tiene pausas). El aviso por Telegram sale por la cola de alertas, no por aquí.
    """
    d = get_dispatcher()
    if not d["enabled"]:
        log.info("Despacho de alertas desactivado (analysis.dispatch_alerts=false).")
        return
    if not events:
        log.info("Video %s: sin eventos MEDIO/ALTO, no se activa ninguna alerta.", source)
        return
    log.warning("Video %s: despachando %d alerta(s)...", source, len(events))
    for e in events:
        log.warning("Evento en t=%ss | nivel %s | puntaje %s | %s",
                    e["video_time_s"], e["level"], e["score"], e["reasons"])
        if e["level"] == "ALTO":
            d["actuator"].trigger(d["siren_s"])
        elif e["level"] == "MEDIO":
            d["actuator"].warn()
        time.sleep(0.5)  # separa los mensajes para verlos uno a uno en Wokwi


LEVEL_COOLDOWN = {"MEDIO": 45, "ALTO": 20}   # segundos entre órdenes a la sirena, por cámara
_last_alert = {}


def dispatch_live_alert(event, camera_id=None):
    """Sirena de un evento en vivo, con espera por cámara y nivel para no saturar."""
    d = get_dispatcher()
    if not d["enabled"]:
        return
    now = time.time()
    level = event["level"]
    clave = (camera_id, level)
    if now - _last_alert.get(clave, 0) < LEVEL_COOLDOWN.get(level, 30):
        return
    _last_alert[clave] = now
    log.warning("EN VIVO | nivel %s | puntaje %s | %s", level, event["score"], event["reasons"])
    if level == "ALTO":
        d["actuator"].trigger(d["siren_s"])
    elif level == "MEDIO":
        d["actuator"].warn()


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
    pagina = request.url.path == "/" or request.url.path.startswith(("/cameras", "/alerts"))
    quiere_html = request.method == "GET" and pagina and "text/html" in request.headers.get("accept", "")
    if quiere_html:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse(status_code=401, content={"error": "No has iniciado sesión."})


@app.exception_handler(auth.Forbidden)
async def _sin_permiso(request: Request, exc):
    return JSONResponse(status_code=403, content={"error": "No tienes permiso para esto."})


# ---------------------- inicio y cierre de sesión ----------------------

def render_login(message=None, status_code=200, tipo="error", identifier=""):
    with open(os.path.join(TEMPLATES_DIR, "login.html"), encoding="utf-8") as fh:
        template = fh.read()
    bloque = ""
    if message:
        etiqueta = "Aviso" if tipo == "alerta" else "Error"
        bloque = (f'<p class="estado" data-tipo="{tipo}" role="alert"><span class="estado__tag">{etiqueta}</span>'
                  f'<span>{html.escape(message)}</span></p>')
    # Lo que escribió el usuario se devuelve escapado y sin caracteres de control.
    visible = "".join(ch for ch in (identifier or "") if ch.isprintable())[:auth.MAX_IDENTIFIER_CHARS]
    return HTMLResponse(fill(template, error=bloque, identifier=html.escape(visible, quote=True)),
                        status_code=status_code, headers={"Cache-Control": "no-store"})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    try:
        ya_inicio = auth.user_from_token(request.cookies.get(auth.SESSION_COOKIE))
    except psycopg2.OperationalError:
        ya_inicio = None        # sin base no se puede comprobar; igual se muestra el formulario
    if ya_inicio:
        return RedirectResponse("/", status_code=303)
    return render_login()


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
    token = auth.create_session(user["id"], ip, request.headers.get("user-agent"))
    log.info("Inicio de sesión de %s desde %s.", user["id"], ip)
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, request, token)
    return response


@app.post("/logout")
def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------------------- API ----------------------

@app.middleware("http")
async def limitar_subidas(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/analyze":
        # Se decide ANTES de recibir el video: sin sesión no se acepta ni un byte, y si es enorme tampoco.
        try:
            user = await asyncio.to_thread(auth.user_from_token, request.cookies.get(auth.SESSION_COOKIE))
        except (psycopg2.OperationalError, psycopg2.pool.PoolError):
            return JSONResponse(status_code=503,
                                content={"error": "El servicio no está disponible. Inténtalo en unos minutos."})
        if user is None:
            return JSONResponse(status_code=401, content={"error": "No has iniciado sesión."})
        if upload_too_big(request):
            return JSONResponse(status_code=413,
                                content={"error": f"El archivo supera el máximo de {MAX_UPLOAD_MB} MB."})
    return await call_next(request)


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
    restos = clean_leftovers()
    if restos:
        log.warning("Se borraron %d video(s) que quedaron a medias de un análisis anterior.", restos)
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
    alerts.start(polling=os.environ.get("TELEGRAM_POLLING", "1") != "0")


@app.on_event("shutdown")
def shutdown():
    alerts.stop()


@app.get("/health")
def health():
    try:
        db.check()
        return {"status": "ok", "db": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degradado", "db": "error"})


# ---------------------- CÁMARA EN VIVO ----------------------

def _process_frame(session, data, user, camera_id):
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    out = session.process(frame)
    if out["event"]:
        insert_event(out["event"], user=user, source_kind="browser", camera_id=camera_id)
        dispatch_live_alert(out["event"], camera_id)
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    """El navegador manda frames JPEG (binario); responde JSON con cajas y nivel."""
    await ws.accept()
    if not auth.origin_allowed(ws):
        await ws.close(code=4403)           # otro sitio intentó abrir la cámara
        return
    try:
        user = await asyncio.to_thread(auth.user_from_websocket, ws)
    except (psycopg2.OperationalError, psycopg2.pool.PoolError):
        await ws.close(code=1013)           # base no disponible: intenta más tarde
        return
    if user is None:
        await ws.close(code=4401)           # sin sesión o sesión vencida
        return
    try:
        camera = await asyncio.to_thread(cameras.resolve_camera, user, ws.query_params.get("camera"))
    except (psycopg2.OperationalError, psycopg2.pool.PoolError):
        await ws.close(code=1013)
        return
    if camera is None:
        await ws.close(code=4404)           # esa cámara no existe o no es del usuario
        return
    await asyncio.to_thread(cameras.touch_camera, camera["id"])
    engine = await asyncio.to_thread(get_engine)
    session = engine.new_session(EVIDENCE_DIR)
    log.info("Cámara en vivo conectada (usuario %s, cámara %s).", user["id"], camera["id"])
    try:
        while True:
            data = await ws.receive_bytes()
            # La inferencia es bloqueante: se ejecuta en un hilo para no frenar el servidor.
            out = await asyncio.to_thread(_process_frame, session, data, user, camera["id"])
            if out is not None:
                await ws.send_json(out)
    except WebSocketDisconnect:
        log.info("Cámara en vivo desconectada.")


@app.post("/analyze")
async def analyze(video: UploadFile = File(...), camera: str = Form(""), user: dict = Depends(auth.current_user)):
    """
    Analiza un video y guarda los EVENTOS encontrados. El video no se conserva: se copia a un
    archivo temporal con nombre único (el motor lo abre por ruta), se analiza, y se borra siempre.
    """
    cam = await asyncio.to_thread(cameras.resolve_camera, user, camera)
    if cam is None:
        return JSONResponse(status_code=404, content={"error": "Esa cámara no existe o no es tuya."})
    label = clean_filename(video.filename)
    extension = os.path.splitext(label)[1].lower()
    if extension not in VIDEO_EXTENSIONS:
        return JSONResponse(status_code=415, content={"error": "Formato no permitido. Usa MP4, MOV, AVI, MKV o WEBM."})
    fd, tmp_path = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=extension, dir=UPLOAD_DIR)
    try:
        recibidos = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                trozo = await video.read(1024 * 1024)
                if not trozo:
                    break
                recibidos += len(trozo)
                if recibidos > MAX_UPLOAD_MB * 1024 * 1024:
                    return JSONResponse(status_code=413,
                                        content={"error": f"El archivo supera el máximo de {MAX_UPLOAD_MB} MB."})
                out.write(trozo)
        try:
            events = get_engine().analyze_video(tmp_path, EVIDENCE_DIR, source_label=label)
        except Exception:
            log.exception("Falló el análisis del video %s", label)       # el detalle queda en el registro, no en la respuesta
            return JSONResponse(status_code=500, content={"error": "No se pudo analizar el video. Prueba con otro archivo."})
        peor = max(events, key=lambda ev: ev["score"]) if events else None
        for e in events:       # un video avisa solo por su evento más grave, para no llenar el chat
            insert_event(e, user=user, source_kind="video", camera_id=cam["id"], notify=e is peor)
        await asyncio.to_thread(dispatch_alerts, events, label)
        return {"archivo": label, "eventos_detectados": len(events), "eventos": events}
    finally:
        try:
            os.remove(tmp_path)          # el video nunca se queda en el servidor
        except OSError:
            pass


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe un evento de una mini PC. Se identifica con 'Authorization: Bearer <token>'."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    edge = await asyncio.to_thread(auth.edge_from_token, token)
    if edge is None:
        return JSONResponse(status_code=401, content={"error": "Token de la mini PC inválido."})
    try:
        if int(request.headers.get("content-length", "0")) > 100_000:
            return JSONResponse(status_code=413, content={"error": "El evento es demasiado grande."})
    except ValueError:
        pass
    try:
        e = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "El cuerpo no es un JSON válido."})
    limpio, motivo = validate_event(e)
    if motivo:
        return JSONResponse(status_code=422, content={"error": motivo})
    camera_id = None
    if limpio.get("camera_id"):
        camara = await asyncio.to_thread(cameras.edge_camera, edge, limpio["camera_id"])
        if camara is None:
            return JSONResponse(status_code=422, content={"error": "Esa cámara no está asignada a este equipo."})
        camera_id = camara["id"]
    await asyncio.to_thread(insert_event, limpio, None, edge["community_id"], "edge", camera_id)
    return {"ok": True}


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
        if user["role"] == "community_admin":
            cur.execute(
                """SELECT m.user_id FROM media_files m LEFT JOIN cameras c ON c.id = m.camera_id
                   WHERE m.storage_key = %s AND m.community_id = %s
                     AND (c.id IS NULL OR c.admin_can_view OR c.user_id = %s)""",
                (safe, user["community_id"], user["id"]))
        else:
            cur.execute(
                """SELECT m.user_id FROM media_files m LEFT JOIN cameras c ON c.id = m.camera_id
                   WHERE m.storage_key = %s AND (m.user_id = %s OR c.user_id = %s)""",
                (safe, user["id"], user["id"]))
        row = cur.fetchone()
        path = os.path.join(EVIDENCE_DIR, safe)
        if row is None or not os.path.exists(path):
            return JSONResponse(status_code=404, content={"error": "no encontrado"})
        if user["role"] == "community_admin" and row["user_id"] != user["id"]:
            # Queda registro de que el administrador abrió una imagen de otra persona.
            cur.execute(
                """INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id,
                                          action, entity, entity_id, ip)
                   VALUES ('user', %s, %s, %s, 'view_evidence', 'media_file', %s, %s)""",
                (user["id"], user["community_id"], row["user_id"], safe,
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
def cameras_page(user: dict = Depends(auth.current_user)):
    return HTMLResponse(pages.render_cameras(user, cameras.list_cameras(user)))


@app.post("/cameras", response_class=HTMLResponse)
def cameras_create(name: str = Form(""), place: str = Form(""), user: dict = Depends(auth.current_user)):
    cam, problema = cameras.create_camera(user, name, place)
    if problema:
        return HTMLResponse(pages.render_cameras(user, cameras.list_cameras(user), message=problema,
                                                 values=(name[:200], place[:200])), status_code=400)
    return RedirectResponse(f"/cameras/{cam['id']}", status_code=303)


@app.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_page(camera_id: str, request: Request, nivel: str = "", desde: str = "", hasta: str = "",
                limite: str = "12", user: dict = Depends(auth.current_user)):
    cam = cameras.get_camera(user, camera_id)
    if cam is None:
        return _camera_not_found(user)
    nivel = nivel.upper() if nivel.upper() in cameras.LEVELS else ""
    d_desde, d_hasta = _parse_date(desde), _parse_date(hasta)
    aviso = "Una fecha no es válida y se ignoró." if (desde and not d_desde) or (hasta and not d_hasta) else None
    limite = max(12, min(int(limite), 200)) if limite.isascii() and limite.isdigit() else 12
    rows = list_events(user, limite + 1, camera_id=cam["id"], level=nivel or None, desde=d_desde, hasta=d_hasta)
    if user["role"] == "community_admin" and cam["kind"] == "private" and cam["user_id"] != user["id"]:
        with db.cursor() as cur:        # el administrador abrió la cámara de otra persona: queda registrado
            cur.execute(
                """INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip)
                   VALUES ('user', %s, %s, %s, 'view_camera', 'camera', %s, %s)""",
                (user["id"], user["community_id"], cam["user_id"], cam["id"], auth._clean_ip(auth.client_ip(request))))
    return HTMLResponse(pages.render_camera(
        user, cam, rows[:limite], nivel, desde if d_desde else "", hasta if d_hasta else "", limite,
        len(rows) > limite, aviso))


@app.post("/cameras/{camera_id}/visibilidad")
def camera_visibility(camera_id: str, visible: str = Form(""), user: dict = Depends(auth.current_user)):
    if not cameras.set_admin_visibility(user, camera_id, visible == "1"):
        return _camera_not_found(user)
    return RedirectResponse(f"/cameras/{camera_id}", status_code=303)


# ---------------------- alertas por Telegram ----------------------

ALERT_NOTICES = {
    "guardado": ("ok", "Preferencias guardadas."),
    "probado": ("ok", "Te mandamos un mensaje de prueba. Revisa tu Telegram."),
    "desvinculado": ("ok", "Telegram desvinculado. Ya no recibirás alertas."),
    "espera": ("alerta", "Espera unos segundos antes de pedir otra prueba."),
    "fallo": ("error", "Telegram no pudo entregar el mensaje. Si bloqueaste el bot, desbloquéalo y vincula de nuevo."),
    "no_vinculado": ("error", "Primero vincula tu Telegram."),
}


def _alerts_response(user, notice=None, link=None, status=200):
    return HTMLResponse(pages.render_alerts(user, alerts.get_channel(user["id"]), alerts.available(),
                                            alerts.recent_deliveries(user["id"]), notice=notice, link=link),
                        status_code=status)


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(m: str = "", user: dict = Depends(auth.current_user)):
    return _alerts_response(user, notice=ALERT_NOTICES.get(m))


@app.post("/alerts/link", response_class=HTMLResponse)
def alerts_link(user: dict = Depends(auth.current_user)):
    link, problema = alerts.create_link(user)
    if problema:
        return _alerts_response(user, notice=("error", problema), status=429 if "demasiados" in problema else 503)
    return _alerts_response(user, link=link)


@app.post("/alerts/unlink")
def alerts_unlink(user: dict = Depends(auth.current_user)):
    alerts.unlink(user["id"])
    return RedirectResponse("/alerts?m=desvinculado", status_code=303)


@app.post("/alerts/settings")
def alerts_settings(min_level: str = Form("MEDIO"), active: str = Form(""), user: dict = Depends(auth.current_user)):
    if min_level not in alerts.LEVELS:
        return _alerts_response(user, notice=("error", "Nivel no válido."), status=400)
    if not alerts.set_preferences(user["id"], min_level, active == "1"):
        return _alerts_response(user, notice=ALERT_NOTICES["no_vinculado"], status=400)
    return RedirectResponse("/alerts?m=guardado", status_code=303)


@app.post("/alerts/test")
def alerts_test(user: dict = Depends(auth.current_user)):
    ok, motivo = alerts.send_test(user)
    return RedirectResponse("/alerts?m=" + ("probado" if ok else motivo), status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: dict = Depends(auth.current_user)):
    rows = list_events(user, 50)
    cards = ""
    for r in rows:
        partes = [r["camera_name"]] if r["camera_name"] else []
        if user["role"] == "community_admin" and r["owner_name"]:
            partes.append("de " + r["owner_name"])
        extra = f'<div>{html.escape(" · ".join(partes))}</div>' if partes else ""
        color = {"ALTO": "#e74c3c", "MEDIO": "#f39c12", "BAJO": "#2ecc71"}.get(r["level"], "#888")
        img = (f'<img src="/evidence/{html.escape(r["image"])}" style="width:100%;border-radius:8px">'
               if r["image"] else "")
        vt = f'{r["video_time_s"]:g}s' if r["video_time_s"] is not None else "en vivo"
        origen = SOURCE_LABELS.get(r["source"], r["source"])
        cards += f"""
        <div style="border:1px solid #ddd;border-radius:10px;padding:10px;background:#fff">
          {img}
          <div style="margin-top:8px">
            <span style="background:{color};color:#fff;padding:2px 8px;border-radius:6px;font-weight:bold">{html.escape(r["level"])}</span>
            <span style="color:#666"> puntaje {r["score"]}</span>
          </div>
          <div style="font-size:14px;margin-top:6px">{html.escape(r["reasons"] or "")}</div>
          <div style="font-size:12px;color:#888;margin-top:6px">{html.escape(origen)} · {vt} · {r["created_at"]}</div>
          {extra}
        </div>"""
    if not cards:
        cards = '<p style="color:#888">Aun no hay eventos. Inicia la cámara para monitorear.</p>'
    with open(os.path.join(TEMPLATES_DIR, "index.html"), encoding="utf-8") as fh:
        template = fh.read()
    rol = "Administrador" if user["role"] == "community_admin" else "Residente"
    propias = cameras.usable_cameras(user)
    if propias:
        opciones = "".join(f'<option value="{pages.esc(c["id"])}">{pages.esc(c["name"])}</option>' for c in propias)
    else:
        opciones = f'<option value="">Se creará «Cámara de {pages.esc(user["full_name"])}» automáticamente</option>'
    return HTMLResponse(
        fill(template, usuario=f'{html.escape(user["full_name"])} · {rol}', cards=cards,
             nonce=request.state.csp_nonce, camara_opciones=opciones),
        headers={"Cache-Control": "no-store"})
