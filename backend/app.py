"""
app.py — Backend de GUARDIAN (FastAPI + SQLite).

Qué hace:
  - Analiza la CÁMARA EN VIVO del navegador (WebSocket /ws/live).
  - (Opcional) Sube un VIDEO y lo analiza con el motor de detección (GuardianEngine).
  - Guarda cada evento de amenaza en una base de datos SQLite.
  - Muestra el historial de evidencia en una página web simple.
  - Recibe eventos del detector en vivo (POST /ingest), para unificar todo.

Correr en local:
    pip install -r backend/requirements.txt
    uvicorn backend.app:app --reload --port 8000
    # abre http://localhost:8000
"""
import os
import sys
import asyncio
import json
import time
import sqlite3
import shutil
import logging
import datetime

import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

# Permite importar los módulos del proyecto (engine, etc.) desde la carpeta padre.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "backend", "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
EVIDENCE_DIR = os.path.join(BASE, "events")  # compartido con el detector
DB_PATH = os.path.join(DATA_DIR, "guardian.db")
CONFIG_PATH = os.path.join(BASE, "config.yaml")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backend")

app = FastAPI(title="GUARDIAN Backend")
_engine = None       # se crea en el primer análisis (carga modelos; puede tardar)
_dispatcher = None   # actuador (MQTT) + notificador (Telegram), creado una vez


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT,
                video_time_s REAL,
                level TEXT,
                score INTEGER,
                reasons TEXT,
                image TEXT,
                people INTEGER,
                status TEXT DEFAULT 'sin verificar'
            )
        """)
init_db()


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


def insert_event(e):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO events (created_at, source, video_time_s, level, score,
               reasons, image, people, status) VALUES (?,?,?,?,?,?,?,?,?)""",
            (datetime.datetime.now().isoformat(timespec="seconds"),
             e.get("source"), e.get("video_time_s"), e.get("level"), e.get("score"),
             e.get("reasons"), e.get("image"), e.get("people"),
             e.get("status", "sin verificar")))


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


# ---------------------- API ----------------------

@app.on_event("startup")
def startup():
    log.warning("Backend GUARDIAN iniciado. Preparando actuador y notificador...")
    get_dispatcher()


@app.get("/health")
def health():
    return {"status": "ok"}



# ---------------------- CÁMARA EN VIVO ----------------------

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


def _process_frame(session, data):
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    out = session.process(frame)
    if out["event"]:
        insert_event(out["event"])
        dispatch_live_alert(out["event"])
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    """El navegador manda frames JPEG (binario); responde JSON con cajas y nivel."""
    await ws.accept()
    session = get_engine().new_session(EVIDENCE_DIR)
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
    """Sube un video, lo analiza y guarda los eventos encontrados."""
    safe = os.path.basename(video.filename).replace(" ", "_")
    dst = os.path.join(UPLOAD_DIR, safe)
    with open(dst, "wb") as f:
        shutil.copyfileobj(video.file, f)
    try:
        events = get_engine().analyze_video(dst, EVIDENCE_DIR, source_label=safe)
    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": str(ex)})
    for e in events:
        insert_event(e)
    dispatch_alerts(events, safe)
    return {"archivo": safe, "eventos_detectados": len(events), "eventos": events}


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe un evento del detector en vivo (JSON) y lo guarda."""
    e = await request.json()
    insert_event({
        "source": e.get("zone", "en vivo"),
        "video_time_s": None,
        "level": e.get("level"),
        "score": e.get("score"),
        "reasons": ", ".join(e.get("reasons", [])) if isinstance(e.get("reasons"), list) else e.get("reasons"),
        "image": os.path.basename(e["image"]) if e.get("image") else None,
        "people": e.get("people"),
    })
    return {"ok": True}


@app.get("/events")
def events(limit: int = 100):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/evidence/{name}")
def evidence(name: str):
    path = os.path.join(EVIDENCE_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "no encontrado"})
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
def home():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50").fetchall()
    cards = ""
    for r in rows:
        color = {"ALTO": "#e74c3c", "MEDIO": "#f39c12", "BAJO": "#2ecc71"}.get(r["level"], "#888")
        img = f'<img src="/evidence/{r["image"]}" style="width:100%;border-radius:8px">' if r["image"] else ""
        vt = f'{r["video_time_s"]}s' if r["video_time_s"] is not None else "en vivo"
        cards += f"""
        <div style="border:1px solid #ddd;border-radius:10px;padding:10px;background:#fff">
          {img}
          <div style="margin-top:8px">
            <span style="background:{color};color:#fff;padding:2px 8px;border-radius:6px;font-weight:bold">{r["level"]}</span>
            <span style="color:#666"> puntaje {r["score"]}</span>
          </div>
          <div style="font-size:14px;margin-top:6px">{r["reasons"] or ""}</div>
          <div style="font-size:12px;color:#888;margin-top:6px">{r["source"]} · {vt} · {r["created_at"]}</div>
        </div>"""
    if not cards:
        cards = '<p style="color:#888">Aun no hay eventos. Inicia la cámara para monitorear.</p>'
    tpl_path = os.path.join(BASE, "backend", "templates", "index.html")
    with open(tpl_path, encoding="utf-8") as fh:
        template = fh.read()
    return template.replace("{cards}", cards)
