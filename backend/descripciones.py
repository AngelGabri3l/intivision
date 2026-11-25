"""
Descripción automática con IA de la foto de cada evento.

  Base     una frase corta, solo en eventos ALTO, con tope diario por comunidad
  Premium  2 a 4 frases (personas, ropa en general, objetos, vehículos, dirección), en eventos MEDIO y ALTO

Cómo funciona:
  1. Al guardar un evento con foto, si el plan, el nivel y la cámara lo permiten, queda «pendiente».
  2. Un hilo toma los pendientes, DESENFOCA LOS ROSTROS que encuentra (la descripción no necesita caras), reduce la foto y se la manda
     a Claude (modelo con visión). Las instrucciones piden describir solo lo visible, sin identificar a nadie ni suponer intenciones.
  3. La descripción se guarda en el evento y llega por Telegram como respuesta al aviso con foto, rotulada como automática.
     Si la IA no ve personas, se avisa «podría ser una falsa alarma». NUNCA cambia el nivel ni suprime la alerta.
  4. Si la IA no está configurada o falla, el evento sigue igual, sin descripción.

Variables: ANTHROPIC_API_KEY (obligatoria para activarla) e IA_MODELO (por defecto claude-haiku-5-5).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

import requests

log = logging.getLogger("backend.descripciones")
API = "https://api.anthropic.com/v1/messages"
VERSION_API = "2023-06-01"
MAX_ANCHO = 768
MAX_INTENTOS = 3
NIVELES = {"short": ("ALTO",), "detailed": ("MEDIO", "ALTO")}
LARGO = {"short": 200, "detailed": 600}

SISTEMA = (
    "Describes fotos de cámaras de seguridad de un barrio en Lima para avisar a los vecinos por Telegram. Reglas:\n"
    "- Describe solo lo que se ve. Si algo no es claro, usa «posible». No inventes.\n"
    "- No identifiques a nadie ni adivines nombres, edad exacta, etnia, nacionalidad, religión, salud ni intenciones.\n"
    "- Los rostros pueden estar difuminados a propósito: no lo menciones.\n"
    "- Español neutro y frases cortas.\n"
    'Responde SOLO con JSON, sin texto alrededor: {"descripcion": "...", "personas": <número de personas visibles>}'
)


class ErrorIA(Exception):
    def __init__(self, mensaje, transitorio=True):
        super().__init__(mensaje)
        self.transitorio = transitorio


def configurada() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def modelo() -> str:
    return os.environ.get("IA_MODELO") or "claude-haiku-5-5"


# ------------------------------------------------------------------ cuándo se describe

def marcar_si_corresponde(cur, event_id, camera_id, level, tiene_foto) -> bool:
    """Deja el evento «pendiente» si la IA está configurada y el plan, el nivel y la cámara lo permiten."""
    from backend import planes
    if not (configurada() and camera_id and tiene_foto):
        return False
    detalle = planes.de_camara(cur, camera_id).get("ai_description") or "none"
    if level not in NIVELES.get(detalle, ()):
        return False
    cur.execute("SELECT ai_enabled FROM cameras WHERE id = %s", (camera_id,))
    fila = cur.fetchone()
    if not fila or not fila["ai_enabled"]:
        return False
    cur.execute("UPDATE events SET ai_status = 'pendiente', ai_detail = %s WHERE id = %s", (detalle, event_id))
    return True


# ------------------------------------------------------------------ la imagen que sale del servidor

_DETECTORES = None


def _detectores_de_rostros():
    """Los clasificadores de rostro de frente y de perfil (se cargan una vez). Lista vacía si OpenCV no los trae."""
    global _DETECTORES
    if _DETECTORES is None:
        import cv2
        _DETECTORES = []
        if hasattr(cv2, "CascadeClassifier") and getattr(getattr(cv2, "data", None), "haarcascades", None):
            for nombre in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
                c = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, nombre))
                if not c.empty():
                    _DETECTORES.append(c)
        if not _DETECTORES:
            log.error("OpenCV no trae el detector de rostros: las descripciones con IA quedan en pausa para no enviar caras.")
    return _DETECTORES


def preparar_imagen(datos: bytes) -> tuple[bytes, int]:
    """Reduce la foto a MAX_ANCHO y pixela los rostros que detecta. Devuelve (JPEG, rostros desenfocados)."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ErrorIA("la foto no se puede leer", transitorio=False)
    alto, ancho = img.shape[:2]
    if ancho > MAX_ANCHO:
        img = cv2.resize(img, (MAX_ANCHO, max(2, round(alto * MAX_ANCHO / ancho))), interpolation=cv2.INTER_AREA)
    gris = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    detectores = _detectores_de_rostros()
    if not detectores:
        # Privacidad primero: sin un detector de rostros la foto NO sale del servidor (mejor sin descripción que con caras).
        raise ErrorIA("no hay detector de rostros disponible (se necesita OpenCV 4.x): la foto no se envía", transitorio=False)
    cajas = []
    for clasificador in detectores:
        cajas += list(clasificador.detectMultiScale(gris, scaleFactor=1.1, minNeighbors=4, minSize=(20, 20)))
    alto, ancho = img.shape[:2]
    for (x, y, w, h) in cajas:
        mx, my = int(w * 0.25), int(h * 0.25)
        x0, y0, x1, y1 = max(0, x - mx), max(0, y - my), min(ancho, x + w + mx), min(alto, y + h + my)
        zona = img[y0:y1, x0:x1]
        if zona.size:
            chica = cv2.resize(zona, (max(1, (x1 - x0) // 10), max(1, (y1 - y0) // 10)), interpolation=cv2.INTER_LINEAR)
            img[y0:y1, x0:x1] = cv2.resize(chica, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise ErrorIA("no se pudo preparar la foto", transitorio=False)
    return buf.tobytes(), len(cajas)


def instrucciones(detalle: str, motivos: str) -> str:
    if detalle == "detailed":
        pedido = ("Describe la escena en 2 a 4 frases (máximo 70 palabras): cuántas personas hay y qué hacen, su ropa en términos generales "
                  "(colores), objetos que llevan en las manos, vehículos y hacia dónde se mueven, y si es de día o de noche.")
    else:
        pedido = "Describe la escena en UNA frase de máximo 20 palabras."
    motivos = re.sub(r"[^\w\s,().:-]", "", motivos or "")[:200]
    return pedido + (f" El detector automático marcó: {motivos}. Si eso no se ve en la imagen, dilo." if motivos else "")


def describir(jpeg: bytes, detalle: str, motivos: str) -> dict:
    """Llama a Claude con la foto ya preparada. Devuelve {"descripcion": str, "personas": int}. Lanza ErrorIA."""
    clave = os.environ.get("ANTHROPIC_API_KEY", "")
    if not clave:
        raise ErrorIA("falta ANTHROPIC_API_KEY", transitorio=False)
    cuerpo = {"model": modelo(), "max_tokens": 300, "system": SISTEMA,
              "messages": [{"role": "user", "content": [
                  {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
                  {"type": "text", "text": instrucciones(detalle, motivos)}]}]}
    try:
        r = requests.post(API, json=cuerpo, timeout=(5, 40),
                          headers={"x-api-key": clave, "anthropic-version": VERSION_API, "content-type": "application/json"})
    except requests.RequestException as e:
        raise ErrorIA(f"sin conexión con la API ({e.__class__.__name__})") from None
    if r.status_code in (429, 500, 502, 503, 504, 529):
        raise ErrorIA(f"la API respondió {r.status_code}")
    if r.status_code >= 400:
        raise ErrorIA(f"la API rechazó el pedido ({r.status_code})", transitorio=False)
    texto = "".join(b.get("text", "") for b in (r.json().get("content") or []) if b.get("type") == "text").strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
    try:
        datos = json.loads(texto)
        desc = " ".join(str(datos["descripcion"]).split())
        personas = int(datos.get("personas", 0))
    except (ValueError, KeyError, TypeError):
        raise ErrorIA("la respuesta no tiene el formato esperado", transitorio=False) from None
    if not desc:
        raise ErrorIA("la descripción vino vacía", transitorio=False)
    return {"descripcion": desc[:LARGO.get(detalle, 600)], "personas": max(0, personas)}


# ------------------------------------------------------------------ la cola

_SELECT_PENDIENTE = """
    SELECT e.id, e.community_id, e.camera_id, e.level, e.ai_detail, e.ai_attempts,
           COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code) FROM event_signals es JOIN signals s ON s.code = es.signal_code
                     WHERE es.event_id = e.id), '') AS motivos,
           (SELECT m.storage_key FROM media_files m WHERE m.event_id = e.id AND m.kind = 'image' ORDER BY m.created_at LIMIT 1) AS foto
    FROM events e
    WHERE e.ai_status = 'pendiente' AND e.created_at + make_interval(secs => e.ai_attempts * 30) <= now()
    ORDER BY e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED
"""


def procesar_uno(evidence_dir: str) -> bool:
    """Describe un evento pendiente. Devuelve True si había alguno."""
    from backend import db, planes
    with db.cursor() as cur:
        cur.execute(_SELECT_PENDIENTE)
        ev = cur.fetchone()
        if ev is None:
            return False
        cap = int(planes.de_camara(cur, ev["camera_id"]).get("ai_daily_cap") or 0) if ev["camera_id"] else 0
        cur.execute("""SELECT count(*) AS n FROM events WHERE community_id = %s AND ai_status = 'hecha'
                       AND ai_described_at >= date_trunc('day', now() AT TIME ZONE 'America/Lima') AT TIME ZONE 'America/Lima'""",
                    (ev["community_id"],))
        if cap <= 0 or cur.fetchone()["n"] >= cap or not ev["foto"]:
            cur.execute("UPDATE events SET ai_status = 'omitida' WHERE id = %s", (ev["id"],))
            return True
        try:
            with open(os.path.join(evidence_dir, os.path.basename(ev["foto"])), "rb") as f:
                jpeg, _ = preparar_imagen(f.read())
            res = describir(jpeg, ev["ai_detail"] or "short", ev["motivos"])
        except Exception as e:                             # noqa: BLE001 - cualquier fallo deja el evento sin descripción
            intentos = ev["ai_attempts"] + 1
            definitivo = isinstance(e, OSError) or not getattr(e, "transitorio", True) or intentos >= MAX_INTENTOS
            cur.execute("UPDATE events SET ai_attempts = %s, ai_status = %s WHERE id = %s",
                        (intentos, "error" if definitivo else "pendiente", ev["id"]))
            log.warning("No se pudo describir el evento %s: %s", ev["id"], str(e)[:160])
            return True
        cur.execute("""UPDATE events SET ai_status = 'hecha', ai_description = %s, ai_sees_people = %s, ai_described_at = now(),
                              ai_attempts = ai_attempts + 1 WHERE id = %s""", (res["descripcion"], res["personas"] > 0, ev["id"]))
        cur.execute("""UPDATE alert_deliveries SET desc_status = 'pendiente'
                       WHERE event_id = %s AND status IN ('pending', 'sent') AND desc_status IS NULL""", (ev["id"],))
    return True


def texto_telegram(descripcion: str, ve_personas) -> str:
    texto = f"🤖 Descripción automática (puede equivocarse): {descripcion}"
    if ve_personas is False:
        texto += "\nLa IA no ve personas en la foto: podría ser una falsa alarma."
    return texto


def enviar_uno() -> bool:
    """Manda una descripción pendiente como respuesta al aviso con foto (cuando ese aviso ya salió)."""
    from backend import alerts, db, telegram_api
    with db.cursor() as cur:
        cur.execute("""SELECT d.id, d.status, d.desc_attempts, d.telegram_message_id, ch.chat_id, ch.is_active,
                              e.ai_description, e.ai_sees_people
                       FROM alert_deliveries d JOIN notification_channels ch ON ch.id = d.channel_id JOIN events e ON e.id = d.event_id
                       WHERE d.desc_status = 'pendiente' AND d.status <> 'pending'
                         AND now() >= coalesce(d.sent_at, d.created_at) + make_interval(secs => d.desc_attempts * 20)
                       ORDER BY d.id LIMIT 1 FOR UPDATE OF d SKIP LOCKED""")
        d = cur.fetchone()
        if d is None:
            return False
        if d["status"] != "sent" or not d["is_active"] or not d["ai_description"] or alerts._client is None:
            cur.execute("UPDATE alert_deliveries SET desc_status = 'omitido' WHERE id = %s", (d["id"],))
            return True
        try:
            alerts._client.send_message(d["chat_id"], texto_telegram(d["ai_description"], d["ai_sees_people"]), reply_to=d["telegram_message_id"])
            estado = "enviado"
        except telegram_api.TelegramError as e:
            estado = "omitido" if e.blocked else ("error" if d["desc_attempts"] + 1 >= MAX_INTENTOS else "pendiente")
        except Exception:                                  # noqa: BLE001
            estado = "error" if d["desc_attempts"] + 1 >= MAX_INTENTOS else "pendiente"
        cur.execute("UPDATE alert_deliveries SET desc_status = %s, desc_attempts = desc_attempts + 1 WHERE id = %s", (estado, d["id"]))
    return True


def ciclo_forever(stop, evidence_dir: str):
    while not stop.is_set():
        try:
            while procesar_uno(evidence_dir):
                pass
            while enviar_uno():
                pass
        except Exception:                                  # noqa: BLE001
            log.exception("Fallo en la cola de descripciones")
            stop.wait(5)
        stop.wait(2)
