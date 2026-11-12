"""
Imagen en vivo (H7): el relevo entre la mini PC y el celular.

    Celular ──wss──► /ws/camera/{id} ──► HUB ◄── /edge/ws ◄──wss── Agente (mini PC)

La mini PC está detrás de un router y el servidor no puede llamarla: es ELLA la que abre y mantiene una conexión hacia acá
(no hace falta abrir ningún puerto en la cuadra). Por esa conexión el servidor le pide `stream` cuando alguien abre su cámara
y `stop` cuando sale el último espectador; el agente responde con JPEG que el hub reparte a quienes miran.

Quién puede ver una cámara lo decide `cameras.get_camera` (el mismo permiso de la ficha): el vecino, las suyas; el
administrador, las zonas comunes, las suyas y las privadas cuyo dueño no se las ocultó. Cualquier otro caso recibe lo mismo
que ante una cámara que no existe.

Sirve con UN solo proceso de uvicorn (el hub vive en memoria). Si algún día se usan varios, hay que moverlo a Redis.
No guarda nada en disco: la última imagen vive en memoria y la foto de respaldo es la del último evento.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque

import psycopg2
import psycopg2.pool
from fastapi import WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from backend import auth, cameras, db

log = logging.getLogger("vigia.live")

FPS = int(os.environ.get("LIVE_FPS", "5"))                          # cuadros por segundo que se le piden a la mini PC
MAX_VIEWERS = int(os.environ.get("LIVE_MAX_VIEWERS", "3"))          # espectadores a la vez por cámara
MAX_VISIT_S = int(os.environ.get("LIVE_MAX_VISIT_S", "300"))        # duración máxima de una visita
MAX_STREAMS_PER_EDGE = int(os.environ.get("LIVE_MAX_STREAMS", "4"))  # flujos a la vez por mini PC (protege su CPU)
MAX_FRAME_BYTES = 250_000
IDENTIFY_S = int(os.environ.get("LIVE_IDENTIFY_S", "10"))             # cuánto dura la imagen de «Identificar» (H5)
MAX_IDENTIFY_PER_EDGE = 2            # cámaras que se pueden identificar a la vez en una misma mini PC
IDENTIFY_GRACE_S = 2.0               # margen para que llegue el último cuadro tras cumplirse el tiempo
MIN_FRAME_GAP_S = 0.08               # como mucho ~12 cuadros por segundo por cámara: lo que sobre se descarta
NO_SIGNAL_S = 5.0                    # sin cuadros durante este tiempo = «sin señal»
LAST_FRAME_MAX_AGE_S = 600           # la última imagen en memoria sirve hasta 10 minutos
REVALIDATE_S = float(os.environ.get("LIVE_REVALIDATE_S", "30"))   # cada cuánto se comprueba que el token de la mini PC siga vigente
CONNECTS_PER_MINUTE = int(os.environ.get("LIVE_CONNECTS_PER_MINUTE", "20"))   # aperturas del visor por usuario y minuto

# Códigos de cierre (los mismos que usa /ws/live, más los de este módulo)
SIN_SESION, ORIGEN, NO_EXISTE, TIEMPO_MAXIMO, REEMPLAZADA, LIMITE, BASE_NO_DISPONIBLE = 4401, 4403, 4404, 4408, 4409, 4429, 1013

_evidence_dir = ""


def configure(evidence_dir: str) -> None:
    global _evidence_dir
    _evidence_dir = evidence_dir


def _ahora() -> float:
    return time.monotonic()


def _iso(valor):
    return valor.isoformat() if isinstance(valor, (datetime.datetime, datetime.date)) else None


def jpeg_valido(datos) -> bool:
    """Un JPEG razonable: tamaño acotado, empieza como JPEG y termina como JPEG. No se decodifica (cuesta CPU)."""
    return (isinstance(datos, (bytes, bytearray)) and 100 < len(datos) <= MAX_FRAME_BYTES
            and bytes(datos[:3]) == b"\xff\xd8\xff" and bytes(datos[-2:]) == b"\xff\xd9")


# ------------------------------------------------------------------ consultas (síncronas: se llaman con to_thread)

def _edge_cameras(edge_id, community_id) -> set:
    with db.cursor() as cur:
        cur.execute("""SELECT id::text AS id FROM cameras
                       WHERE edge_device_id = %s AND community_id = %s AND is_active""", (edge_id, community_id))
        return {r["id"] for r in cur.fetchall()}


def _camera_edge(camera_id):
    with db.cursor() as cur:
        cur.execute("SELECT edge_device_id::text AS e FROM cameras WHERE id = %s", (camera_id,))
        fila = cur.fetchone()
    return fila["e"] if fila else None


def _audit_view(user, cam, ip) -> None:
    with db.cursor() as cur:
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, ip)
                       VALUES ('user', %s, %s, %s, 'view_camera_live', 'camera', %s, %s)""",
                    (user["id"], user["community_id"], cam["user_id"], cam["id"], ip))


def _last_event_image(camera_id):
    with db.cursor() as cur:
        cur.execute("""SELECT m.storage_key FROM media_files m JOIN events e ON e.id = m.event_id
                       WHERE e.camera_id = %s AND m.kind = 'image' ORDER BY e.id DESC LIMIT 1""", (camera_id,))
        fila = cur.fetchone()
    return fila["storage_key"] if fila else None


# ------------------------------------------------------------------ el hub

class EdgeConn:
    """La conexión abierta de una mini PC."""

    def __init__(self, ws, edge, camaras):
        self.ws = ws
        self.id, self.community_id = str(edge["id"]), str(edge["community_id"])
        self.cameras = set(camaras)          # las cámaras de esta mini PC (solo de estas se aceptan cuadros)
        self.streams = set()                 # las que se le pidieron en vivo ahora
        self._lock = asyncio.Lock()

    async def send(self, mensaje) -> None:
        async with self._lock:
            await self.ws.send_json(mensaje)


class Viewer:
    """Un celular mirando una cámara. Guarda UN solo cuadro pendiente: si es lento, se pierden los viejos, no se acumulan."""

    def __init__(self, ws, user, camera_id):
        self.ws, self.user, self.camera_id = ws, user, camera_id
        self.frame = None
        self.control = []
        self.wakeup = asyncio.Event()
        self.joined = _ahora()
        self.estado = None
        self.cerrar = None                   # código con el que hay que cerrar, si lo decidió el servidor

    def offer_frame(self, datos) -> None:
        self.frame = datos
        self.wakeup.set()

    def offer_control(self, mensaje) -> None:
        self.control.append(mensaje)
        self.wakeup.set()


class Hub:
    def __init__(self):
        self.edges = {}                       # edge_id -> EdgeConn
        self.viewers = defaultdict(set)       # camera_id -> {Viewer}
        self.last = {}                        # camera_id -> (jpeg, instante)
        self.frame_at = {}                    # camera_id -> instante del último cuadro aceptado
        self.camera_ok = {}                   # camera_id -> lo que dice la mini PC (False = la cámara no da cuadros)
        self.camera_edge = {}                 # camera_id -> edge_id
        self.connects = defaultdict(deque)    # user_id -> instantes de aperturas recientes
        self.identify = {}                    # source_id -> {edge_id, hasta, last}: cámaras SIN asignar que se están identificando (H5)
        self.stats = {"cuadros": 0, "descartados": 0}

    def reset(self) -> None:
        self.__init__()

    def edge_for(self, camera_id):
        conn = self.edges.get(self.camera_edge.get(camera_id))
        return conn if conn and camera_id in conn.cameras else None

    def rate_ok(self, user_id) -> bool:
        q, ahora = self.connects[user_id], _ahora()
        while q and q[0] < ahora - 60:
            q.popleft()
        if len(q) >= CONNECTS_PER_MINUTE:
            return False
        q.append(ahora)
        return True

    async def start_stream(self, camera_id) -> bool:
        conn = self.edge_for(camera_id)
        if conn is None:
            return False
        if camera_id in conn.streams:
            return True
        if len(conn.streams) >= MAX_STREAMS_PER_EDGE:
            return False
        conn.streams.add(camera_id)
        try:
            await conn.send({"cmd": "stream", "camera_id": camera_id, "fps": FPS})
        except Exception:       # noqa: BLE001 - la mini PC se cayó justo ahora
            conn.streams.discard(camera_id)
            return False
        return True

    async def stop_stream(self, camera_id) -> None:
        conn = self.edge_for(camera_id)
        if conn is None or camera_id not in conn.streams:
            return
        conn.streams.discard(camera_id)
        try:
            await conn.send({"cmd": "stop", "camera_id": camera_id})
        except Exception:       # noqa: BLE001
            pass

    def push_frame(self, camera_id, jpeg) -> bool:
        ahora = _ahora()
        if ahora - self.frame_at.get(camera_id, 0.0) < MIN_FRAME_GAP_S:
            self.stats["descartados"] += 1
            return False
        self.frame_at[camera_id] = ahora
        self.last[camera_id] = (bytes(jpeg), ahora)
        self.camera_ok[camera_id] = True
        self.stats["cuadros"] += 1
        for v in self.viewers.get(camera_id, ()):
            v.offer_frame(bytes(jpeg))
        return True

    def identify_active(self, source_id):
        """El registro de una identificación en curso, o None (y se olvida) si ya se cumplió su tiempo."""
        e = self.identify.get(source_id)
        if e is not None and _ahora() > e["hasta"] + IDENTIFY_GRACE_S:
            del self.identify[source_id]
            return None
        return e

    async def start_identify(self, source_id, edge_id, device_key) -> str:
        """
        Le pide a la mini PC una imagen de una fuente sin asignar durante IDENTIFY_S segundos.
        Devuelve «ok», «sin_conexion» (la mini PC no está conectada) u «ocupado» (ya identifica el máximo a la vez).
        """
        conn = self.edges.get(edge_id)
        if conn is None:
            return "sin_conexion"
        ahora = _ahora()
        for sid in [s for s, e in self.identify.items() if ahora > e["hasta"] + IDENTIFY_GRACE_S]:
            del self.identify[sid]
        actual = self.identify.get(source_id)
        if actual is not None and actual["edge_id"] == edge_id:
            return "ok"                                      # ya se está identificando: no se repite la orden
        if sum(1 for e in self.identify.values() if e["edge_id"] == edge_id) >= MAX_IDENTIFY_PER_EDGE:
            return "ocupado"
        self.identify[source_id] = {"edge_id": edge_id, "hasta": ahora + IDENTIFY_S, "last": None}
        try:
            await conn.send({"cmd": "identify", "source_id": source_id, "device_key": device_key, "seconds": IDENTIFY_S, "fps": 1})
        except Exception:       # noqa: BLE001 - la mini PC se cayó justo ahora
            self.identify.pop(source_id, None)
            return "sin_conexion"
        return "ok"

    def push_identify(self, source_id, jpeg) -> bool:
        e = self.identify_active(source_id)
        if e is None:
            return False
        e["last"] = (bytes(jpeg), _ahora())
        return True

    def identify_frame(self, source_id):
        e = self.identify_active(source_id)
        return e["last"][0] if e is not None and e["last"] is not None else None

    def estado_para(self, v: Viewer) -> str:
        """en_vivo, esperando o sin_senal, según lo que está pasando con la cámara de este espectador."""
        ahora = _ahora()
        if self.edge_for(v.camera_id) is None or self.camera_ok.get(v.camera_id) is False:
            return "sin_senal"
        if ahora - self.frame_at.get(v.camera_id, -1e9) <= NO_SIGNAL_S:
            return "en_vivo"
        return "sin_senal" if ahora - v.joined > NO_SIGNAL_S else "esperando"


hub = Hub()


# ------------------------------------------------------------------ la mini PC: /edge/ws

def _bearer(ws: WebSocket):
    cab = ws.headers.get("authorization", "")
    return cab[7:].strip() if cab.lower().startswith("bearer ") else None


async def _cerrar(ws, codigo) -> None:
    try:
        await ws.close(code=codigo)
    except Exception:       # noqa: BLE001 - ya estaba cerrada
        pass


def _on_frame(conn: EdgeConn, datos: bytes) -> None:
    if len(datos) <= 16:
        return
    try:
        camera_id = str(uuid.UUID(bytes=bytes(datos[:16])))
    except ValueError:
        return
    jpeg = datos[16:]
    if not jpeg_valido(jpeg):
        return                               # algo que no es un JPEG: se ignora
    if camera_id in conn.cameras:
        hub.camera_edge[camera_id] = conn.id
        hub.push_frame(camera_id, jpeg)
    elif hub.identify.get(camera_id, {}).get("edge_id") == conn.id:
        hub.push_identify(camera_id, jpeg)   # una fuente SIN asignar que el administrador pidió identificar (H5)
    # cualquier otro identificador (una cámara que no es de esta mini PC) se ignora


def _on_text(conn: EdgeConn, texto: str) -> None:
    try:
        m = json.loads(texto)
    except ValueError:
        return
    if isinstance(m, dict) and m.get("type") == "estado" and m.get("camera_id") in conn.cameras and isinstance(m.get("ok"), bool):
        hub.camera_ok[m["camera_id"]] = m["ok"]


async def handle_edge(ws: WebSocket) -> None:
    await ws.accept()
    token = _bearer(ws)
    try:
        edge = await asyncio.to_thread(auth.edge_from_token, token)
        if edge is None:
            await _cerrar(ws, SIN_SESION)        # token inválido o revocado
            return
        camaras = await asyncio.to_thread(_edge_cameras, edge["id"], edge["community_id"])
    except (psycopg2.OperationalError, psycopg2.pool.PoolError):
        await _cerrar(ws, BASE_NO_DISPONIBLE)
        return
    conn = EdgeConn(ws, edge, camaras)
    anterior = hub.edges.get(conn.id)
    hub.edges[conn.id] = conn
    if anterior is not None:
        await _cerrar(anterior.ws, REEMPLAZADA)  # una mini PC solo tiene una conexión: la nueva reemplaza a la vieja
    for cid in conn.cameras:
        hub.camera_edge[cid] = conn.id
    log.info("Mini PC conectada al relevo (%d cámara(s)).", len(conn.cameras))
    try:
        await conn.send({"type": "hola", "v": 1})
        for cid in conn.cameras:
            if hub.viewers.get(cid):             # había gente mirando: se le pide el flujo otra vez
                await hub.start_stream(cid)
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=REVALIDATE_S)
            except asyncio.TimeoutError:
                try:
                    vigente = await asyncio.to_thread(auth.edge_from_token, token)
                except (psycopg2.OperationalError, psycopg2.pool.PoolError):
                    continue
                if vigente is None:                # se revocó el token mientras estaba conectada
                    await _cerrar(ws, SIN_SESION)
                    break
                continue
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                _on_frame(conn, msg["bytes"])
            elif msg.get("text") is not None:
                _on_text(conn, msg["text"])
    except Exception:       # noqa: BLE001 - cualquier corte de la conexión
        pass
    finally:
        if hub.edges.get(conn.id) is conn:
            del hub.edges[conn.id]
        conn.streams.clear()
        for sid in [s for s, e in hub.identify.items() if e["edge_id"] == conn.id]:
            hub.identify.pop(sid, None)
        log.info("Mini PC desconectada del relevo.")


# ------------------------------------------------------------------ el celular: /ws/camera/{id}

async def _drenar(v: Viewer) -> None:
    while v.control:
        await v.ws.send_json(v.control.pop(0))
    if v.frame is not None:
        datos, v.frame = v.frame, None
        await v.ws.send_bytes(datos)


async def _enviar(v: Viewer) -> None:
    try:
        while True:
            await v.wakeup.wait()
            v.wakeup.clear()
            await _drenar(v)
    except Exception:       # noqa: BLE001 - el celular se fue
        return


async def _recibir(v: Viewer) -> None:
    try:
        while True:
            msg = await v.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
    except Exception:       # noqa: BLE001
        return


async def _vigilar(v: Viewer) -> None:
    """Cada medio segundo: ¿pasó el tiempo máximo? ¿cambió el estado de la cámara?"""
    while True:
        await asyncio.sleep(0.5)
        if _ahora() - v.joined > MAX_VISIT_S:
            v.offer_control({"type": "fin", "motivo": "tiempo_maximo", "segundos": MAX_VISIT_S})
            v.cerrar = TIEMPO_MAXIMO
            return
        nuevo = hub.estado_para(v)
        if nuevo != v.estado:
            v.estado = nuevo
            v.offer_control({"type": nuevo})


async def handle_viewer(ws: WebSocket, camera_id: str) -> None:
    await ws.accept()
    if not auth.origin_allowed(ws):
        await _cerrar(ws, ORIGEN)                # otro sitio intentó abrir la cámara
        return
    try:
        user = await asyncio.to_thread(auth.user_from_websocket, ws)
        if user is None:
            await _cerrar(ws, SIN_SESION)
            return
        if not hub.rate_ok(user["id"]):          # antes de tocar la base: no se puede martillar con identificadores
            await ws.send_json({"type": "limite", "motivo": "demasiadas_conexiones"})
            await _cerrar(ws, LIMITE)
            return
        cam = await asyncio.to_thread(cameras.get_camera, user, camera_id)
        if cam is None:
            await _cerrar(ws, NO_EXISTE)         # no existe o no es suya: indistinguible
            return
        if len(hub.viewers.get(cam["id"], ())) >= MAX_VIEWERS:
            await ws.send_json({"type": "limite", "max": MAX_VIEWERS})
            await _cerrar(ws, LIMITE)
            return
        eid = await asyncio.to_thread(_camera_edge, cam["id"])
        if eid:
            hub.camera_edge[cam["id"]] = eid
            conn = hub.edges.get(eid)
            if conn is not None and cam["id"] not in conn.cameras:
                conn.cameras = await asyncio.to_thread(_edge_cameras, eid, user["community_id"])   # se asignó después de conectarse
        if user["role"] == "community_admin" and cam["kind"] == "private" and cam["user_id"] != user["id"]:
            try:
                ip = auth._clean_ip(auth.client_ip(ws))
            except Exception:       # noqa: BLE001
                ip = None
            await asyncio.to_thread(_audit_view, user, cam, ip)     # el administrador mira la cámara de otra persona: queda registrado
    except (psycopg2.OperationalError, psycopg2.pool.PoolError):
        await _cerrar(ws, BASE_NO_DISPONIBLE)
        return

    v = Viewer(ws, user, cam["id"])
    primero = not hub.viewers.get(cam["id"])
    hub.viewers[cam["id"]].add(v)
    log.info("Visor conectado a una cámara (%d espectador(es)).", len(hub.viewers[cam["id"]]))
    tareas = []
    try:
        if hub.edge_for(cam["id"]) is None:
            v.estado = "sin_senal"
            v.offer_control({"type": "sin_senal", "desde": _iso(cam.get("last_seen_at")), "motivo": "equipo_desconectado"})
        else:
            v.estado = "esperando"
            v.offer_control({"type": "esperando"})
            if primero or cam["id"] not in (hub.edge_for(cam["id"]).streams):
                if not await hub.start_stream(cam["id"]):
                    v.estado = "sin_senal"
                    v.offer_control({"type": "sin_senal", "desde": None, "motivo": "demasiados_flujos"})
        tareas = [asyncio.create_task(_enviar(v)), asyncio.create_task(_recibir(v)), asyncio.create_task(_vigilar(v))]
        await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
        if v.cerrar is not None:
            try:
                await _drenar(v)                 # que llegue el aviso de «tiempo máximo» antes de cerrar
            except Exception:       # noqa: BLE001
                pass
            await _cerrar(ws, v.cerrar)
    finally:
        for t in tareas:
            t.cancel()
        if tareas:
            await asyncio.gather(*tareas, return_exceptions=True)
        hub.viewers[cam["id"]].discard(v)
        if not hub.viewers[cam["id"]]:
            del hub.viewers[cam["id"]]
            await hub.stop_stream(cam["id"])     # salió el último: la mini PC deja de enviar
        log.info("Visor desconectado.")


# ------------------------------------------------------------------ la última imagen (y plan B del visor)

def last_image_response(user, camera_id):
    """La última imagen de la cámara: la del flujo en vivo si es reciente; si no, la foto de su último evento."""
    cam = cameras.get_camera(user, camera_id)
    if cam is None:
        return JSONResponse(status_code=404, content={"error": "no encontrado"})
    item = hub.last.get(cam["id"])
    if item and _ahora() - item[1] <= LAST_FRAME_MAX_AGE_S:
        return Response(content=item[0], media_type="image/jpeg")
    clave = _last_event_image(cam["id"])
    if clave and _evidence_dir:
        ruta = os.path.join(_evidence_dir, os.path.basename(clave))
        if os.path.exists(ruta):
            return FileResponse(ruta, media_type="image/jpeg")
    return JSONResponse(status_code=404, content={"error": "sin imagen"})
