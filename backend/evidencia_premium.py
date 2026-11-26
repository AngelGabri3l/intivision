"""
Extras del Plan Premium: descarga de evidencia con huellas SHA-256, vencimiento de fotos y clips, y resumen semanal.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import zipfile

from backend import acceso, planes

def _alcance(user):
    """(SQL, parámetros) de los eventos que esta persona puede ver, con marcadores %(acc_uid)s y %(acc_com)s (ver acceso.py)."""
    return acceso.nombrado(acceso.eventos(user, "e", "c"), user)


def _sha256_archivo(ruta) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def evento_visible(cur, user, event_id):
    """El evento con su cámara, si este usuario puede verlo; si no, None (no se distingue de «no existe»)."""
    cond, extra = _alcance(user)
    cur.execute(f"""SELECT e.id, e.level, e.score, e.people_count, e.status, e.camera_id::text AS camera_id,
                           to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD HH24:MI:SS') AS hora_lima,
                           to_char(e.reviewed_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD HH24:MI:SS') AS revisado_lima,
                           e.ai_description, e.ai_sees_people, r.full_name AS revisado_por, c.name AS camara, c.place AS lugar, c.user_id::text AS dueno,
                           co.name AS comunidad, e.community_id::text AS community_id
                    FROM events e LEFT JOIN cameras c ON c.id = e.camera_id LEFT JOIN users r ON r.id = e.reviewed_by
                    JOIN communities co ON co.id = e.community_id
                    WHERE e.id = %(ev)s AND {cond}""",
                dict(extra, ev=int(event_id)))
    return cur.fetchone()


def zip_evidencia(cur, user, event_id, evidence_dir, ip=None):
    """
    (código HTTP, contenido o mensaje, nombre del archivo). El ZIP lleva la foto, el clip, evento.json y huellas.txt con el
    SHA-256 de cada archivo (se verifica con `sha256sum -c huellas.txt`). Solo para cámaras con el beneficio en su plan.
    """
    ev = evento_visible(cur, user, event_id)
    if ev is None:
        return 404, "No encontrado.", None
    plan = planes.de_camara(cur, ev["camera_id"]) if ev["camera_id"] else {}
    if not plan.get("evidence_export"):
        return 403, "La descarga de evidencia es parte del Plan Premium.", None
    cur.execute("""SELECT id, kind, storage_key, sha256 FROM media_files WHERE event_id = %s ORDER BY kind DESC, created_at""", (ev["id"],))
    archivos = []
    for m in cur.fetchall():
        ruta = os.path.join(evidence_dir, os.path.basename(m["storage_key"]))
        if not os.path.exists(ruta):
            continue
        huella = _sha256_archivo(ruta)
        if not m["sha256"]:
            cur.execute("UPDATE media_files SET sha256 = %s WHERE id = %s", (huella, m["id"]))
        nombre = ("foto.jpg" if m["kind"] == "image" else "clip.mp4")
        if any(a[0] == nombre for a in archivos):
            nombre = nombre.replace(".", f"_{len(archivos)}.")
        archivos.append((nombre, ruta, huella))
    cur.execute("""SELECT s.description, es.detail FROM event_signals es JOIN signals s ON s.code = es.signal_code
                   WHERE es.event_id = %s ORDER BY s.code""", (ev["id"],))
    senales = [r["detail"] or r["description"] for r in cur.fetchall()]
    ficha = {"evento": ev["id"], "fecha_hora_lima": ev["hora_lima"], "comunidad": ev["comunidad"], "camara": ev["camara"],
             "lugar": ev["lugar"], "nivel": ev["level"], "puntaje": ev["score"], "personas": ev["people_count"], "senales": senales,
             "revision": {"estado": ev["status"], "por": ev["revisado_por"], "fecha_hora_lima": ev["revisado_lima"]},
             "descripcion_automatica": ({"texto": ev["ai_description"], "ve_personas": ev["ai_sees_people"],
                                         "nota": "Generada por IA; puede equivocarse."} if ev["ai_description"] else None),
             "archivos": {n: h for n, _, h in archivos},
             "generado_lima": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d %H:%M:%S"),
             "nota": "Generado por INTIVISION. Las huellas SHA-256 permiten comprobar que los archivos no se modificaron."}
    datos_ficha = json.dumps(ficha, ensure_ascii=False, indent=2).encode("utf-8")
    huellas = "".join(f"{h}  {n}\n" for n, _, h in archivos) + f"{hashlib.sha256(datos_ficha).hexdigest()}  evento.json\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, ruta, _ in archivos:
            z.write(ruta, nombre, compress_type=zipfile.ZIP_STORED)       # foto y video ya vienen comprimidos
        z.writestr("evento.json", datos_ficha)
        z.writestr("huellas.txt", huellas)
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip)
                   VALUES ('user', %s, %s, %s, 'export_evidence', 'event', %s, %s)""",
                (user["id"], ev["community_id"], ev["dueno"] if ev["dueno"] and ev["dueno"] != str(user["id"]) else None, str(ev["id"]), ip))
    return 200, buf.getvalue(), f"evidencia_evento_{ev['id']}.zip"


# ------------------------------------------------------------------ vencimiento de fotos y clips

def purgar(cur, evidence_dir, ejecutar=False):
    """Fotos y clips vencidos (expires_at pasado). Sin `ejecutar`, solo cuenta. Devuelve (archivos, bytes)."""
    cur.execute("""SELECT id, storage_key, size_bytes FROM media_files WHERE expires_at IS NOT NULL AND expires_at < now() ORDER BY expires_at""")
    filas = cur.fetchall()
    total = sum(int(f["size_bytes"] or 0) for f in filas)
    if ejecutar and filas:
        for f in filas:
            try:
                os.remove(os.path.join(evidence_dir, os.path.basename(f["storage_key"])))
            except OSError:
                pass
        cur.execute("DELETE FROM media_files WHERE id = ANY(%s::uuid[])", ([str(f["id"]) for f in filas],))
    return len(filas), total


def purga_forever(stop, evidence_dir, cada_s: float = 3600):
    """Cada hora borra las fotos y clips vencidos (antes solo se hacía a mano con la terminal)."""
    import logging
    from backend import db
    log = logging.getLogger("backend.purga")
    while not stop.is_set():
        try:
            with db.cursor() as cur:
                n, total = purgar(cur, evidence_dir, ejecutar=True)
            if n:
                log.info("Se borraron %d archivo(s) vencido(s) (%.1f MB).", n, total / 1e6)
        except Exception:       # noqa: BLE001
            log.exception("Fallo al borrar evidencias vencidas")
        stop.wait(cada_s)


# ------------------------------------------------------------------ resumen semanal

def destinatarios_semanales(cur):
    """Quienes reciben el resumen: tienen Premium (propio o de su comunidad) y Telegram vinculado y activo."""
    cur.execute(f"""SELECT DISTINCT u.id::text AS id, u.full_name, u.role, u.community_id::text AS community_id
                    FROM users u JOIN notification_channels ch ON ch.user_id = u.id
                    WHERE u.status = 'active' AND ch.kind = 'telegram' AND ch.is_active AND ch.verified_at IS NOT NULL
                      AND {planes.sql_usuario_premium('u')} ORDER BY u.full_name""")
    return cur.fetchall()


def resumen_semanal(cur, user):
    """El texto del resumen de los últimos 7 días que esta persona puede ver."""
    cond, p = _alcance(user)
    cur.execute(f"""SELECT count(*) AS total,
                           count(*) FILTER (WHERE e.level = 'ALTO') AS altos,
                           count(*) FILTER (WHERE e.level = 'MEDIO') AS medios,
                           count(*) FILTER (WHERE e.status = 'falsa alarma') AS falsas,
                           count(*) FILTER (WHERE e.status = 'sin verificar' AND e.level IN ('MEDIO', 'ALTO')) AS pendientes,
                           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM event_signals es WHERE es.event_id = e.id AND es.signal_code = 'weapon')) AS armas
                    FROM events e LEFT JOIN cameras c ON c.id = e.camera_id
                    WHERE {cond} AND e.occurred_at >= now() - interval '7 days'""", p)
    t = cur.fetchone()
    cur.execute(f"""SELECT c.name, count(*) AS n FROM events e JOIN cameras c ON c.id = e.camera_id
                    WHERE {cond} AND e.occurred_at >= now() - interval '7 days' AND e.level IN ('MEDIO', 'ALTO')
                    GROUP BY c.name ORDER BY n DESC, c.name LIMIT 1""", p)
    top = cur.fetchone()
    cur.execute(f"""SELECT to_char(e.occurred_at AT TIME ZONE 'America/Lima', 'HH24') AS h, count(*) AS n
                    FROM events e LEFT JOIN cameras c ON c.id = e.camera_id
                    WHERE {cond} AND e.occurred_at >= now() - interval '7 days' AND e.level IN ('MEDIO', 'ALTO')
                    GROUP BY h ORDER BY n DESC, h LIMIT 1""", p)
    hora = cur.fetchone()
    nombre = (user.get("full_name") or "").split(" ")[0]
    lineas = [f"📊 Resumen semanal de INTIVISION{(', ' + nombre) if nombre else ''}", "Últimos 7 días:"]
    if not t["total"]:
        lineas.append("• Sin eventos. Tus cámaras estuvieron tranquilas.")
    else:
        lineas.append(f"• {t['total']} evento(s): {t['altos']} de nivel alto y {t['medios']} de nivel medio.")
        if t["armas"]:
            lineas.append(f"• ⚠️ {t['armas']} con posible arma.")
        if t["falsas"]:
            lineas.append(f"• {t['falsas']} marcado(s) como falsa alarma.")
        if top:
            lineas.append(f"• Cámara con más alertas: {top['name']} ({top['n']}).")
        if hora:
            lineas.append(f"• Hora con más alertas: {hora['h']}:00 a {(int(hora['h']) + 1) % 24:02d}:00.")
        if user["role"] == "community_admin" and t["pendientes"]:
            lineas.append(f"• {t['pendientes']} alerta(s) siguen sin revisar en «Pendientes».")
    return "\n".join(lineas)
