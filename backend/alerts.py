"""
Alertas por Telegram (VIGIA-14).

  - Vinculación: cada usuario genera un enlace de un solo uso; al abrirlo en Telegram, el bot recibe
    «/start <código>» y guarda el chat de esa persona.
  - Destinatarios: el dueño de una cámara privada; los administradores, si es una zona común.
  - Cola: cada aviso es una fila en alert_deliveries; un hilo aparte los envía, con reintentos.

El servidor nunca espera a Telegram dentro de una petición.
"""
import hashlib
import logging
import os
import re
import secrets
import threading
import time

import psycopg2.errors

from backend import camera_watch, db, hitl, telegram_api

log = logging.getLogger("alerts")

CODE_MINUTES = 10
MAX_ATTEMPTS = 6
STALE_MINUTES = 10                                   # un aviso que no salió en este tiempo ya no sirve: caduca
LEVELS = ("MEDIO", "ALTO")
COOLDOWN = {"ALTO": int(os.environ.get("ALERT_COOLDOWN_ALTO_S", "20")),
            "MEDIO": int(os.environ.get("ALERT_COOLDOWN_MEDIO_S", "45"))}   # segundos entre avisos por cámara y canal
CONFLICT_WAIT, ERROR_WAIT = 30, 5                     # esperas del sondeo cuando algo falla
TEST_EVERY_S = 30                                    # un mensaje de prueba cada tanto, por usuario
LINKS_PER_WINDOW = (5, 600)                          # 5 enlaces nuevos cada 10 minutos, por usuario
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_cfg = {"token": None, "evidence_dir": "events", "base_url": ""}
_client = None
_bot_username = None
_recent = {"test": {}, "link": {}}
_recent_lock = threading.Lock()
_threads = []
_stop = threading.Event()
_siren = None                                        # función que hace sonar la sirena (la pone app.py)


# ------------------------------------------------------------------ configuración

def configure(token, evidence_dir, base_url=""):
    global _client, _bot_username
    _cfg.update(token=(token or "").strip() or None, evidence_dir=evidence_dir, base_url=(base_url or "").strip().rstrip("/"))
    _client = telegram_api.Telegram(_cfg["token"]) if _cfg["token"] else None
    _bot_username = None


def available() -> bool:
    return _client is not None


def bot_username():
    """El nombre del bot (para armar el enlace). Se consulta una vez y se recuerda."""
    global _bot_username
    if _bot_username is None and _client is not None:
        try:
            _bot_username = (_client.get_me() or {}).get("username")
        except telegram_api.TelegramError as ex:
            log.warning("No se pudo consultar el bot de Telegram: %s", ex.description)
    return _bot_username


def _allow(kind, key, every=None, window=None, limit=None):
    """Límite de frecuencia sencillo, en memoria. every: una vez cada N s; window/limit: N veces en una ventana."""
    now = time.monotonic()
    with _recent_lock:
        marcas = [t for t in _recent[kind].get(key, []) if now - t < (window or every)]
        if (every and marcas) or (limit and len(marcas) >= limit):
            _recent[kind][key] = marcas
            return False
        marcas.append(now)
        _recent[kind][key] = marcas
        return True


def reset_limits():
    with _recent_lock:
        _recent["test"].clear()
        _recent["link"].clear()


# ------------------------------------------------------------------ canal de cada usuario

def get_channel(user_id):
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id, chat_id, min_level, is_active, verified_at,
                              (link_code_hash IS NOT NULL AND link_expires_at > now()) AS link_pending,
                              GREATEST(0, CEIL(EXTRACT(EPOCH FROM (link_expires_at - now())) / 60))::int AS link_minutes
                       FROM notification_channels WHERE user_id = %s AND kind = 'telegram'
                       ORDER BY created_at LIMIT 1""", (str(user_id),))
        row = cur.fetchone()
    return dict(row) if row else None


def create_link(user):
    """(enlace, None) o (None, motivo). El código solo se ve ahora: en la base queda su hash."""
    if not available():
        return None, "Telegram no está configurado en este servidor."
    if not _allow("link", str(user["id"]), window=LINKS_PER_WINDOW[1], limit=LINKS_PER_WINDOW[0]):
        return None, "Pediste demasiados enlaces. Espera unos minutos."
    usuario_bot = bot_username()
    if not usuario_bot:
        return None, "No se pudo contactar a Telegram. Inténtalo en unos minutos."
    code = secrets.token_urlsafe(16)
    digest = hashlib.sha256(code.encode()).hexdigest()
    with db.cursor() as cur:
        cur.execute("""UPDATE notification_channels
                       SET link_code_hash = %s, link_expires_at = now() + make_interval(mins => %s)
                       WHERE user_id = %s AND kind = 'telegram' RETURNING id""", (digest, CODE_MINUTES, str(user["id"])))
        if cur.fetchone() is None:
            cur.execute("""INSERT INTO notification_channels (user_id, kind, link_code_hash, link_expires_at)
                           VALUES (%s, 'telegram', %s, now() + make_interval(mins => %s))""",
                        (str(user["id"]), digest, CODE_MINUTES))
    return f"https://t.me/{usuario_bot}?start={code}", None


def unlink(user_id):
    with db.cursor() as cur:
        cur.execute("DELETE FROM notification_channels WHERE user_id = %s AND kind = 'telegram'", (str(user_id),))


def set_preferences(user_id, min_level, active):
    if min_level not in LEVELS:
        return False
    with db.cursor() as cur:
        cur.execute("""UPDATE notification_channels SET min_level = %s, is_active = %s
                       WHERE user_id = %s AND kind = 'telegram' AND verified_at IS NOT NULL RETURNING id""",
                    (min_level, bool(active), str(user_id)))
        return cur.fetchone() is not None


def send_test(user):
    """(True, None) o (False, motivo). Va directo a Telegram: no pasa por la cola."""
    canal = get_channel(user["id"])
    if not available() or not canal or not canal["verified_at"]:
        return False, "no_vinculado"
    if not _allow("test", str(user["id"]), every=TEST_EVERY_S):
        return False, "espera"
    try:
        _client.send_message(canal["chat_id"], "Mensaje de prueba de VIGÍA: si lo ves, las alertas llegarán a este chat.")
    except telegram_api.TelegramError as ex:
        if ex.blocked:
            with db.cursor() as cur:
                cur.execute("UPDATE notification_channels SET is_active = false WHERE id = %s", (canal["id"],))
        log.warning("Falló el mensaje de prueba: %s", ex.description)
        return False, "fallo"
    return True, None


def recent_deliveries(user_id, limit=10):
    with db.cursor() as cur:
        cur.execute("""SELECT d.status, d.attempts, d.last_error, e.level, c.name AS camera_name,
                              to_char(d.created_at AT TIME ZONE 'America/Lima', 'DD/MM HH24:MI') AS cuando
                       FROM alert_deliveries d
                       JOIN notification_channels ch ON ch.id = d.channel_id
                       JOIN events e ON e.id = d.event_id
                       LEFT JOIN cameras c ON c.id = e.camera_id
                       WHERE ch.user_id = %s ORDER BY d.id DESC LIMIT %s""", (str(user_id), limit))
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ vincular desde Telegram

def link_chat(code, chat_id):
    """'ok' | 'invalid' | 'taken'."""
    if not isinstance(code, str) or not _CODE_RE.match(code):
        return "invalid"
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE notification_channels
                           SET chat_id = %s, verified_at = now(), link_code_hash = NULL, link_expires_at = NULL, is_active = true
                           WHERE link_code_hash = %s AND link_expires_at > now() AND kind = 'telegram'
                           RETURNING id""", (str(chat_id), hashlib.sha256(code.encode()).hexdigest()))
            return "ok" if cur.fetchone() else "invalid"
    except psycopg2.errors.UniqueViolation:
        return "taken"                                           # ese chat ya está vinculado a otra cuenta


def _reply(chat_id, text):
    try:
        _client.send_message(chat_id, text)
    except telegram_api.TelegramError as ex:
        log.warning("No se pudo responder en Telegram: %s", ex.description)


_REPLIES = {
    "ok": "Listo: tu cuenta quedó vinculada. Recibirás aquí las alertas de tus cámaras.",
    "taken": "Este chat ya está vinculado a otra cuenta de VIGÍA.",
    "invalid": "Ese enlace no es válido o ya venció. Genera uno nuevo en VIGÍA, en Alertas.",
}
_HELP = "Este bot solo envía alertas de VIGÍA. Para vincularlo, abre VIGÍA, entra a Alertas y usa Vincular Telegram."


def handle_update(update):
    if update.get("callback_query"):
        handle_callback(update["callback_query"])                # un clic en Confirmar o Descartar (H6)
        return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("id") is None:
        return
    if chat.get("type") != "private":
        _reply(chat["id"], "VIGÍA solo se vincula desde un chat privado con el bot.")
        return
    texto = (msg.get("text") or "").strip()
    if texto.split(maxsplit=1)[:1] == ["/start"]:
        partes = texto.split(maxsplit=1)
        if len(partes) == 1:
            _reply(chat["id"], _HELP)
        else:
            _reply(chat["id"], _REPLIES[link_chat(partes[1].strip(), chat["id"])])
        return
    _reply(chat["id"], _HELP)


def poll_once(offset=None, timeout=25):
    """Una vuelta de sondeo. Devuelve el siguiente offset."""
    for update in _client.get_updates(offset, timeout=timeout):
        offset = update["update_id"] + 1
        try:
            handle_update(update)
        except Exception:
            log.exception("Error procesando un mensaje de Telegram")
    return offset


def poll_forever(stop):
    try:
        _client.delete_webhook()                                 # con un webhook activo, el sondeo no recibe nada
    except telegram_api.TelegramError as ex:
        log.warning("No se pudo quitar el webhook de Telegram: %s", ex.description)
    offset = None
    while not stop.is_set():
        try:
            inicio = time.monotonic()
            nuevo = poll_once(offset)
            if nuevo == offset and time.monotonic() - inicio < 1:
                stop.wait(1)                 # sin novedades y sin haber esperado: una pausa, para no dar vueltas en vacío
            offset = nuevo
        except telegram_api.TelegramError as ex:
            if ex.code == 409:
                log.error("Otro programa está leyendo los mensajes de este mismo bot (¿el mismo token en dos "
                          "lugares?). Usa un bot distinto en cada entorno.")
                stop.wait(CONFLICT_WAIT)
            else:
                log.warning("Sondeo de Telegram: %s", ex.description)
                stop.wait(ERROR_WAIT)
        except Exception:
            log.exception("Fallo inesperado en el sondeo de Telegram")
            stop.wait(ERROR_WAIT)


# ------------------------------------------------------------------ a quién se avisa

_ENQUEUE_HEAD = """
    INSERT INTO alert_deliveries (event_id, channel_id, audience)
    SELECT %(event)s, ch.id, %(audience)s
    FROM notification_channels ch
    JOIN users u ON u.id = ch.user_id AND u.status = 'active'
    WHERE ch.kind = 'telegram' AND ch.is_active AND ch.verified_at IS NOT NULL AND ch.chat_id IS NOT NULL
      AND (ch.min_level = 'MEDIO' OR %(level)s = 'ALTO')
"""
_TO_OWNER = " AND ch.user_id = %(owner)s::uuid "
_TO_ADMINS = " AND u.community_id = %(community)s::uuid AND u.role = 'community_admin' "
_ENQUEUE_TAIL = """
      AND NOT EXISTS (
            SELECT 1 FROM alert_deliveries d2 JOIN events e2 ON e2.id = d2.event_id
            WHERE d2.channel_id = ch.id AND e2.camera_id IS NOT DISTINCT FROM %(camera)s::uuid
              AND e2.level = %(level)s AND d2.created_at > now() - make_interval(secs => %(cooldown)s))
"""


def enqueue(cur, event_id, community_id, camera_id, user_id, level):
    """
    Crea los avisos de un evento, dentro de la misma operación que lo guardó (así no se pierde ninguno).
    Solo avisan los MEDIO y ALTO. Devuelve cuántos avisos se crearon.
    Si la comunidad activó la revisión (H6) y el evento es de una zona común, el aviso va SOLO a los administradores, con botones,
    y el evento queda esperando su respuesta.
    """
    if level not in LEVELS:
        return 0
    owner, a_admins, comun = None, False, False
    if camera_id:
        cur.execute("SELECT kind, user_id::text AS user_id FROM cameras WHERE id = %s", (str(camera_id),))
        cam = cur.fetchone()
        if cam and cam["kind"] == "private" and cam["user_id"]:
            owner = cam["user_id"]
        else:
            a_admins = True                                      # zona común: la ven los administradores
            comun = bool(cam and cam["kind"] == "common")
    elif user_id:
        owner = str(user_id)
    else:
        a_admins = True
    audience = "normal"
    if comun:
        decision = hitl.review_decision(cur, event_id, community_id, camera_id, level)   # H6: ¿lo revisa un administrador primero?
        if decision == "cubierto":
            return 0                                             # ya hay una revisión abierta de esta cámara: no se avisa otra vez
        if decision is not None:
            audience = "review"
            hitl.start_review(cur, event_id, decision)
    params = {"event": event_id, "level": level, "camera": str(camera_id) if camera_id else None,
              "cooldown": COOLDOWN[level], "owner": owner, "community": str(community_id), "audience": audience}
    sql = _ENQUEUE_HEAD + (_TO_ADMINS if a_admins else _TO_OWNER) + _ENQUEUE_TAIL
    cur.execute(sql, params)
    return cur.rowcount


# ------------------------------------------------------------------ envío

_SELECT_DUE = """
    SELECT d.id, d.attempts, d.created_at < now() - make_interval(mins => %s) AS stale,
           ch.id::text AS channel_id, ch.chat_id, ch.is_active,
           d.audience, e.id AS event_id, e.status AS event_status,
           COALESCE(ROUND(EXTRACT(EPOCH FROM (e.escalate_at - e.created_at)))::int, 0) AS review_secs,
           e.level, e.camera_id::text AS camera_id,
           to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'DD/MM/YYYY HH24:MI') AS cuando,
           c.name AS camera_name, c.place AS camera_place,
           COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code)
                     FROM event_signals es JOIN signals s ON s.code = es.signal_code
                     WHERE es.event_id = e.id), '') AS reasons,
           (SELECT m.storage_key FROM media_files m
             WHERE m.event_id = e.id AND m.kind = 'image' ORDER BY m.created_at LIMIT 1) AS image
    FROM alert_deliveries d
    JOIN notification_channels ch ON ch.id = d.channel_id
    JOIN events e ON e.id = d.event_id
    LEFT JOIN cameras c ON c.id = e.camera_id
    WHERE d.status = 'pending'
      AND now() >= d.created_at + make_interval(secs => (ARRAY[0, 5, 15, 35, 75, 155])[LEAST(d.attempts, 5) + 1])
    ORDER BY d.id LIMIT 1 FOR UPDATE OF d SKIP LOCKED
"""


def _minutos(segundos) -> str:
    segundos = int(segundos or 0)
    if segundos >= 90:
        m = round(segundos / 60)
        return f"{m} minuto{'s' if m != 1 else ''}"
    return f"{segundos} segundos"


def _cuerpo(d) -> str:
    camara = d["camera_name"] or "sin cámara asignada"
    if d["camera_place"]:
        camara += f" ({d['camera_place']})"
    return (f"Cámara: {camara}\n"
            f"Motivo: {d['reasons'] or 'actividad detectada'}\n"
            f"Hora: {d['cuando']} (hora de Lima)")


def build_text(d) -> str:
    icono, nombre = ("🚨", "ALTA") if d["level"] == "ALTO" else ("⚠️", "MEDIA")
    cuerpo = _cuerpo(d)
    audiencia = d.get("audience") or "normal"
    if audiencia == "review":
        return (f"{icono} Alerta {nombre} POR REVISAR\n{cuerpo}\n\n"
                f"Confirma o descarta. Si nadie responde en {_minutos(d.get('review_secs'))}, se avisará a los vecinos como NO VERIFICADA.")
    if audiencia == "broadcast":
        return f"✅ Alerta {nombre} CONFIRMADA por un administrador\n{cuerpo}"
    if audiencia == "escalated":
        return f"⚠️ Alerta {nombre} NO VERIFICADA: ningún administrador la revisó a tiempo\n{cuerpo}"
    return f"{icono} Alerta {nombre}\n{cuerpo}"


def build_result_text(d, modo) -> str:
    """El mensaje que ven los administradores cuando la alerta ya se resolvió (confirmar, descartar) o se escaló (escalada)."""
    icono, nombre = ("🚨", "ALTA") if d["level"] == "ALTO" else ("⚠️", "MEDIA")
    quien = d.get("revisor") or "un administrador"
    if modo == "confirmar":
        cabecera = f"✅ Alerta {nombre} CONFIRMADA por {quien} a las {d.get('revisada') or '--:--'}. Se avisó a los vecinos."
    elif modo == "descartar":
        cabecera = f"🚫 Alerta {nombre} DESCARTADA como falsa alarma por {quien} a las {d.get('revisada') or '--:--'}."
    else:
        cabecera = (f"{icono} Alerta {nombre} POR REVISAR\n⏱ Nadie respondió a tiempo: se avisó a los vecinos como NO VERIFICADA. "
                    "Todavía puedes confirmarla o descartarla.")
    return f"{cabecera}\n{_cuerpo(d)}"


def _markup(d, botones=True):
    filas = []
    if botones and d.get("audience") == "review" and d.get("event_status") == "sin verificar":
        filas.append([{"text": "✅ Confirmar", "callback_data": f"rv:{d['event_id']}:c"},
                      {"text": "🚫 Descartar", "callback_data": f"rv:{d['event_id']}:d"}])
    base = _cfg["base_url"]
    if d["camera_id"] and base.startswith("https://"):           # Telegram solo acepta enlaces seguros
        filas.append([{"text": "Ver cámara", "url": f"{base}/cameras/{d['camera_id']}"}])
    return {"inline_keyboard": filas} if filas else None


def _send(d):
    """Envía el aviso. Devuelve (la respuesta de Telegram, si llevaba foto)."""
    texto, markup = build_text(d), _markup(d)
    ruta = os.path.join(_cfg["evidence_dir"], os.path.basename(d["image"])) if d["image"] else None
    if ruta and os.path.exists(ruta):
        return _client.send_photo(d["chat_id"], ruta, texto, markup), True
    return _client.send_message(d["chat_id"], texto, markup), False


def _finish(cur, d, status, error):
    cur.execute("""UPDATE alert_deliveries SET status = %s, attempts = attempts + 1, last_error = %s WHERE id = %s""",
                (status, error, d["id"]))


def _failed_attempt(cur, d, ex):
    if ex.blocked:
        cur.execute("UPDATE notification_channels SET is_active = false WHERE id = %s", (d["channel_id"],))
        _finish(cur, d, "failed", "el usuario bloqueó el bot o borró el chat: canal desactivado")
        return
    intentos = d["attempts"] + 1
    cur.execute("UPDATE alert_deliveries SET attempts = %s, status = %s, last_error = %s WHERE id = %s",
                (intentos, "failed" if intentos >= MAX_ATTEMPTS else "pending", ex.description[:300], d["id"]))


def process_one() -> bool:
    """Envía un aviso pendiente que ya le toque. Devuelve True si había alguno."""
    with db.cursor() as cur:
        cur.execute(_SELECT_DUE, (STALE_MINUTES,))
        d = cur.fetchone()
        if d is None:
            return False
        if d["stale"]:
            _finish(cur, d, "failed", "caducó: no salió a tiempo")
        elif not d["is_active"]:
            _finish(cur, d, "failed", "el canal está en pausa o desactivado")
        elif d["audience"] == "review" and d["event_status"] != "sin verificar":
            _finish(cur, d, "failed", "la alerta ya fue revisada antes de enviarse")
        else:
            try:
                resultado, con_foto = _send(d)
            except telegram_api.TelegramError as ex:
                _failed_attempt(cur, d, ex)
            except Exception as ex:
                _failed_attempt(cur, d, telegram_api.TelegramError(0, telegram_api.clean(ex, _cfg["token"])))
            else:
                mensaje = resultado.get("message_id") if isinstance(resultado, dict) else None
                cur.execute("""UPDATE alert_deliveries SET status = 'sent', attempts = attempts + 1, sent_at = now(),
                                      last_error = NULL, telegram_message_id = %s, has_photo = %s WHERE id = %s""",
                            (mensaje, con_foto, d["id"]))
    return True


def worker_forever(stop):
    while not stop.is_set():
        try:
            while process_one():
                pass
        except Exception:
            log.exception("Fallo en la cola de alertas")
            stop.wait(5)
        stop.wait(2)


# ------------------------------------------------------------------ revisión del administrador (H6)

_RV = re.compile(r"^rv:(\d{1,18}):([cd])$")
_SELECT_MENSAJES = """
    SELECT ch.chat_id, d.telegram_message_id AS message_id, d.has_photo, d.audience,
           e.id AS event_id, e.level, e.status AS event_status, e.camera_id::text AS camera_id,
           to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'DD/MM/YYYY HH24:MI') AS cuando,
           c.name AS camera_name, c.place AS camera_place,
           COALESCE((SELECT string_agg(s.description, ', ' ORDER BY s.code)
                     FROM event_signals es JOIN signals s ON s.code = es.signal_code WHERE es.event_id = e.id), '') AS reasons,
           (SELECT u.full_name FROM users u WHERE u.id = e.reviewed_by) AS revisor,
           to_char(e.reviewed_at AT TIME ZONE 'America/Lima', 'HH24:MI') AS revisada
    FROM alert_deliveries d
    JOIN notification_channels ch ON ch.id = d.channel_id
    JOIN events e ON e.id = d.event_id
    LEFT JOIN cameras c ON c.id = e.camera_id
    WHERE d.event_id = %s AND d.audience = 'review' AND d.status = 'sent' AND d.telegram_message_id IS NOT NULL
"""


def set_siren(funcion):
    """app.py indica aquí cómo se hace sonar la sirena (la misma que usan las demás alertas)."""
    global _siren
    _siren = funcion


def enqueue_community(cur, event_id, community_id, level, audience, excluir=None):
    """
    Deja en cola el aviso para TODA la comunidad (según el nivel mínimo de cada quien), menos para quien ya recibió
    uno de este evento (los administradores que lo revisaron) y para `excluir` (quien acaba de revisarlo: ya lo sabe).
    Devuelve cuántos avisos se crearon.
    """
    cur.execute("""
        INSERT INTO alert_deliveries (event_id, channel_id, audience)
        SELECT %(event)s, ch.id, %(audience)s
        FROM notification_channels ch
        JOIN users u ON u.id = ch.user_id AND u.status = 'active'
        WHERE ch.kind = 'telegram' AND ch.is_active AND ch.verified_at IS NOT NULL AND ch.chat_id IS NOT NULL
          AND u.community_id = %(community)s::uuid AND (ch.min_level = 'MEDIO' OR %(level)s = 'ALTO')
          AND NOT EXISTS (SELECT 1 FROM alert_deliveries d WHERE d.event_id = %(event)s AND d.channel_id = ch.id)
          AND (%(excluir)s::uuid IS NULL OR ch.user_id <> %(excluir)s::uuid)""",
                {"event": event_id, "audience": audience, "community": str(community_id), "level": level, "excluir": excluir})
    return cur.rowcount


def on_review_applied(cur, res):
    """Dentro de la misma transacción que guarda la revisión: si se confirmó, los avisos a los vecinos quedan en cola."""
    if res["decision"] == "confirmar" and not res["escalada"]:
        res["avisados"] = enqueue_community(cur, res["event_id"], res["community_id"], res["level"], "broadcast", excluir=res["admin_id"])


def _editar_mensaje(m, modo):
    d = dict(m)
    texto = build_result_text(d, modo)
    markup = _markup(d, botones=(modo == "escalada")) or {"inline_keyboard": []}
    try:
        if m["has_photo"]:
            _client.edit_message_caption(m["chat_id"], m["message_id"], texto, markup)
        else:
            _client.edit_message_text(m["chat_id"], m["message_id"], texto, markup)
    except telegram_api.TelegramError as ex:
        if ex.blocked or "not modified" in ex.description.lower():
            return
        log.warning("No se pudo actualizar un mensaje de revisión: %s", ex.description)


def _editar_mensajes(event_id, modo):
    """Cambia el mensaje que recibió cada administrador: sin botones y diciendo qué pasó (o que se escaló)."""
    with db.cursor() as cur:
        cur.execute(_SELECT_MENSAJES, (event_id,))
        mensajes = [dict(r) for r in cur.fetchall()]
    for m in mensajes:
        _editar_mensaje(m, modo)


def after_review(res, admin=None):
    """Lo que sigue DESPUÉS de guardar la revisión: la sirena (si se confirmó) y actualizar los mensajes de los administradores."""
    try:
        if res["decision"] == "confirmar" and _siren is not None:
            try:
                _siren()
            except Exception:
                log.exception("No se pudo activar la sirena")
        if available():
            _editar_mensajes(res["event_id"], res["decision"])
    except Exception:
        log.exception("Falló la actualización de una revisión")


def _admin_of_chat(chat_id):
    with db.cursor() as cur:
        cur.execute("""SELECT u.id::text AS id, u.community_id::text AS community_id
                       FROM notification_channels ch JOIN users u ON u.id = ch.user_id AND u.status = 'active'
                       WHERE ch.kind = 'telegram' AND ch.chat_id = %s AND ch.verified_at IS NOT NULL AND ch.is_active
                         AND u.role = 'community_admin' LIMIT 1""", (str(chat_id),))
        fila = cur.fetchone()
    return dict(fila) if fila else None


def handle_callback(cb):
    """Un clic en «Confirmar» o «Descartar». Solo cuenta si viene del chat privado de un administrador de la comunidad del evento."""
    cid = cb.get("id")

    def responder(texto, alerta=False):
        if cid:
            try:
                _client.answer_callback_query(cid, texto, alerta)
            except telegram_api.TelegramError as ex:
                log.warning("No se pudo responder al botón: %s", ex.description)

    chat = (cb.get("message") or {}).get("chat") or {}
    m = _RV.match(str(cb.get("data") or ""))
    if (m is None or chat.get("type") != "private" or chat.get("id") is None
            or str((cb.get("from") or {}).get("id")) != str(chat["id"])):
        responder("Esa acción no es válida.")
        return
    decision = "confirmar" if m.group(2) == "c" else "descartar"
    try:
        admin = _admin_of_chat(chat["id"])
        if admin is None:
            responder("Solo un administrador de la comunidad puede revisar alertas.", True)
            return
        res = hitl.review_event(admin, int(m.group(1)), decision, "telegram", on_applied=on_review_applied)
    except hitl.ReviewError:
        responder("Esa alerta ya no está disponible.", True)           # no existe, o es de otra comunidad: se responde igual
        return
    except Exception:
        log.exception("No se pudo registrar la revisión")
        responder("No se pudo registrar. Inténtalo de nuevo en un momento.", True)
        return
    if not res["applied"]:
        responder(f"Esta alerta ya fue revisada: {res['estado']}.")
        return
    responder("Confirmada: se avisó a los vecinos." if decision == "confirmar" else "Descartada como falsa alarma.")
    threading.Thread(target=after_review, args=(res, admin), name="revision-envio", daemon=True).start()


def _on_escalated(cur, ev):
    ev["avisados"] = enqueue_community(cur, ev["id"], ev["community_id"], ev["level"], "escalated")


def escalate_once() -> int:
    """Escala las alertas cuyo tiempo se cumplió sin respuesta: avisa a todos como «no verificada» y lo anota en los mensajes de los administradores."""
    escaladas = hitl.escalate_due(_on_escalated)
    for ev in escaladas:
        log.warning("Alerta %s sin respuesta de los administradores: se avisó a %d persona(s) como NO VERIFICADA.", ev["id"], ev.get("avisados", 0))
        if available():
            try:
                _editar_mensajes(ev["id"], "escalada")
            except Exception:
                log.exception("No se pudo anotar el escalado en los mensajes de los administradores")
    return len(escaladas)


def escalation_forever(stop):
    while not stop.is_set():
        try:
            escalate_once()
        except Exception:
            log.exception("Fallo al escalar alertas sin revisar")
        stop.wait(15)


def _send_text(chat_id, text):
    _client.send_message(chat_id, text)


def notify_user(user_id, text) -> bool:
    """
    Le manda un mensaje de texto por Telegram a una persona, si lo tiene vinculado y activo. Nunca lanza errores:
    un aviso que no sale no debe romper lo que lo provocó. Devuelve si se envió.
    """
    if not available() or not user_id:
        return False
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT chat_id FROM notification_channels
                           WHERE user_id = %s AND kind = 'telegram' AND is_active AND verified_at IS NOT NULL
                             AND chat_id IS NOT NULL LIMIT 1""", (str(user_id),))
            fila = cur.fetchone()
        if fila is None:
            return False
        _send_text(fila["chat_id"], text)
        return True
    except Exception as ex:
        log.warning("No se pudo avisar por Telegram: %s", telegram_api.clean(ex, _cfg["token"]))
        return False


def camera_watch_forever(stop):
    """Revisa cada minuto si alguna cámara perdió señal (H4) y avisa por Telegram."""
    camera_watch.run_forever(stop, _send_text)


def start(polling=True):
    """Arranca los hilos: la cola de envíos, el aviso de cámaras sin señal y, si se pide, el sondeo del bot."""
    if not available():
        log.warning("Telegram sin configurar (falta TELEGRAM_TOKEN): no habrá alertas por Telegram.")
        return
    _stop.clear()
    for nombre, destino in (("alertas-cola", worker_forever), ("alertas-camaras", camera_watch_forever),
                            ("alertas-escalado", escalation_forever),
                            ("alertas-sondeo", poll_forever if polling else None)):
        if destino:
            t = threading.Thread(target=destino, args=(_stop,), name=nombre, daemon=True)
            t.start()
            _threads.append(t)
    log.warning("Alertas por Telegram activas (sondeo del bot: %s).", "sí" if polling else "no")


def stop():
    _stop.set()
    _threads.clear()
