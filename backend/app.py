import os
import re
import sys
import asyncio
import time
import tempfile
import logging
import datetime
import html

import cv2
import numpy as np
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

# Permite importar los módulos del proyecto (engine, etc.) desde la carpeta padre.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "backend", "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
EVIDENCE_DIR = os.path.join(BASE, "events")  # compartido con el detector
CONFIG_PATH = os.path.join(BASE, "config.yaml")
TEMPLATES_DIR = os.path.join(BASE, "backend", "templates")
COMMUNITY_SLUG = os.environ.get("COMMUNITY_SLUG", "").strip()
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))   # igual que el límite de la página
TMP_PREFIX = "vigia_upload_"                                    # nombre de los videos en análisis

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backend")

app = FastAPI(title="GUARDIAN Backend")
_engine = None       # se crea en el primer análisis (carga modelos; puede tardar)
_dispatcher = None   # actuador (MQTT) + notificador (Telegram), creado una vez
_community_id = None


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


# ---------------------- videos subidos (temporales) ----------------------

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


# ---------------------- comunidad ----------------------

class CommunityNotConfigured(Exception):
    """COMMUNITY_SLUG falta o la comunidad no existe en la base."""


def get_community_id():
    """Id de la comunidad donde se guardan los eventos (se busca una vez y se recuerda)."""
    global _community_id
    if _community_id is None:
        if not COMMUNITY_SLUG:
            raise CommunityNotConfigured("Falta la variable de entorno COMMUNITY_SLUG.")
        with db.cursor() as cur:
            cur.execute("SELECT id FROM communities WHERE slug = %s", (COMMUNITY_SLUG,))
            row = cur.fetchone()
        if row is None:
            raise CommunityNotConfigured(
                f"No existe la comunidad «{COMMUNITY_SLUG}». Créala con: python -m backend.manage "
                f"create-community --name ... --slug {COMMUNITY_SLUG} --district-ubigeo ... --district-name ...")
        _community_id = row["id"]
    return _community_id


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
SOURCE_LABELS = {"browser": "Cámara web", "video": "Video", "edge": "Mini PC"}


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


def insert_event(e, source_kind="browser", community_id=None, camera_id=None):
    """Guarda un evento con sus motivos (event_signals) y su imagen (media_files)."""
    community = community_id or get_community_id()
    image = os.path.basename(e["image"]) if e.get("image") else None
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO events (community_id, camera_id, source, level, score, people_count,
                                   status, video_time_s, occurred_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now()) RETURNING id""",
            (community, camera_id, source_kind, e.get("level"), int(e.get("score") or 0),
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
                """INSERT INTO media_files (community_id, camera_id, event_id, kind, storage_key, size_bytes)
                   VALUES (%s,%s,%s,'image',%s,%s) ON CONFLICT (storage_key) DO NOTHING""",
                (community, camera_id, event_id, image, size))
    return event_id


def list_events(limit=100):
    """Los eventos más recientes de la comunidad, con motivos e imagen."""
    limit = max(1, min(int(limit), 500))
    with db.cursor() as cur:
        cur.execute(
            """SELECT e.id,
                      to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD"T"HH24:MI:SS') AS created_at,
                      e.source, e.video_time_s, e.level, e.score,
                      COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code)
                                FROM event_signals es JOIN signals s ON s.code = es.signal_code
                                WHERE es.event_id = e.id), '') AS reasons,
                      (SELECT m.storage_key FROM media_files m
                        WHERE m.event_id = e.id AND m.kind = 'image'
                        ORDER BY m.created_at LIMIT 1) AS image,
                      e.people_count AS people, e.status
               FROM events e
               WHERE e.community_id = %s
               ORDER BY e.id DESC LIMIT %s""", (get_community_id(), limit))
        rows = cur.fetchall()
    for r in rows:
        if r["video_time_s"] is not None:
            r["video_time_s"] = float(r["video_time_s"])
    return rows


# ---------------------- alertas (actuador + Telegram) ----------------------

def get_dispatcher():
    """Crea una sola vez el actuador (MQTT/simulado) y el notificador (Telegram)."""
    global _dispatcher
    if _dispatcher is None:
        from actuator import build_actuator
        from notifier import Notifier
        cfg = load_cfg()
        n = cfg.get("notifier", {})
        _dispatcher = {
            "actuator": build_actuator(cfg),
            "notifier": Notifier(EVIDENCE_DIR, n.get("telegram_token"), n.get("telegram_chat_id")),
            "siren_s": cfg.get("actuator", {}).get("siren_seconds", 10),
            "zone": cfg.get("zone_name", "Cuadra 01"),
            "enabled": cfg.get("analysis", {}).get("dispatch_alerts", True),
        }
    return _dispatcher


def dispatch_alerts(events, source):
    """
    Envía las alertas de un video analizado:
      - MQTT: una orden por evento (WARN para MEDIO, ON para ALTO), para verla en Wokwi.
      - Telegram: solo la del evento más grave del video (evita llenar el chat).
    """
    d = get_dispatcher()
    if not d["enabled"]:
        log.info("Despacho de alertas desactivado (analysis.dispatch_alerts=false).")
        return
    if not events:
        log.info("Video %s: sin eventos MEDIO/ALTO, no se envía ninguna alerta.", source)
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
    worst = max(events, key=lambda e: e["score"])
    meta = {"zone": d["zone"], "level": worst["level"], "score": worst["score"],
            "reasons": worst["reasons"].split(", ") if worst["reasons"] else [],
            "time": datetime.datetime.now().strftime("%H:%M:%S")}
    d["notifier"].send_photo(os.path.join(EVIDENCE_DIR, worst["image"]), meta)


LEVEL_COOLDOWN = {"MEDIO": 45, "ALTO": 20}   # segundos entre alertas externas (MQTT/Telegram)
_last_alert = {}


def dispatch_live_alert(event):
    """Alerta externa de un evento en vivo (con cooldown por nivel para no saturar)."""
    d = get_dispatcher()
    if not d["enabled"]:
        return
    now = time.time()
    level = event["level"]
    if now - _last_alert.get(level, 0) < LEVEL_COOLDOWN.get(level, 30):
        return
    _last_alert[level] = now
    log.warning("EN VIVO | nivel %s | puntaje %s | %s", level, event["score"], event["reasons"])
    if level == "ALTO":
        d["actuator"].trigger(d["siren_s"])
    elif level == "MEDIO":
        d["actuator"].warn()
    meta = {"zone": d["zone"], "level": level, "score": event["score"],
            "reasons": event["reasons"].split(", ") if event["reasons"] else [],
            "time": datetime.datetime.now().strftime("%H:%M:%S")}
    d["notifier"].send_photo(os.path.join(EVIDENCE_DIR, event["image"]), meta)


# ---------------------- errores ----------------------

@app.exception_handler(psycopg2.OperationalError)
@app.exception_handler(psycopg2.pool.PoolError)
async def _base_no_disponible(request: Request, exc):
    # La base está apagada o reiniciándose: se avisa con claridad y la app se recupera sola.
    log.error("Base de datos no disponible: %s", exc)
    return JSONResponse(status_code=503,
                        content={"error": "El servicio no está disponible. Inténtalo en unos minutos."})


@app.exception_handler(CommunityNotConfigured)
async def _comunidad_no_configurada(request: Request, exc):
    log.error("%s", exc)
    return JSONResponse(status_code=503, content={"error": str(exc)})


# ---------------------- API ----------------------

@app.middleware("http")
async def limitar_subidas(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/analyze" and upload_too_big(request):
        return JSONResponse(status_code=413,
                            content={"error": f"El archivo supera el máximo de {MAX_UPLOAD_MB} MB."})
    return await call_next(request)


@app.on_event("startup")
def startup():
    log.warning("Backend GUARDIAN iniciado. Preparando actuador y notificador...")
    restos = clean_leftovers()
    if restos:
        log.warning("Se borraron %d video(s) que quedaron a medias de un análisis anterior.", restos)
    db.dsn()   # si falta DATABASE_URL, el arranque se detiene con un mensaje claro
    try:
        db.check()
        get_community_id()
        log.warning("Conexión a PostgreSQL correcta (comunidad «%s»).", COMMUNITY_SLUG)
    except (psycopg2.OperationalError, CommunityNotConfigured) as ex:
        # La base puede arrancar después: se reintenta en cada petición.
        log.error("Todavía no se puede usar la base de datos: %s", ex)
    get_dispatcher()


@app.get("/health")
def health():
    try:
        db.check()
        return {"status": "ok", "db": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degradado", "db": "error"})


# ---------------------- CÁMARA EN VIVO ----------------------

def _process_frame(session, data):
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    out = session.process(frame)
    if out["event"]:
        insert_event(out["event"], source_kind="browser")
        dispatch_live_alert(out["event"])
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    """El navegador manda frames JPEG (binario); responde JSON con cajas y nivel."""
    await ws.accept()
    try:
        await asyncio.to_thread(get_community_id)
    except (CommunityNotConfigured, psycopg2.OperationalError) as ex:
        log.error("Cámara en vivo rechazada: %s", ex)
        await ws.close(code=1013)           # intenta más tarde
        return
    engine = await asyncio.to_thread(get_engine)
    session = engine.new_session(EVIDENCE_DIR)
    log.info("Cámara en vivo conectada.")
    try:
        while True:
            data = await ws.receive_bytes()
            # La inferencia es bloqueante: se ejecuta en un hilo para no frenar el servidor.
            out = await asyncio.to_thread(_process_frame, session, data)
            if out is not None:
                await ws.send_json(out)
    except WebSocketDisconnect:
        log.info("Cámara en vivo desconectada.")


@app.post("/analyze")
async def analyze(video: UploadFile = File(...)):
    """
    Analiza un video y guarda los EVENTOS encontrados. El video no se conserva: se copia a un
    archivo temporal con nombre único (el motor lo abre por ruta), se analiza, y se borra siempre.
    """
    label = os.path.basename(video.filename or "video").replace(" ", "_")
    extension = re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(label)[1])[:8] or ".mp4"
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
        except Exception as ex:
            return JSONResponse(status_code=500, content={"error": str(ex)})
        for e in events:
            insert_event(e, source_kind="video")
        dispatch_alerts(events, label)
        return {"archivo": label, "eventos_detectados": len(events), "eventos": events}
    finally:
        try:
            os.remove(tmp_path)          # el video nunca se queda en el servidor
        except OSError:
            pass


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe un evento de otro equipo (por ejemplo, el detector) y lo guarda."""
    e = await request.json()
    await asyncio.to_thread(insert_event, {
        "level": e.get("level"),
        "score": e.get("score"),
        "reasons": e.get("reasons"),
        "image": e.get("image"),
        "people": e.get("people"),
    }, "edge")
    return {"ok": True}


@app.get("/events")
def events(limit: int = 100):
    return list_events(limit)


@app.get("/evidence/{name}")
def evidence(name: str):
    path = os.path.join(EVIDENCE_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "no encontrado"})
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
def home():
    rows = list_events(50)
    cards = ""
    for r in rows:
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
        </div>"""
    if not cards:
        cards = '<p style="color:#888">Aun no hay eventos. Inicia la cámara para monitorear.</p>'
    with open(os.path.join(TEMPLATES_DIR, "index.html"), encoding="utf-8") as fh:
        template = fh.read()
    return HTMLResponse(template.replace("{cards}", cards), headers={"Cache-Control": "no-store"})
