"""
Aviso de «cámara sin señal» (H4).

Cada minuto se revisan las cámaras que dependen de una mini PC. Si una lleva más de 10 minutos sin dar señal
(cameras.last_seen_at), se avisa por Telegram UNA sola vez a su dueño, o a los administradores si es de una zona
común, y se anota en cameras.offline_notified_at. Cuando vuelve a dar señal, se avisa otra vez y se limpia la marca.

  • Las cámaras inactivas no avisan, ni las que nunca dieron señal (recién asignadas).
  • Si una mini PC se apaga y se caen varias cámaras de la misma persona, llega UN solo mensaje que las lista.
  • Si nadie tiene Telegram vinculado y activo, no se marca como avisada: se reintenta en el siguiente ciclo,
    para que el aviso llegue cuando alguien vincule su cuenta.
  • Si Telegram falla, se reintenta pasados unos minutos. Si la persona bloqueó el bot, su canal se desactiva.
"""
import logging
import os
import time

from backend import db, telegram_api

log = logging.getLogger("guardian.camera_watch")

OFFLINE_MINUTES = int(os.environ.get("CAMERA_OFFLINE_MINUTES", "10"))
CHECK_EVERY_S = int(os.environ.get("CAMERA_WATCH_EVERY_S", "60"))
RETRY_AFTER_S = 300
MAX_LISTED = 25                         # cámaras que se nombran en un mismo mensaje

_last_failure = {}                      # id de cámara -> instante del último intento fallido

_CAMERAS = """
    SELECT c.id::text AS id, c.name, c.kind, c.user_id::text AS user_id, c.community_id::text AS community_id,
           EXTRACT(EPOCH FROM now() - c.last_seen_at)::int AS silent_s
    FROM cameras c
    WHERE c.is_active AND c.edge_device_id IS NOT NULL AND c.last_seen_at IS NOT NULL AND {condition}
    ORDER BY c.community_id, c.name"""
_OFFLINE = "c.offline_notified_at IS NULL AND c.last_seen_at < now() - make_interval(mins => %s)"
_RECOVERED = "c.offline_notified_at IS NOT NULL AND c.last_seen_at >= now() - make_interval(mins => %s)"

_RECIPIENTS = """
    SELECT ch.id::text AS channel_id, ch.chat_id
    FROM notification_channels ch
    JOIN users u ON u.id = ch.user_id AND u.status = 'active'
    WHERE ch.kind = 'telegram' AND ch.is_active AND ch.verified_at IS NOT NULL AND ch.chat_id IS NOT NULL
      AND ((%(owner)s::uuid IS NOT NULL AND ch.user_id = %(owner)s::uuid)
        OR (%(owner)s::uuid IS NULL AND u.community_id = %(community)s::uuid AND u.role = 'community_admin'))"""


def _minutes(camera):
    return max(1, round(camera["silent_s"] / 60))


def offline_text(cams):
    pie = "Revisa que la mini PC esté encendida y con internet."
    if len(cams) == 1:
        return f"⚠️ VIGÍA: la cámara «{cams[0]['name']}» dejó de dar señal hace {_minutes(cams[0])} min. {pie}"
    lineas = [f"• {c['name']} (hace {_minutes(c)} min)" for c in cams[:MAX_LISTED]]
    if len(cams) > MAX_LISTED:
        lineas.append(f"• …y {len(cams) - MAX_LISTED} más")
    return f"⚠️ VIGÍA: {len(cams)} cámaras dejaron de dar señal:\n" + "\n".join(lineas) + f"\n{pie}"


def recovered_text(cams):
    if len(cams) == 1:
        return f"✅ VIGÍA: la cámara «{cams[0]['name']}» volvió a dar señal."
    lineas = [f"• {c['name']}" for c in cams[:MAX_LISTED]]
    if len(cams) > MAX_LISTED:
        lineas.append(f"• …y {len(cams) - MAX_LISTED} más")
    return f"✅ VIGÍA: {len(cams)} cámaras volvieron a dar señal:\n" + "\n".join(lineas)


def _recipients(cur, camera):
    owner = camera["user_id"] if camera["kind"] == "private" else None
    cur.execute(_RECIPIENTS, {"owner": owner, "community": camera["community_id"]})
    return [dict(r) for r in cur.fetchall()]


def _plan(cur, cams):
    """Qué mensaje va a qué canal: {canal: {"chat_id", "cams": [...]}}, y las cámaras sin ningún destinatario."""
    por_canal, sin_destino = {}, []
    ahora = time.monotonic()
    for cam in cams:
        if ahora - _last_failure.get(cam["id"], -RETRY_AFTER_S - 1) < RETRY_AFTER_S:
            continue                                     # falló hace poco: se espera antes de reintentar
        canales = _recipients(cur, cam)
        if not canales:
            sin_destino.append(cam)
        for canal in canales:
            por_canal.setdefault(canal["channel_id"], {"chat_id": canal["chat_id"], "cams": []})["cams"].append(cam)
    return por_canal, sin_destino


def _deliver(por_canal, texto, send):
    """Envía los mensajes. Devuelve las cámaras entregadas a al menos un canal y los canales bloqueados."""
    entregadas, bloqueados = set(), []
    for canal_id, dato in por_canal.items():
        try:
            send(dato["chat_id"], texto(dato["cams"]))
        except telegram_api.TelegramError as ex:
            if ex.blocked:
                bloqueados.append(canal_id)
                log.warning("Un canal de Telegram quedó desactivado: el usuario bloqueó el bot o borró el chat.")
            else:
                log.warning("No se pudo avisar de una cámara sin señal: %s", ex.description[:200])
            for cam in dato["cams"]:
                _last_failure[cam["id"]] = time.monotonic()
        except Exception as ex:                          # noqa: BLE001
            log.warning("No se pudo avisar de una cámara sin señal: %s", telegram_api.clean(ex))
            for cam in dato["cams"]:
                _last_failure[cam["id"]] = time.monotonic()
        else:
            entregadas.update(c["id"] for c in dato["cams"])
    return entregadas, bloqueados


def check_once(send):
    """
    Una pasada de revisión. send(chat_id, texto) envía un mensaje y lanza TelegramError si falla.
    Devuelve cuántas cámaras se avisaron como caídas y cuántas como recuperadas.
    """
    with db.cursor() as cur:
        cur.execute(_CAMERAS.format(condition=_OFFLINE), (OFFLINE_MINUTES,))
        caidas = [dict(r) for r in cur.fetchall()]
        cur.execute(_CAMERAS.format(condition=_RECOVERED), (OFFLINE_MINUTES,))
        vueltas = [dict(r) for r in cur.fetchall()]
        plan_caidas, _ = _plan(cur, caidas)
        plan_vueltas, vueltas_sin_destino = _plan(cur, vueltas)

    enviadas_caidas, bloq1 = _deliver(plan_caidas, offline_text, send)
    enviadas_vueltas, bloq2 = _deliver(plan_vueltas, recovered_text, send)

    with db.cursor() as cur:
        if bloq1 or bloq2:
            cur.execute("UPDATE notification_channels SET is_active = false WHERE id = ANY(%s::uuid[])",
                        (bloq1 + bloq2,))
        if enviadas_caidas:
            cur.execute("UPDATE cameras SET offline_notified_at = now() WHERE id = ANY(%s::uuid[])",
                        (sorted(enviadas_caidas),))
        # Una cámara que se recuperó y no tiene a quién avisar también se limpia: si no, su marca viejaría
        # tapando el próximo aviso de una caída de verdad.
        limpiar = sorted(enviadas_vueltas | {c["id"] for c in vueltas_sin_destino})
        if limpiar:
            cur.execute("UPDATE cameras SET offline_notified_at = NULL WHERE id = ANY(%s::uuid[])", (limpiar,))
    for cam_id in enviadas_caidas | enviadas_vueltas:
        _last_failure.pop(cam_id, None)
    return {"offline": len(enviadas_caidas), "recovered": len(enviadas_vueltas)}


def run_forever(stop, send):
    while not stop.is_set():
        try:
            check_once(send)
        except Exception:                                # noqa: BLE001
            log.exception("Fallo al revisar las cámaras sin señal")
        stop.wait(CHECK_EVERY_S)
