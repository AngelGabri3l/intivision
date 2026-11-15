"""
Los datos del panel de incidentes del administrador (H8).

  • GET /admin/panel/datos         las tarjetas, los gráficos, el ranking, las señales y los equipos (JSON)
  • GET /admin/panel/exportar.csv  los eventos del rango, para abrirlos en Excel

REGLAS (las mismas en todas las consultas):
  • Solo cuentan las cámaras que el administrador puede ver: las de zona común, las privadas con admin_can_view
    encendido y las suyas. Una cámara que su dueño oculta no aparece en NINGUNA cifra, ni en el ranking, ni en el
    total de cámaras. Los eventos sin cámara (análisis de videos) no cuentan.
  • «Incidente» es un evento que no se descartó. «Falsa alarma» es el que un administrador descartó. Un evento sin
    verificar cuenta como incidente hasta que se revise.
  • Todo se agrupa por la hora de Lima (el servidor guarda UTC): un evento a las 23:30 cuenta en su día y en su hora.
  • Todas las consultas llevan parámetros; ningún valor que mande el navegador se pega en el SQL.
"""
import csv
import datetime
import io
import json
import re
import threading
import time

import psycopg2.extras

from backend import db, hitl, sources

LIMA = datetime.timezone(datetime.timedelta(hours=-5))      # Perú no tiene horario de verano: siempre UTC-5
DEFECTO_DIAS = 30
MAX_DIAS = 366                  # el rango más largo que se puede pedir
MIN_PREVIO = 5                  # si el periodo anterior tuvo menos incidentes, la comparación es en número y no en porcentaje
POCOS_DATOS = 20                # por debajo de esto el mapa de calor avisa que son pocos datos
TOP_RANKING = 10
TOP_SENALES = 8
VENTANA_HORAS = 6               # largo del horario sensible que se sugiere
MIN_PARA_SUGERIR = 20           # con menos incidentes en los últimos 30 días no se sugiere nada
HORARIO_DEFECTO = (22, 6)       # el del motor (config.yaml) cuando la comunidad no definió uno
LIMITE_CSV = 50_000
ESPERA_CSV_S = 5                # entre una exportación y la siguiente del mismo administrador
LOTE_CSV = 500

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
NIVELES = ("ALTO", "MEDIO", "BAJO")
_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Las cámaras que este administrador puede ver (c es la fila de cameras).
VISIBLE = "(c.community_id = %(comunidad)s AND (c.kind = 'common' OR c.admin_can_view OR c.user_id = %(admin_id)s))"
LOCAL = "(e.occurred_at AT TIME ZONE 'America/Lima')"      # la hora de Lima del evento, como «timestamp» sin zona

_ultima_exportacion = {}
_candado = threading.Lock()


class DashboardError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _ahora(ahora=None):
    return ahora or datetime.datetime.now(datetime.timezone.utc)


def _hoy_lima(ahora):
    return ahora.astimezone(LIMA).date()


def _base(admin, f=None):
    p = {"comunidad": str(admin["community_id"]), "admin_id": str(admin["id"])}
    if f:
        p.update(desde=f["desde"], hasta=f["hasta"], camara=f["camara"])
    return p


def _donde(f, con_falsas):
    """Las condiciones de los eventos del rango y la cámara elegidos, ya con la regla de visibilidad."""
    cond = [VISIBLE, "e.community_id = %(comunidad)s",
            "e.occurred_at >= (%(desde)s::date)::timestamp AT TIME ZONE 'America/Lima'",
            "e.occurred_at < ((%(hasta)s::date) + 1)::timestamp AT TIME ZONE 'America/Lima'"]
    if f["camara"]:
        cond.append("e.camera_id = %(camara)s")
    if not con_falsas:
        cond.append("e.status <> 'falsa alarma'")
    return " AND ".join(cond)


# ------------------------------------------------------------------ filtros

def leer_filtros(admin, desde=None, hasta=None, camara=None, ahora=None):
    """
    Valida lo que pide el navegador. Por defecto: los últimos 30 días hasta hoy (hora de Lima), todas las cámaras.
    Una cámara que no existe, que es de otra comunidad o que su dueño oculta responde igual: 404.
    """
    hoy = _hoy_lima(_ahora(ahora))
    try:
        for texto in (desde, hasta):
            if texto and not _FECHA.match(str(texto)):
                raise ValueError
        d_hasta = datetime.date.fromisoformat(hasta) if hasta else hoy
        d_desde = datetime.date.fromisoformat(desde) if desde else d_hasta - datetime.timedelta(days=DEFECTO_DIAS - 1)
    except ValueError:
        raise DashboardError(422, "fecha_invalida", "Las fechas deben tener el formato AAAA-MM-DD y existir en el calendario.") from None
    if d_desde > d_hasta:
        raise DashboardError(422, "rango_invalido", "La fecha inicial no puede ser posterior a la final.")
    if d_hasta > hoy:
        raise DashboardError(422, "rango_invalido", "La fecha final no puede ser futura.")
    if (d_hasta - d_desde).days + 1 > MAX_DIAS:
        raise DashboardError(422, "rango_muy_largo", f"El rango no puede pasar de {MAX_DIAS} días.")
    id_camara = None
    if camara:
        if not _UUID.match(str(camara)):
            raise DashboardError(404, "no_encontrada", "No existe esa cámara.")
        with db.cursor() as cur:
            cur.execute(f"SELECT c.id::text AS id FROM cameras c WHERE c.id = %(camara)s AND {VISIBLE}",
                        {**_base(admin), "camara": str(camara)})
            fila = cur.fetchone()
        if fila is None:
            raise DashboardError(404, "no_encontrada", "No existe esa cámara.")
        id_camara = fila["id"]
    return {"desde": d_desde, "hasta": d_hasta, "camara": id_camara}


# ------------------------------------------------------------------ tarjetas, con su comparación

def comparar(actual, anterior):
    """
    La diferencia con el periodo anterior. Con menos de 5 incidentes antes, un porcentaje engaña (de 1 a 3 es «+200 %»):
    se da solo la diferencia. Si antes no hubo ninguno y ahora sí, es «nuevo».
    """
    dif = actual - anterior
    if anterior == 0:
        return {"diferencia": dif, "porcentaje": None, "nuevo": actual > 0}
    if anterior < MIN_PREVIO:
        return {"diferencia": dif, "porcentaje": None, "nuevo": False}
    return {"diferencia": dif, "porcentaje": round(dif * 100.0 / anterior), "nuevo": False}


# ventana: (cuándo empieza, cuándo termina, si el final cuenta). {H} es la medianoche de hoy en Lima; {A}, el momento actual en Lima.
# «Hoy» se compara con ayer HASTA LA MISMA HORA; 7 y 30 días, con los 7 y 30 días justo anteriores.
_VENTANAS = {
    "hoy": {"actual": ("{H}", "{A}", True), "anterior": ("{H} - interval '1 day'", "{A} - interval '1 day'", True)},
    "7d": {"actual": ("{H} - interval '6 days'", "{A}", True), "anterior": ("{H} - interval '13 days'", "{H} - interval '6 days'", False)},
    "30d": {"actual": ("{H} - interval '29 days'", "{A}", True), "anterior": ("{H} - interval '59 days'", "{H} - interval '29 days'", False)},
}


def tarjetas(admin, camara=None, ahora=None):
    """Incidentes de hoy, de los últimos 7 y de los últimos 30 días, por nivel, y cada uno frente a su periodo anterior."""
    ahora = _ahora(ahora)
    columnas, nombres = [], []
    for clave, ventana in _VENTANAS.items():
        for cuando, (ini, fin, incluye) in ventana.items():
            for nivel in (None,) + NIVELES:
                cond = f"x.l >= {ini.format(H='b.h', A='b.a')} AND x.l {'<=' if incluye else '<'} {fin.format(H='b.h', A='b.a')}"
                if nivel:
                    cond += f" AND x.level = '{nivel}'"
                columnas.append(f"count(*) FILTER (WHERE {cond}) AS c{len(columnas)}")
                nombres.append((clave, cuando, nivel or "total"))
    p = {**_base(admin), "hoy": _hoy_lima(ahora), "ahora": ahora, "camara": camara}
    sql = f"""
        WITH b AS (SELECT (%(hoy)s::date)::timestamp AS h, (%(ahora)s::timestamptz AT TIME ZONE 'America/Lima') AS a)
        SELECT {', '.join(columnas)}
        FROM b,
             LATERAL (SELECT {LOCAL} AS l, e.level
                      FROM events e JOIN cameras c ON c.id = e.camera_id
                      WHERE {VISIBLE} AND e.community_id = %(comunidad)s AND e.status <> 'falsa alarma'
                        AND e.occurred_at >= (b.h - interval '59 days') AT TIME ZONE 'America/Lima'
                        AND e.occurred_at <= %(ahora)s::timestamptz
                        {'AND e.camera_id = %(camara)s' if camara else ''}) x"""
    with db.cursor() as cur:
        cur.execute(sql, p)
        fila = list(cur.fetchone().values())
    cuentas = {}
    for valor, (clave, cuando, nivel) in zip(fila, nombres):
        cuentas.setdefault(clave, {}).setdefault(cuando, {})[nivel] = int(valor)
    salida = {}
    for clave, v in cuentas.items():
        salida[clave] = {"actual": v["actual"], "anterior": v["anterior"],
                         "comparacion": comparar(v["actual"]["total"], v["anterior"]["total"]),
                         "comparacion_alto": comparar(v["actual"]["ALTO"], v["anterior"]["ALTO"])}
    return salida


def camaras_en_linea(admin, camara=None, ahora=None):
    """Cuántas de las cámaras de las mini PC que puede ver el administrador dieron señal hace poco."""
    p = {**_base(admin), "ahora": _ahora(ahora), "camara": camara, "segundos": sources.ONLINE_SECONDS}
    with db.cursor() as cur:
        cur.execute(f"""SELECT count(*) AS total,
                               count(*) FILTER (WHERE c.last_seen_at >= %(ahora)s::timestamptz - make_interval(secs => %(segundos)s)) AS en_linea
                        FROM cameras c WHERE {VISIBLE} AND c.is_active AND c.source_id IS NOT NULL
                        {'AND c.id = %(camara)s' if camara else ''}""", p)
        fila = cur.fetchone()
    return {"total": int(fila["total"]), "en_linea": int(fila["en_linea"])}


# ------------------------------------------------------------------ gráficos

def serie_diaria(admin, f):
    """Incidentes por día en el rango, por nivel. Los días sin incidentes salen con ceros (el gráfico no salta días)."""
    with db.cursor() as cur:
        cur.execute(f"""SELECT {LOCAL}::date AS dia, e.level, count(*) AS n
                        FROM events e JOIN cameras c ON c.id = e.camera_id
                        WHERE {_donde(f, False)} GROUP BY 1, 2""", _base(admin, f))
        filas = cur.fetchall()
    por_dia = {}
    for r in filas:
        por_dia.setdefault(r["dia"], {})[r["level"]] = int(r["n"])
    salida, dia = [], f["desde"]
    while dia <= f["hasta"]:
        v = por_dia.get(dia, {})
        item = {"dia": dia.isoformat(), "ALTO": v.get("ALTO", 0), "MEDIO": v.get("MEDIO", 0), "BAJO": v.get("BAJO", 0)}
        item["total"] = item["ALTO"] + item["MEDIO"] + item["BAJO"]
        salida.append(item)
        dia += datetime.timedelta(days=1)
    return salida


def calor(admin, f, con_falsas=False):
    """Incidentes por día de la semana (lunes primero) y por hora de Lima: una matriz de 7 filas por 24 columnas."""
    with db.cursor() as cur:
        cur.execute(f"""SELECT EXTRACT(ISODOW FROM {LOCAL})::int AS dow, EXTRACT(HOUR FROM {LOCAL})::int AS hora, count(*) AS n
                        FROM events e JOIN cameras c ON c.id = e.camera_id
                        WHERE {_donde(f, con_falsas)} GROUP BY 1, 2""", _base(admin, f))
        filas = cur.fetchall()
    matriz = [[0] * 24 for _ in range(7)]
    for r in filas:
        matriz[r["dow"] - 1][r["hora"]] = int(r["n"])
    total = sum(sum(fila) for fila in matriz)
    return {"matriz": matriz, "maximo": max(max(fila) for fila in matriz), "total": total, "pocos_datos": total < POCOS_DATOS,
            "incluye_falsas": bool(con_falsas), "dias": DIAS}


def ranking(admin, f):
    """Las cámaras con más incidentes en el rango, con sus falsas alarmas."""
    with db.cursor() as cur:
        cur.execute(f"""SELECT c.id::text AS id, c.name, c.place, c.kind, c.is_active,
                               count(*) FILTER (WHERE e.status <> 'falsa alarma') AS incidentes,
                               count(*) FILTER (WHERE e.status = 'falsa alarma') AS falsas,
                               max(e.occurred_at) AS ultimo
                        FROM events e JOIN cameras c ON c.id = e.camera_id
                        WHERE {_donde(f, True)}
                        GROUP BY c.id ORDER BY incidentes DESC, falsas DESC, lower(c.name), c.id LIMIT {TOP_RANKING}""", _base(admin, f))
        filas = cur.fetchall()
    return [{"id": r["id"], "name": r["name"], "place": r["place"], "kind": r["kind"], "archivada": not r["is_active"],
             "incidentes": int(r["incidentes"]), "falsas": int(r["falsas"]),
             "ultimo": r["ultimo"].astimezone(LIMA).isoformat(timespec="seconds") if r["ultimo"] else None} for r in filas]


def senales(admin, f):
    """
    Las señales que más aparecen en los eventos del rango, cada una dividida en incidentes y falsas alarmas (sirve para saber
    cuál produce más alarmas falsas). «Persona detectada» no se cuenta: está en todos los eventos.
    """
    with db.cursor() as cur:
        cur.execute(f"""SELECT s.code, s.description,
                               count(*) FILTER (WHERE e.status <> 'falsa alarma') AS incidentes,
                               count(*) FILTER (WHERE e.status = 'falsa alarma') AS falsas
                        FROM event_signals es
                        JOIN events e ON e.id = es.event_id
                        JOIN cameras c ON c.id = e.camera_id
                        JOIN signals s ON s.code = es.signal_code
                        WHERE {_donde(f, True)} AND es.signal_code <> 'person_present'
                        GROUP BY s.code, s.description ORDER BY count(*) DESC, s.code LIMIT {TOP_SENALES}""", _base(admin, f))
        filas = cur.fetchall()
    return [{"code": r["code"], "description": r["description"], "incidentes": int(r["incidentes"]), "falsas": int(r["falsas"])} for r in filas]


def equipos(admin, ahora=None):
    """
    El estado de cada mini PC: en línea o sin señal, su versión y sus cámaras. El estado del equipo es de la infraestructura y se muestra
    completo; el conteo de cámaras sigue la regla de privacidad (no cuenta las que su dueño oculta).
    """
    p = {**_base(admin), "ahora": _ahora(ahora), "segundos": sources.ONLINE_SECONDS}
    with db.cursor() as cur:
        cur.execute(f"""
            SELECT d.id::text AS id, d.name, d.status, d.version, d.last_seen_at,
                   CASE WHEN d.last_seen_at IS NULL THEN NULL
                        ELSE GREATEST(0, EXTRACT(EPOCH FROM (%(ahora)s::timestamptz - d.last_seen_at)))::bigint END AS seconds_since,
                   (SELECT count(*) FROM cameras c WHERE c.edge_device_id = d.id AND c.is_active AND {VISIBLE}) AS camaras,
                   (SELECT count(*) FROM cameras c WHERE c.edge_device_id = d.id AND c.is_active AND {VISIBLE}
                       AND c.last_seen_at >= %(ahora)s::timestamptz - make_interval(secs => %(segundos)s)) AS camaras_en_linea,
                   (SELECT count(*) FROM camera_sources s WHERE s.edge_device_id = d.id AND s.state = 'available'
                       AND NOT EXISTS (SELECT 1 FROM cameras c WHERE c.source_id = s.id AND c.is_active)) AS sin_asignar
            FROM edge_devices d WHERE d.community_id = %(comunidad)s ORDER BY lower(d.name), d.name""", p)
        filas = cur.fetchall()
    return [{"id": r["id"], "name": r["name"], "state": sources._estado_equipo(r["status"], r["last_seen_at"], r["seconds_since"]),
             "version": r["version"], "seconds_since": r["seconds_since"],
             "camaras": int(r["camaras"]), "camaras_en_linea": int(r["camaras_en_linea"]), "sin_asignar": int(r["sin_asignar"])} for r in filas]


def opciones_camara(admin):
    """Las cámaras entre las que el administrador puede filtrar: las que puede ver y están activas o tienen historial."""
    with db.cursor() as cur:
        cur.execute(f"""SELECT c.id::text AS id, c.name, c.place, c.kind, c.is_active FROM cameras c
                        WHERE {VISIBLE} AND (c.is_active OR EXISTS (SELECT 1 FROM events e WHERE e.camera_id = c.id))
                        ORDER BY c.is_active DESC, lower(c.name), c.id LIMIT 200""", _base(admin))
        filas = cur.fetchall()
    return [{"id": r["id"], "name": r["name"], "place": r["place"], "kind": r["kind"], "archivada": not r["is_active"]} for r in filas]


def contar_exportacion(admin, f):
    """Cuántas filas tendría el CSV del rango (para avisar antes de descargar si pasa del tope)."""
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM events e JOIN cameras c ON c.id = e.camera_id WHERE {_donde(f, True)}", _base(admin, f))
        return int(cur.fetchone()["n"])


# ------------------------------------------------------------------ horario sensible sugerido (fase C)

def suma_ventana(horas, inicio, largo=VENTANA_HORAS):
    """Cuántos incidentes caen en las `largo` horas que empiezan en `inicio` (puede cruzar la medianoche)."""
    return sum(horas[(inicio + k) % 24] for k in range(largo))


def ventana_mas_cargada(horas, largo=VENTANA_HORAS):
    """La hora de inicio de la ventana continua con más incidentes. Si hay empate, la que empieza más temprano."""
    return max(range(24), key=lambda i: (suma_ventana(horas, i, largo), -i))


def _horas_del_horario(inicio, fin):
    """Las horas (0 a 23) que cubre un horario [inicio, fin), que puede cruzar la medianoche."""
    if inicio <= fin:
        return set(range(inicio, fin))
    return set(range(inicio, 24)) | set(range(0, fin))


def histograma_horas(admin, ahora=None):
    """Incidentes por hora de Lima de los últimos 30 días, de toda la comunidad (no depende de los filtros de la pantalla)."""
    ahora = _ahora(ahora)
    p = {**_base(admin), "hoy": _hoy_lima(ahora), "ahora": ahora}
    with db.cursor() as cur:
        cur.execute(f"""SELECT EXTRACT(HOUR FROM {LOCAL})::int AS hora, count(*) AS n
                        FROM events e JOIN cameras c ON c.id = e.camera_id
                        WHERE {VISIBLE} AND e.community_id = %(comunidad)s AND e.status <> 'falsa alarma'
                          AND e.occurred_at >= ((%(hoy)s::date - 29)::timestamp) AT TIME ZONE 'America/Lima'
                          AND e.occurred_at <= %(ahora)s::timestamptz
                        GROUP BY 1""", p)
        filas = cur.fetchall()
    horas = [0] * 24
    for r in filas:
        horas[r["hora"]] = int(r["n"])
    return horas


def horario_actual(admin):
    """El horario sensible vigente de la comunidad: [inicio, fin] y si es el de fábrica (la comunidad no definió uno)."""
    with db.cursor() as cur:
        cur.execute("SELECT settings FROM communities WHERE id = %s", (str(admin["community_id"]),))
        fila = cur.fetchone()
    ajustes = fila["settings"] if fila and isinstance(fila["settings"], dict) else {}
    definido = sources._sensitive_hours(ajustes.get("sensitive_hours"))
    return (definido or list(HORARIO_DEFECTO)), definido is None


def horario(admin, ahora=None):
    """
    El horario sensible sugerido: la ventana continua de 6 horas con más incidentes de los últimos 30 días (sin falsas alarmas), con el
    porcentaje de incidentes que concentra, junto al horario vigente y qué porcentaje cubre. Con menos de 20 incidentes no sugiere nada.
    """
    horas = histograma_horas(admin, ahora)
    total = sum(horas)
    (a_ini, a_fin), por_defecto = horario_actual(admin)
    cubiertas = _horas_del_horario(a_ini, a_fin)
    en_actual = sum(horas[h] for h in cubiertas)
    actual = {"inicio": a_ini, "fin": a_fin, "por_defecto": por_defecto, "incidentes": en_actual,
              "porcentaje": round(en_actual * 100.0 / total) if total else 0}
    if total < MIN_PARA_SUGERIR:
        return {"estado": "sin_datos_suficientes", "total": total, "minimo": MIN_PARA_SUGERIR, "actual": actual, "sugerido": None, "igual": False}
    ini = ventana_mas_cargada(horas)
    n = suma_ventana(horas, ini)
    sugerido = {"inicio": ini, "fin": (ini + VENTANA_HORAS) % 24, "incidentes": n, "porcentaje": round(n * 100.0 / total)}
    return {"estado": "ok", "total": total, "minimo": MIN_PARA_SUGERIR, "actual": actual, "sugerido": sugerido,
            "igual": (a_ini, a_fin) == (sugerido["inicio"], sugerido["fin"])}


def aplicar_horario(admin, inicio, ip=None, ahora=None):
    """
    Aplica la sugerencia actual como horario sensible de la comunidad. El navegador manda la hora de inicio que vio; si desde entonces
    la sugerencia cambió, se rechaza (409): nunca se aplica algo que el administrador no vio. Queda en audit_log con el horario anterior.
    """
    if isinstance(inicio, bool) or not isinstance(inicio, int) or not 0 <= inicio <= 23:
        raise DashboardError(422, "inicio_invalido", "La hora de inicio debe ser un número entero de 0 a 23.")
    h = horario(admin, ahora)
    if h["estado"] != "ok":
        raise DashboardError(409, "sin_datos_suficientes", f"Todavía no hay datos suficientes para sugerir un horario (se necesitan {MIN_PARA_SUGERIR} incidentes en 30 días).")
    if inicio != h["sugerido"]["inicio"]:
        raise DashboardError(409, "sugerencia_cambio", "La sugerencia cambió desde que la viste. Actualiza el panel y revísala otra vez.")
    if h["igual"]:
        raise DashboardError(409, "ya_aplicado", "Ese horario ya es el vigente.")
    fin = h["sugerido"]["fin"]
    with db.cursor() as cur:
        cur.execute("SELECT settings FROM communities WHERE id = %s FOR UPDATE", (str(admin["community_id"]),))
        antes = cur.fetchone()["settings"]
        anterior = sources._sensitive_hours(antes.get("sensitive_hours")) if isinstance(antes, dict) else None
        cur.execute("""UPDATE communities
                       SET settings = jsonb_set(CASE WHEN jsonb_typeof(settings) = 'object' THEN settings ELSE '{}'::jsonb END,
                                                '{sensitive_hours}', %s::jsonb, true)
                       WHERE id = %s""", (json.dumps([inicio, fin]), str(admin["community_id"])))
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id, ip, meta)
                       VALUES ('user', %s, %s, 'apply_sensitive_hours', 'community', %s, %s, %s::jsonb)""",
                    (str(admin["id"]), str(admin["community_id"]), str(admin["community_id"]), ip,
                     json.dumps({"inicio": inicio, "fin": fin, "anterior": anterior})))
    return {"inicio": inicio, "fin": fin, "anterior": anterior}


def panel(admin, desde=None, hasta=None, camara=None, con_falsas=False, ahora=None):
    """Todo lo que muestra la pantalla, en un solo JSON."""
    ahora = _ahora(ahora)
    f = leer_filtros(admin, desde, hasta, camara, ahora)
    filas = contar_exportacion(admin, f)
    try:
        pendientes = hitl.pending_count(admin)
    except Exception:
        pendientes = None
    return {
        "generado_en": ahora.astimezone(LIMA).isoformat(timespec="seconds"),
        "rango": {"desde": f["desde"].isoformat(), "hasta": f["hasta"].isoformat(), "dias": (f["hasta"] - f["desde"]).days + 1},
        "camara": f["camara"],
        "tarjetas": tarjetas(admin, f["camara"], ahora),
        "camaras": camaras_en_linea(admin, f["camara"], ahora),
        "pendientes": pendientes,
        "serie_diaria": serie_diaria(admin, f),
        "calor": calor(admin, f, con_falsas),
        "ranking": ranking(admin, f),
        "senales": senales(admin, f),
        "equipos": equipos(admin, ahora),
        "horario": horario(admin, ahora),
        "opciones_camara": opciones_camara(admin),
        "exportacion": {"filas": filas, "limite": LIMITE_CSV, "recortada": filas > LIMITE_CSV},
    }


# ------------------------------------------------------------------ exportación a CSV

COLUMNAS_CSV = ["id", "fecha_hora_lima", "dia_semana", "hora", "camara", "lugar", "tipo_camara", "nivel", "puntaje", "personas", "estado",
                "senales", "revisado_en_lima", "segundos_de_revision", "escalado"]


def celda(valor):
    """
    Un texto para una celda de Excel. Si empieza con = + - @ (o con un tabulador o un retorno), Excel lo tomaría por una fórmula y la
    ejecutaría: se le antepone una comilla simple, que Excel muestra como texto.
    """
    if valor is None:
        return ""
    texto = str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


def comprobar_espera(admin, ahora_s=None):
    """Una exportación por administrador cada pocos segundos: el CSV es la consulta más pesada del panel."""
    ahora_s = time.monotonic() if ahora_s is None else ahora_s
    with _candado:
        antes = _ultima_exportacion.get(str(admin["id"]))
        if antes is not None and ahora_s - antes < ESPERA_CSV_S:
            raise DashboardError(429, "demasiado_pronto", f"Espera {ESPERA_CSV_S} segundos antes de exportar otra vez.")
        _ultima_exportacion[str(admin["id"])] = ahora_s
        if len(_ultima_exportacion) > 1000:
            for k in sorted(_ultima_exportacion, key=_ultima_exportacion.get)[:500]:
                del _ultima_exportacion[k]


def auditar_exportacion(admin, f, filas, ip=None):
    """Queda registrado quién exportó, qué rango y cuántas filas. No se guarda ningún dato de los eventos."""
    meta = {"desde": f["desde"].isoformat(), "hasta": f["hasta"].isoformat(), "camara": f["camara"], "filas": filas}
    with db.cursor() as cur:
        cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id, ip, meta)
                       VALUES ('user', %s, %s, 'export_events_csv', 'events', NULL, %s, %s::jsonb)""",
                    (str(admin["id"]), str(admin["community_id"]), ip, json.dumps(meta)))


def nombre_archivo(f):
    return f"eventos_{f['desde'].isoformat()}_a_{f['hasta'].isoformat()}.csv"


def _fila_csv(r):
    return [r["id"], r["fecha_hora_lima"], DIAS[r["dow"] - 1], r["hora"], celda(r["camara"]), celda(r["lugar"]),
            "común" if r["tipo"] == "common" else "privada", r["nivel"], r["puntaje"], r["personas"], celda(r["estado"]),
            celda(r["senales"]), r["revisado_en"] or "", "" if r["segundos_de_revision"] is None else r["segundos_de_revision"],
            "sí" if r["escalado"] else "no"]


def generar_csv(admin, f, limite=None):
    """
    El CSV por partes. UTF-8 con BOM (para que Excel muestre bien las tildes), comas y fin de línea de Windows. Lleva todas las
    cámaras visibles del rango, con su estado (la columna «estado» distingue las falsas alarmas). NO lleva nombres ni correos de los
    dueños, ni coordenadas, ni enlaces a fotos.
    """
    limite = LIMITE_CSV if limite is None else limite
    yield "\ufeff".encode("utf-8")
    cabecera = io.StringIO()
    csv.writer(cabecera, lineterminator="\r\n").writerow(COLUMNAS_CSV)
    yield cabecera.getvalue().encode("utf-8")
    sql = f"""
        SELECT e.id, to_char({LOCAL}, 'YYYY-MM-DD"T"HH24:MI:SS') || '-05:00' AS fecha_hora_lima,
               EXTRACT(ISODOW FROM {LOCAL})::int AS dow, EXTRACT(HOUR FROM {LOCAL})::int AS hora,
               c.name AS camara, c.place AS lugar, c.kind AS tipo, e.level AS nivel, e.score AS puntaje, e.people_count AS personas,
               e.status AS estado,
               (SELECT string_agg(s.description, '; ' ORDER BY s.code) FROM event_signals es
                  JOIN signals s ON s.code = es.signal_code WHERE es.event_id = e.id) AS senales,
               CASE WHEN e.reviewed_at IS NULL THEN NULL
                    ELSE to_char(e.reviewed_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD"T"HH24:MI:SS') || '-05:00' END AS revisado_en,
               CASE WHEN e.reviewed_at IS NULL THEN NULL
                    ELSE round(EXTRACT(EPOCH FROM (e.reviewed_at - e.occurred_at)))::bigint END AS segundos_de_revision,
               (e.escalated_at IS NOT NULL) AS escalado
        FROM events e JOIN cameras c ON c.id = e.camera_id
        WHERE {_donde(f, True)}
        ORDER BY e.occurred_at DESC, e.id DESC LIMIT %(limite)s"""
    with db.connection() as conn:
        with conn.cursor(name="export_eventos", cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.itersize = LOTE_CSV
            cur.execute(sql, {**_base(admin, f), "limite": limite})
            while True:
                lote = cur.fetchmany(LOTE_CSV)
                if not lote:
                    break
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\r\n")
                for r in lote:
                    w.writerow(_fila_csv(r))
                yield buf.getvalue().encode("utf-8")
