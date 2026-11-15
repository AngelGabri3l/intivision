"""
Ubicación de las cámaras y mapa de calor por cámara (H8, fase D).

REGLAS DE PRIVACIDAD (la ubicación exacta de una cámara privada es el domicilio de una persona: dato personal, Ley 29733):
  • El mapa es solo para administradores.
  • Una cámara de zona común la ubica el administrador. Una cámara privada solo la ubica su DUEÑO, y solo aparece en el mapa si el
    dueño marcó expresamente «mostrar en el mapa» (map_visible, apagado por defecto, con su fecha) y además deja ver la cámara al
    administrador (admin_can_view). Se dibuja con las coordenadas exactas que el dueño eligió, sin su nombre.
  • El dueño puede quitar la ubicación o retirar el permiso cuando quiera. Si la cámara se archiva, la ubicación se borra.
  • audit_log anota que se fijó o se quitó una ubicación, SIN guardar las coordenadas. El CSV del panel no lleva coordenadas.

LAS IMÁGENES DEL MAPA (teselas) pasan por este servidor: el navegador solo habla con el mismo sitio (la política de seguridad no permite
nada de terceros) y el proveedor no ve la IP ni la zona que mira cada persona, solo la del servidor. Hay un solo servidor de origen (el de
la variable MAP_TILE_URL), solo se aceptan números z/x/y dentro de rango, no se siguen redirecciones, la respuesta tiene tope de tamaño y
tiene que ser un PNG, hay caché en disco (la política de OpenStreetMap pide guardar al menos 7 días) y un límite de pedidos por persona.
El mapa gratuito de OpenStreetMap sirve para arrancar y ensayar; con tráfico real hay que usar un proveedor con clave o mapas propios.
"""
import datetime
import json
import logging
import math
import os
import threading
import time

import psycopg2.errors
import requests

from backend import cameras, dashboard, db

log = logging.getLogger("guardian.mapa")

LIMA_CENTRO = (-12.046374, -77.042793)      # Plaza Mayor de Lima, solo para centrar el mapa si la comunidad no tiene centro propio
LEJOS_KM = 5.0                              # más lejos que esto del centro de la comunidad, se pide confirmar
PRECISION_MAX_M = 100_000
TESELA_URL_DEFECTO = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATRIBUCION = "© colaboradores de OpenStreetMap"
TESELA_MAX_BYTES = 200_000
TESELA_TTL_S = 7 * 86400
TESELA_POR_MINUTO = 400
CACHE_MAX_MB_DEFECTO = 200
PODAR_CADA = 200                            # cada cuántas teselas nuevas se revisa el tamaño de la caché
PNG = b"\x89PNG\r\n\x1a\n"

_pedidos = {}
_candado = threading.Lock()
_escritas = [0]


class MapaError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


FALTA_MIGRACION = MapaError(503, "falta_migracion", "Falta aplicar la migración 005 de la base de datos (ubicación de cámaras).")


# ------------------------------------------------------------------ coordenadas

def validar_coordenadas(lat, lng):
    """Dos números reales dentro de rango, redondeados a 6 decimales (unos 11 cm). (0, 0) es el error típico de un GPS sin señal."""
    for v in (lat, lng):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise MapaError(422, "coordenadas_invalidas", "La latitud y la longitud deben ser dos números.")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise MapaError(422, "coordenadas_fuera_de_rango", "La latitud va de −90 a 90 y la longitud de −180 a 180.")
    lat, lng = round(float(lat), 6), round(float(lng), 6)
    if lat == 0 and lng == 0:
        raise MapaError(422, "coordenadas_invalidas", "Las coordenadas 0, 0 no son una ubicación real: revisa que el GPS haya encontrado señal.")
    return lat, lng


def validar_precision(valor):
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor) or not 0 <= valor <= PRECISION_MAX_M:
        raise MapaError(422, "precision_invalida", "La precisión debe ser un número de metros entre 0 y 100000.")
    return int(round(valor))


def distancia_km(lat1, lng1, lat2, lng2):
    """Distancia sobre la esfera terrestre (fórmula de haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def centro_comunidad(cur, community_id):
    """(lat, lng, definido). Si la comunidad no tiene centro propio se usa el de Lima solo para centrar el mapa, sin pedir confirmaciones."""
    cur.execute("SELECT center_lat, center_lng FROM communities WHERE id = %s", (str(community_id),))
    fila = cur.fetchone()
    if fila and fila["center_lat"] is not None and fila["center_lng"] is not None:
        return float(fila["center_lat"]), float(fila["center_lng"]), True
    return LIMA_CENTRO[0], LIMA_CENTRO[1], False


# ------------------------------------------------------------------ fijar, quitar y consentimiento

def _camara_permitida(fila, actor, como):
    """El dueño solo puede tocar su cámara privada; el administrador, solo las de zona común de su comunidad. Archivadas, nunca."""
    if fila is None or not fila["is_active"]:
        return False
    if como == "dueno":
        return fila["kind"] == "private" and fila["user_id"] == str(actor["id"])
    return fila["kind"] == "common" and fila["community_id"] == str(actor["community_id"]) and actor.get("role") == "community_admin"


def _bloquear_camara(cur, camera_id, actor, como):
    if not cameras.is_uuid(camera_id):
        raise MapaError(404, "no_encontrada", "No existe esa cámara.")
    cur.execute("""SELECT id::text AS id, kind, user_id::text AS user_id, community_id::text AS community_id, is_active
                   FROM cameras WHERE id = %s FOR UPDATE""", (camera_id,))
    fila = cur.fetchone()
    if not _camara_permitida(fila, actor, como):
        raise MapaError(404, "no_encontrada", "No existe esa cámara.")      # no se distingue «no existe» de «no es tuya»
    return fila


def _auditar(cur, actor, community_id, camera_id, accion, meta, ip):
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id, ip, meta)
                   VALUES ('user', %s, %s, %s, 'camera', %s, %s, %s::jsonb)""",
                (str(actor["id"]), str(community_id), accion, camera_id, ip, json.dumps(meta)))


def fijar_ubicacion(actor, camera_id, datos, ip=None, como="dueno"):
    """
    Guarda la ubicación de una cámara. `datos`: lat, lng, precision_m (opcional), confirmar_lejos y, solo para el dueño,
    visible_en_mapa (el consentimiento; por defecto NO). Si queda a más de 5 km del centro de la comunidad, responde 409 «muy_lejos»
    hasta que se confirme: casi siempre es un error de dedo.
    """
    lat, lng = validar_coordenadas(datos.get("lat"), datos.get("lng"))
    precision = validar_precision(datos.get("precision_m"))
    visible = datos.get("visible_en_mapa", False)
    if como == "dueno" and not isinstance(visible, bool):
        raise MapaError(422, "visible_invalido", "«Mostrar en el mapa» debe ser verdadero o falso.")
    try:
        with db.cursor() as cur:
            fila = _bloquear_camara(cur, camera_id, actor, como)
            clat, clng, definido = centro_comunidad(cur, fila["community_id"])
            lejos = definido and distancia_km(lat, lng, clat, clng) > LEJOS_KM
            if lejos and datos.get("confirmar_lejos") is not True:
                km = distancia_km(lat, lng, clat, clng)
                raise MapaError(409, "muy_lejos", f"Esa ubicación queda a {km:.1f} km de la zona de la comunidad. Si es la correcta, confírmala.")
            if como == "dueno":
                cur.execute("""UPDATE cameras SET lat = %s, lng = %s, location_accuracy_m = %s, location_set_at = now(), location_set_by = %s,
                                   map_visible_at = CASE WHEN %s AND map_visible THEN map_visible_at WHEN %s THEN now() ELSE NULL END,
                                   map_visible = %s
                               WHERE id = %s""", (lat, lng, precision, str(actor["id"]), visible, visible, visible, camera_id))
            else:
                cur.execute("""UPDATE cameras SET lat = %s, lng = %s, location_accuracy_m = %s, location_set_at = now(), location_set_by = %s
                               WHERE id = %s""", (lat, lng, precision, str(actor["id"]), camera_id))
            _auditar(cur, actor, fila["community_id"], camera_id, "set_camera_location",
                     {"como": como, "visible_en_mapa": visible if como == "dueno" else None, "lejos": bool(lejos)}, ip)
    except psycopg2.errors.UndefinedColumn:
        raise FALTA_MIGRACION from None
    return {"lat": lat, "lng": lng, "precision_m": precision, "visible_en_mapa": visible if como == "dueno" else None, "lejos": bool(lejos)}


def quitar_ubicacion(actor, camera_id, ip=None, como="dueno"):
    """Borra la ubicación y retira el permiso de mostrarla. Es inmediato: la cámara sale del mapa en el acto."""
    try:
        with db.cursor() as cur:
            fila = _bloquear_camara(cur, camera_id, actor, como)
            cur.execute("""UPDATE cameras SET lat = NULL, lng = NULL, location_accuracy_m = NULL, location_set_at = NULL, location_set_by = NULL,
                               map_visible = false, map_visible_at = NULL WHERE id = %s""", (camera_id,))
            _auditar(cur, actor, fila["community_id"], camera_id, "clear_camera_location", {"como": como}, ip)
    except psycopg2.errors.UndefinedColumn:
        raise FALTA_MIGRACION from None
    return {"ok": True}


def cambiar_visibilidad_mapa(actor, camera_id, visible, ip=None):
    """El dueño da o retira el permiso de mostrar su cámara en el mapa sin cambiar su ubicación. Para darlo, la cámara debe estar ubicada."""
    if not isinstance(visible, bool):
        raise MapaError(422, "visible_invalido", "«Mostrar en el mapa» debe ser verdadero o falso.")
    try:
        with db.cursor() as cur:
            fila = _bloquear_camara(cur, camera_id, actor, "dueno")
            cur.execute("SELECT lat FROM cameras WHERE id = %s", (camera_id,))
            if visible and cur.fetchone()["lat"] is None:
                raise MapaError(409, "sin_ubicacion", "Primero asigna la ubicación de la cámara.")
            cur.execute("""UPDATE cameras SET map_visible_at = CASE WHEN %s AND map_visible THEN map_visible_at WHEN %s THEN now() ELSE NULL END,
                               map_visible = %s WHERE id = %s""", (visible, visible, visible, camera_id))
            _auditar(cur, actor, fila["community_id"], camera_id, "set_camera_map_visibility", {"visible": visible}, ip)
    except psycopg2.errors.UndefinedColumn:
        raise FALTA_MIGRACION from None
    return {"visible_en_mapa": visible}


def limpiar_ubicacion(cur, camera_id):
    """
    Al archivar una cámara (reasignarla o quitar su asignación) su ubicación se borra. Va con un punto de guardado: si la migración 005 no se
    aplicó todavía, no hay nada que limpiar y la asignación sigue funcionando.
    """
    cur.execute("SAVEPOINT limpiar_ubicacion")
    try:
        cur.execute("""UPDATE cameras SET lat = NULL, lng = NULL, location_accuracy_m = NULL, location_set_at = NULL, location_set_by = NULL,
                           map_visible = false, map_visible_at = NULL WHERE id = %s""", (camera_id,))
    except psycopg2.errors.UndefinedColumn:
        cur.execute("ROLLBACK TO SAVEPOINT limpiar_ubicacion")
    else:
        cur.execute("RELEASE SAVEPOINT limpiar_ubicacion")


def ficha_ubicacion(user, cam):
    """Lo que necesita la ficha: solo para el dueño de una cámara privada activa. None si no corresponde o si falta la migración."""
    if not (cam["kind"] == "private" and cam["is_active"] and cam["user_id"] == str(user["id"])):
        return None
    try:
        with db.cursor() as cur:
            cur.execute("SELECT lat, lng, location_accuracy_m, map_visible, location_set_at FROM cameras WHERE id = %s", (cam["id"],))
            fila = cur.fetchone()
            clat, clng, definido = centro_comunidad(cur, cam["community_id"])
    except psycopg2.errors.UndefinedColumn:
        return None
    return {"lat": fila["lat"], "lng": fila["lng"], "precision_m": fila["location_accuracy_m"], "visible_en_mapa": bool(fila["map_visible"]),
            "fijada_en": fila["location_set_at"].isoformat(timespec="seconds") if fila["location_set_at"] else None,
            "centro": {"lat": clat, "lng": clng, "definido": definido}, "admin_puede_ver": bool(cam["admin_can_view"])}


# ------------------------------------------------------------------ datos del mapa de calor

def datos_mapa(admin, desde=None, hasta=None, modo="incidentes", ahora=None):
    """
    Las cámaras que se dibujan en el mapa con sus incidentes (o sus falsas alarmas) del rango, y las de zona común que aún no tienen ubicación.
    Una privada se dibuja solo si tiene ubicación, su dueño marcó «mostrar en el mapa» y deja ver la cámara al administrador; nunca con el
    nombre del dueño.
    """
    if modo not in ("incidentes", "falsas"):
        raise dashboard.DashboardError(422, "modo_invalido", "El modo debe ser «incidentes» o «falsas».")
    f = dashboard.leer_filtros(admin, desde, hasta, None, ahora)
    p = {"comunidad": str(admin["community_id"]), "admin_id": str(admin["id"]), "desde": f["desde"], "hasta": f["hasta"]}
    try:
        with db.cursor() as cur:
            clat, clng, definido = centro_comunidad(cur, admin["community_id"])
            cur.execute("""SELECT c.id::text AS id, c.name, c.place, c.kind, c.lat, c.lng,
                                  count(e.id) FILTER (WHERE e.status <> 'falsa alarma') AS incidentes,
                                  count(e.id) FILTER (WHERE e.status = 'falsa alarma') AS falsas,
                                  max(e.occurred_at) AS ultimo
                           FROM cameras c
                           LEFT JOIN events e ON e.camera_id = c.id AND e.community_id = c.community_id
                                AND e.occurred_at >= (%(desde)s::date)::timestamp AT TIME ZONE 'America/Lima'
                                AND e.occurred_at < ((%(hasta)s::date) + 1)::timestamp AT TIME ZONE 'America/Lima'
                           WHERE c.community_id = %(comunidad)s AND c.is_active AND c.lat IS NOT NULL
                             AND (c.kind = 'common' OR (c.map_visible AND (c.admin_can_view OR c.user_id = %(admin_id)s)))
                           GROUP BY c.id ORDER BY lower(c.name), c.id""", p)
            filas = cur.fetchall()
            cur.execute("""SELECT c.id::text AS id, c.name, c.place FROM cameras c
                           WHERE c.community_id = %(comunidad)s AND c.is_active AND c.kind = 'common' AND c.lat IS NULL
                           ORDER BY lower(c.name), c.id""", p)
            sin_ubicar = [dict(r) for r in cur.fetchall()]
    except psycopg2.errors.UndefinedColumn:
        raise FALTA_MIGRACION from None
    camaras = []
    for r in filas:
        valor = int(r["falsas"] if modo == "falsas" else r["incidentes"])
        camaras.append({"id": r["id"], "name": r["name"], "place": r["place"], "kind": r["kind"], "lat": r["lat"], "lng": r["lng"],
                        "incidentes": int(r["incidentes"]), "falsas": int(r["falsas"]), "valor": valor,
                        "ultimo": r["ultimo"].astimezone(dashboard.LIMA).isoformat(timespec="seconds") if r["ultimo"] else None})
    return {"centro": {"lat": clat, "lng": clng, "definido": definido},
            "rango": {"desde": f["desde"].isoformat(), "hasta": f["hasta"].isoformat(), "dias": (f["hasta"] - f["desde"]).days + 1},
            "modo": modo, "camaras": camaras, "sin_ubicar": sin_ubicar, "maximo": max([c["valor"] for c in camaras], default=0),
            "tesela": {"url": "/mapa/teselas/{z}/{x}/{y}.png", "atribucion": ATRIBUCION, "disponible": bool(contacto())}}


# ------------------------------------------------------------------ las imágenes del mapa (teselas)

def contacto():
    """Quién responde por esta aplicación ante el proveedor del mapa (un correo o una dirección institucional). Sin él, no hay mapa."""
    return os.environ.get("MAP_CONTACT", "").strip()


def _carpeta_cache():
    return os.environ.get("MAP_CACHE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "maptiles")


def _limitar(usuario_id, ahora=None):
    """Hasta 400 teselas por minuto por persona: un mapa normal pide unas 20 por pantalla."""
    ahora = time.monotonic() if ahora is None else ahora
    with _candado:
        recientes = [t for t in _pedidos.get(usuario_id, []) if ahora - t < 60]
        if len(recientes) >= TESELA_POR_MINUTO:
            _pedidos[usuario_id] = recientes
            raise MapaError(429, "demasiados_pedidos", "Demasiados pedidos de mapa en poco tiempo. Espera un minuto.")
        recientes.append(ahora)
        _pedidos[usuario_id] = recientes
        if len(_pedidos) > 2000:
            for k in [k for k, v in _pedidos.items() if not v or ahora - v[-1] > 60]:
                del _pedidos[k]


def validar_tesela(z, x, y):
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (z, x, y)) or not 0 <= z <= 19:
        raise MapaError(404, "tesela_invalida", "Esa imagen del mapa no existe.")
    if not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise MapaError(404, "tesela_invalida", "Esa imagen del mapa no existe.")


def _descargar(z, x, y):
    url = os.environ.get("MAP_TILE_URL") or TESELA_URL_DEFECTO
    cabeceras = {"User-Agent": f"VIGIA-Guardian/1.0 ({contacto()})", "Accept": "image/png"}
    try:
        with requests.get(url.format(z=z, x=x, y=y), headers=cabeceras, timeout=(3.05, 8), stream=True, allow_redirects=False) as r:
            if r.status_code != 200:
                raise MapaError(502, "tesela_no_disponible", "El proveedor del mapa no respondió bien. Inténtalo en un momento.")
            trozos, total = [], 0
            for trozo in r.iter_content(chunk_size=16384):
                total += len(trozo)
                if total > TESELA_MAX_BYTES:
                    raise MapaError(502, "tesela_no_disponible", "El proveedor del mapa respondió algo demasiado grande.")
                trozos.append(trozo)
    except requests.RequestException:
        raise MapaError(502, "tesela_no_disponible", "No se pudo contactar al proveedor del mapa.") from None
    datos = b"".join(trozos)
    if not datos.startswith(PNG):
        raise MapaError(502, "tesela_no_disponible", "El proveedor del mapa no devolvió una imagen válida.")
    return datos


def _podar():
    """Si la caché pasa del tope, borra las teselas más viejas hasta dejarla en el 80 %."""
    carpeta = _carpeta_cache()
    tope = int(os.environ.get("MAP_CACHE_MAX_MB", CACHE_MAX_MB_DEFECTO)) * 1024 * 1024
    archivos, total = [], 0
    for base, _, nombres in os.walk(carpeta):
        for n in nombres:
            ruta = os.path.join(base, n)
            try:
                st = os.stat(ruta)
            except OSError:
                continue
            archivos.append((st.st_mtime, st.st_size, ruta))
            total += st.st_size
    if total <= tope:
        return 0
    borrados = 0
    for _, tam, ruta in sorted(archivos):
        if total <= tope * 0.8:
            break
        try:
            os.remove(ruta)
            total -= tam
            borrados += 1
        except OSError:
            pass
    return borrados


def obtener_tesela(usuario_id, z, x, y):
    """
    La imagen del mapa: de la caché si tiene menos de 7 días; si no, del proveedor. Si el proveedor falla y hay una copia vieja, se usa esa.
    """
    if not contacto():
        raise MapaError(503, "mapa_no_configurado", "El mapa no está configurado: falta indicar MAP_CONTACT en el servidor.")
    validar_tesela(z, x, y)
    _limitar(str(usuario_id))
    ruta = os.path.join(_carpeta_cache(), str(z), str(x), f"{y}.png")
    existe = os.path.isfile(ruta)
    if existe and time.time() - os.path.getmtime(ruta) < TESELA_TTL_S:
        with open(ruta, "rb") as fh:
            return fh.read()
    try:
        datos = _descargar(z, x, y)
    except MapaError:
        if existe:
            with open(ruta, "rb") as fh:
                return fh.read()
        raise
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = f"{ruta}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporal, "wb") as fh:
        fh.write(datos)
    os.replace(temporal, ruta)                              # nadie lee una tesela a medio escribir
    with _candado:
        _escritas[0] += 1
        revisar = _escritas[0] % PODAR_CADA == 0
    if revisar:
        _podar()
    return datos
