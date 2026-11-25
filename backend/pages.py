"""
HTML de las pantallas de cámaras (/cameras y /cameras/{id}). Todo lo que viene de la base de datos o de lo
que escribió un usuario se escapa antes de entrar a la página.
"""
import datetime
import html
import logging
import os
import re
from urllib.parse import urlencode

from backend import alerts, auth, cameras, hitl
from backend.suscripciones import fecha as suscripciones_fecha, soles as suscripciones_soles

log = logging.getLogger("pages")

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
SOURCE_LABELS = {"browser": "Cámara web", "video": "Video", "edge": "Mini PC"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fill(template, **values):
    """
    Sustituye {nombre} en UNA sola pasada. Lo que se inserta no se vuelve a interpretar: si un usuario
    se llamara «{cards}» o escribiera «{error}», no podría colar contenido en la página.
    """
    patron = r"\{(" + "|".join(re.escape(k) for k in values) + r")\}"
    return re.sub(patron, lambda m: values[m.group(1)], template)


def read_template(name) -> str:
    with open(os.path.join(TEMPLATES_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def role_text(user) -> str:
    from backend import acceso
    return acceso.nombre_rol(user)


def pending_link(user, active=None) -> str:
    """
    El enlace «Pendientes (n)» del menú, solo para administradores. Si la base de datos falla, la página sigue
    funcionando: el enlace aparece sin número.
    """
    if user.get("role") != "community_admin":
        return ""
    try:
        n = hitl.pending_count(user)
    except Exception:
        log.warning("No se pudo contar las alertas pendientes para el menú", exc_info=True)
        n = 0
    actual = ' aria-current="page"' if active == "pendientes" else ""
    return f'<a class="pestana" id="nav-pendientes" href="/admin/pendientes"{actual}>Pendientes{f" ({n})" if n else ""}</a>'


def equipos_link(user, active=None) -> str:
    """El enlace «Equipos» del menú, solo para administradores."""
    if user.get("role") != "community_admin":
        return ""
    actual = ' aria-current="page"' if active == "equipos" else ""
    return f'<a class="pestana" id="nav-equipos" href="/admin/equipos"{actual}>Equipos</a>'


def panel_link(user, active=None) -> str:
    """El enlace «Panel» del menú, solo para administradores."""
    if user.get("role") != "community_admin":
        return ""
    actual = ' aria-current="page"' if active == "panel" else ""
    return f'<a class="pestana" id="nav-panel" href="/admin"{actual}>Panel</a>'


def mapa_link(user, active=None) -> str:
    """El enlace «Mapa» del menú, solo para administradores."""
    if user.get("role") != "community_admin":
        return ""
    actual = ' aria-current="page"' if active == "mapa" else ""
    return f'<a class="pestana" id="nav-mapa" href="/admin/mapa"{actual}>Mapa</a>'


def admin_links(user, active=None) -> str:
    """Los enlaces que solo ve un administrador: Panel, Mapa, Pendientes (con su número) y Equipos."""
    return panel_link(user, active) + mapa_link(user, active) + pending_link(user, active) + equipos_link(user, active)


def menu_usuario(user, active) -> str:
    """
    El nombre de la persona abre un menú con lo de su cuenta: Mi plan (donde se paga, se baja de plan o se cancela), la seguridad de
    su cuenta y Cerrar sesión. Es un <details>: funciona sin JavaScript; un script pequeño lo cierra con Esc o al tocar fuera.
    """
    def enlace(href, texto, clave=None):
        actual = ' aria-current="page"' if clave and clave == active else ""
        return f'<a href="{href}"{actual}>{texto}</a>'
    nombre = (user["full_name"] or "").strip() or "Mi cuenta"
    return ('<details class="menu-usuario"><summary class="menu-usuario__boton">'
            f'<span class="menu-usuario__avatar" aria-hidden="true">{esc(nombre[:1].upper())}</span>'
            f'<span class="menu-usuario__texto"><span class="menu-usuario__nombre">{esc(nombre)}</span>'
            f'<span class="menu-usuario__rol">{role_text(user)}</span></span>'
            '<span class="menu-usuario__flecha" aria-hidden="true">▾</span><span class="solo-lectores">Menú de tu cuenta</span></summary>'
            '<div class="menu-usuario__panel">'
            f'<div class="menu-usuario__cabecera"><strong>{esc(nombre)}</strong><span>{role_text(user)}</span></div>'
            + (enlace("/plan", "Mi plan", "plan") if user.get("role") in ("resident", "community_admin") else "")
            + enlace("/cuenta/contrasena", "Cambiar contraseña", "cuenta")
            + enlace("/alerts#titulo-seguridad", "Seguridad de tu cuenta")
            + '<form method="post" action="/logout"><button class="menu-usuario__salir" type="submit">Cerrar sesión</button></form>'
            '</div></details>')


def nav(user, active) -> str:
    """
    La cabecera completa menos su marco: en la primera fila la marca, y a la derecha el usuario con «Cerrar sesión»; en la segunda, las
    pestañas. El vecino ve Cámaras y Alertas; el administrador, además, Panel, Mapa, Pendientes y Equipos.
    """
    def pestana(href, texto, clave):
        actual = ' aria-current="page"' if clave == active else ""
        return f'<a class="pestana" href="{href}"{actual}>{texto}</a>'
    return ('<div class="contenedor cabecera__fila"><div class="marca">'
            '<span class="marca__nombre pixel">INTIVISION</span><span class="marca__modulo">Guardian</span></div>'
            f'<div class="cabecera__usuario">{menu_usuario(user, active)}</div></div>'
            '<nav aria-label="Principal" class="cabecera__pestanas"><div class="contenedor cabecera__tabs">'
            + pestana("/cameras", "Cámaras", "camaras") + pestana("/alerts", "Alertas", "alertas")
            + admin_links(user, active)
            + '</div></nav>')


_ALERT_LABELS = {"error": "Error", "alerta": "Aviso", "ok": "Listo", "info": "Info"}


def alert_block(message, kind="error") -> str:
    if not message:
        return ""
    etiqueta = _ALERT_LABELS.get(kind, "Aviso")
    return (f'<p class="estado" data-tipo="{esc(kind)}" role="alert"><span class="estado__tag">{etiqueta}</span>'
            f'<span>{esc(message)}</span></p>')


def level_badge(level) -> str:
    clave = str(level).lower() if level in cameras.LEVELS else "otro"
    return f'<span class="nivel nivel--{clave}">{esc(level)}</span>'


def format_when(created_at) -> str:
    """'2026-11-03T22:26:10' (hora de Lima) -> '03/11/26 22:26'."""
    try:
        return datetime.datetime.fromisoformat(created_at).strftime("%d/%m/%y %H:%M")
    except (TypeError, ValueError):
        return esc(created_at)


def count_text(n, singular, plural) -> str:
    return f"{n} {singular if n == 1 else plural}"


# ------------------------------------------------------------------ tarjetas

def event_card(r, show_owner) -> str:
    """La misma tarjeta del historial del panel, ya con sus clases finales (esta pantalla no usa JavaScript)."""
    imagen = (f'<img class="evento__img" src="/evidence/{esc(r["image"])}" loading="lazy" '
              f'alt="Imagen de evidencia del evento">') if r["image"] else ""
    seg = f'en el seg. {r["video_time_s"]:g}' if r["video_time_s"] is not None else "En vivo"
    origen = SOURCE_LABELS.get(r["source"], r["source"])
    linea = ""
    if show_owner and r.get("owner_name"):
        linea = f'<div class="evento__camara">De: {esc(r["owner_name"])}</div>'
    return (f'<div class="evento{"" if r["image"] else " evento--sin-imagen"}" role="listitem">{imagen}'
            f'<div class="evento__cuerpo"><div class="evento__fila">{level_badge(r["level"])}'
            f'<span class="evento__puntaje">Puntaje {esc(r["score"])}</span></div>'
            f'<div class="evento__motivo">{esc(r["reasons"])}</div>'
            f'<div class="evento__meta"><span class="evento__origen">{esc(origen)}</span><span>{seg}</span>'
            f'<span>{format_when(r["created_at"])}</span></div>{linea}{ia_block(r)}{video_block(r)}{zip_link(r)}</div></div>')


def ia_block(r) -> str:
    """La descripción automática del evento, rotulada como tal (puede equivocarse)."""
    if not r.get("ai_description"):
        return ""
    aviso = ('<span class="evento__ia-aviso">La IA no ve personas: podría ser una falsa alarma.</span>'
             if r.get("ai_sees_people") is False else "")
    return (f'<p class="evento__ia"><span class="evento__ia-rotulo">Descripción automática · puede equivocarse</span>'
            f'{esc(r["ai_description"])}{aviso}</p>')


def zip_link(r) -> str:
    """Descarga de la evidencia con huellas (Plan Premium)."""
    if not r.get("premium") or not r.get("id"):
        return ""
    return f'<a class="evento__zip" href="/events/{esc(r["id"])}/evidencia.zip" download>Descargar evidencia (ZIP)</a>'


def video_block(r) -> str:
    """El clip del evento (Plan Premium), plegado para no cargarlo hasta que se pida."""
    if not r.get("clip"):
        return ""
    poster = f' poster="/evidence/{esc(r["image"])}"' if r.get("image") else ""
    return (f'<details class="evento__video"><summary>▶ Ver video del evento</summary>'
            f'<video controls preload="none" playsinline src="/evidence/{esc(r["clip"])}"{poster}></video></details>')


def camera_status(c) -> str:
    if c["is_active"]:
        return f'<span class="punto punto--activa"></span> Activa · {esc(cameras.ago(c["last_seen_at"]))}'
    return '<span class="punto"></span> Inactiva'


def ownership_text(c, user) -> str:
    if c["kind"] == "common":
        return "Zona común"
    if c["user_id"] != user["id"]:
        return f'De: {esc(c["owner_name"])}'
    return "Visible para el administrador" if c["admin_can_view"] else "Oculta al administrador"


def plan_badge(c) -> str:
    """La marca «Premium» de una cámara (las del plan Base no llevan marca)."""
    return '<span class="plan plan--premium" title="Clip de video, posibles armas y detección más precisa">Premium</span>' if c.get("premium") else ""


def camera_card(c, user) -> str:
    lugar = f'<p class="cam__lugar">{esc(c["place"])}</p>' if c["place"] else ""
    ultimo = level_badge(c["ultimo_nivel"]) if c["ultimo_nivel"] else ""
    return (f'<article class="cam" role="listitem"><div class="cam__fila">'
            f'<h3 class="cam__nombre">{esc(c["name"])}</h3><span class="cam__marcas">{plan_badge(c)}{ultimo}</span></div>{lugar}'
            f'<div class="cam__datos"><span>{esc(cameras.SOURCE_TEXT.get(c["source_type"], c["source_type"]))}</span>'
            f'<span>{camera_status(c)}</span>'
            f'<span>{count_text(c["hoy"], "evento hoy", "eventos hoy")} · {c["total"]} en total</span>'
            f'<span>{ownership_text(c, user)}</span></div>'
            f'<div class="cam__pie"><a class="btn" href="/cameras/{esc(c["id"])}">Abrir</a></div></article>')


# ------------------------------------------------------------------ pantallas

def camera_block(clave, titulo, cams, user, vacio, aviso="") -> str:
    """
    Una sección de la lista de cámaras: su título, cuántas hay activas y sus tarjetas (o el mensaje de vacío). Las cámaras archivadas
    no cuentan ni se mezclan con las activas: van en un desplegable al final, para no perder el acceso a su historial.
    """
    activas = [c for c in cams if c["is_active"]]
    archivadas = [c for c in cams if not c["is_active"]]
    grilla = f'<div class="camaras" role="list">{"".join(camera_card(c, user) for c in activas) if activas else vacio}</div>'
    desplegable = ""
    if archivadas:
        desplegable = (f'<details class="archivadas"><summary>Archivadas ({len(archivadas)})</summary>'
                       f'<div class="camaras" role="list">{"".join(camera_card(c, user) for c in archivadas)}</div></details>')
    return (f'<section class="seccion" aria-labelledby="titulo-{clave}"><div class="contenedor">{aviso}'
            f'<div class="historial__cabecera"><h2 class="historial__titulo" id="titulo-{clave}">'
            f'<span class="pixel">{titulo}</span></h2>'
            f'<span class="historial__total">{esc(count_text(len(activas), "cámara", "cámaras"))}</span></div>'
            f'{grilla}{desplegable}</div></section>')


def _vacio(titulo, texto) -> str:
    return f'<div class="vacio"><p class="vacio__titulo">{titulo}</p><p class="vacio__texto">{texto}</p></div>'


def render_cameras(user, cams, message=None) -> str:
    """
    El vecino ve solo «Mis cámaras». El administrador ve primero las registradas a su cuenta y, más abajo, todas las demás de la
    comunidad que puede ver; cada cámara sale en un solo bloque.
    """
    admin = user["role"] == "community_admin"
    aviso = alert_block(message)
    if admin:
        mias = [c for c in cams if c["user_id"] == user["id"]]
        otras = [c for c in cams if c["user_id"] != user["id"]]
        bloques = (camera_block("mias", "Mis cámaras", mias, user,
                                _vacio("Ninguna cámara está a tu nombre", "Las cámaras de la comunidad están más abajo."), aviso)
                   + camera_block("comunidad", "Cámaras de la comunidad", otras, user,
                                  _vacio("Todavía no hay otras cámaras en la comunidad",
                                         'Asígnalas desde <a href="/admin/equipos">Equipos y cámaras</a>.')))
    else:
        bloques = camera_block("mias", "Mis cámaras", cams, user,
                               _vacio("Aún no tienes cámaras", "Tu administrador te asignará una desde el panel de equipos y cámaras."), aviso)
    return fill(
        read_template("camaras.html"),
        nav=nav(user, "camaras"),
        titulo="Cámaras" if admin else "Mis cámaras",
        descripcion=("Primero las que están a tu nombre y, más abajo, las de la comunidad que puedes ver. Las de los vecinos "
                     "aparecen mientras su dueño no te las oculte." if admin else
                     "Las cámaras que tu administrador te asignó, cada una con su historial de eventos."),
        bloques=bloques)


def filter_link(cam_id, nivel, desde, hasta, limite) -> str:
    params = {k: v for k, v in (("nivel", nivel), ("desde", desde), ("hasta", hasta)) if v}
    params["limite"] = limite
    return f"/cameras/{cam_id}?{urlencode(params)}"


def live_block(cam, nonce) -> str:
    """El visor en vivo de la ficha. Las cámaras del navegador no tienen mini PC: no llevan visor."""
    if cam.get("source_type") == "browser":
        return ""
    return fill(read_template("vivo.html"), nonce=esc(nonce),
                script=fill(read_template("vivo.js"), camera_id=esc(cam["id"])))


def render_camera(user, cam, events, nivel, desde, hasta, limite, has_more, message=None, nonce="", ubicacion=None) -> str:
    es_dueno = cam["user_id"] == user["id"]
    admin = user["role"] == "community_admin"
    filtrando = bool(nivel or desde or hasta)

    resumen = "".join(
        f'<div class="resumen__dato"><span class="resumen__valor pixel">{valor}</span>'
        f'<span class="resumen__etiqueta">{etiqueta}</span></div>'
        for valor, etiqueta in ((cam["hoy"], "Hoy"), (cam["semana"], "7 días"), (cam["altos"], "Nivel alto")))

    datos = (f'<div class="ficha"><span class="ficha__clave">Tipo</span><span>{esc(cameras.SOURCE_TEXT.get(cam["source_type"], cam["source_type"]))}</span>'
             f'<span class="ficha__clave">Estado</span><span>{camera_status(cam)}</span>'
             f'<span class="ficha__clave">Dueño</span><span>{"Zona común" if cam["kind"] == "common" else esc(cam["owner_name"])}</span>'
             f'<span class="ficha__clave">Eventos</span><span>{cam["total"]} en total</span>'
             f'<span class="ficha__clave">Plan</span><span>{"Premium" if cam.get("premium") else "Base"}</span></div>')

    if es_dueno and cam["kind"] == "private":
        visible = cam["admin_can_view"]
        visibilidad = (f'<form class="visibilidad" method="post" action="/cameras/{esc(cam["id"])}/visibilidad">'
                       f'<p class="visibilidad__texto">El administrador de la comunidad '
                       f'<strong>{"puede" if visible else "no puede"}</strong> ver esta cámara y sus eventos.</p>'
                       f'<input type="hidden" name="visible" value="{0 if visible else 1}">'
                       f'<button class="btn btn--texto" type="submit">'
                       f'{"Ocultar al administrador" if visible else "Permitir al administrador"}</button></form>')
    elif admin and cam["kind"] == "private":
        visibilidad = ('<p class="visibilidad__texto">Estás viendo la cámara de otra persona. '
                       'Este acceso queda registrado.</p>')
    else:
        visibilidad = ""

    opciones = "".join(f'<option value="{n}"{" selected" if n == nivel else ""}>{n.capitalize()}</option>'
                       for n in cameras.LEVELS)
    filtros = (f'<form class="filtros" method="get" action="/cameras/{esc(cam["id"])}">'
               f'<div class="campo"><label class="campo__etiqueta" for="f-nivel">Nivel</label>'
               f'<select class="campo__entrada" id="f-nivel" name="nivel"><option value="">Todos</option>{opciones}</select></div>'
               f'<div class="campo"><label class="campo__etiqueta" for="f-desde">Desde</label>'
               f'<input class="campo__entrada" id="f-desde" name="desde" type="date" value="{esc(desde)}"></div>'
               f'<div class="campo"><label class="campo__etiqueta" for="f-hasta">Hasta</label>'
               f'<input class="campo__entrada" id="f-hasta" name="hasta" type="date" value="{esc(hasta)}"></div>'
               f'<div class="filtros__acciones"><button class="btn" type="submit">Filtrar</button>'
               + (f'<a class="btn btn--texto" href="/cameras/{esc(cam["id"])}">Quitar filtros</a>' if filtrando else "")
               + '</div></form>')

    if events:
        lista = "".join(event_card(e, False) for e in events)
    else:
        lista = ('<div class="vacio"><p class="vacio__titulo">'
                 + ("Ningún evento coincide con el filtro" if filtrando else "Esta cámara aún no tiene eventos")
                 + '</p><p class="vacio__texto">'
                 + ("Prueba con otro nivel o con otras fechas." if filtrando else
                    "Cuando elijas esta cámara en «Analizar» y se detecte algo, aparecerá aquí.")
                 + '</p></div>')
    mas = ""
    if has_more:
        mas = (f'<div class="mas"><a class="btn" href="{esc(filter_link(cam["id"], nivel, desde, hasta, limite + 12))}">'
               f'Ver más eventos</a></div>')

    return fill(
        read_template("camara.html"),
        titulo_pagina=esc(cam["name"]), nav=nav(user, "camaras"),
        nombre=esc(cam["name"]), lugar=esc(cam["place"]) if cam["place"] else "Sin lugar indicado.",
        aviso=alert_block(message, "alerta"), resumen=resumen, datos=datos, visibilidad=visibilidad,
        filtros=filtros, total=esc(count_text(len(events), "evento", "eventos")) + ("+" if has_more else ""),
        eventos=lista, mas=mas, vivo=live_block(cam, nonce), ubicacion=ubicacion_block(cam, ubicacion, nonce))


# ------------------------------------------------------------------ alertas por Telegram

_DELIVERY_TEXT = {"sent": ("Enviado", "ok"), "pending": ("Pendiente", "info"), "failed": ("No se pudo enviar", "error")}


def delivery_row(r) -> str:
    texto, tipo = _DELIVERY_TEXT.get(r["status"], ("—", "info"))
    detalle = f' title="{esc(r["last_error"])}"' if r["last_error"] else ""
    return (f'<div class="aviso">{level_badge(r["level"])}<span class="aviso__camara">{esc(r["camera_name"] or "Sin cámara")}</span>'
            f'<span class="aviso__fecha">{esc(r["cuando"])}</span>'
            f'<span class="aviso__estado aviso__estado--{tipo}"{detalle}>{texto}</span></div>')


def render_pending(user, nonce) -> str:
    """La pantalla «Pendientes de revisión» (H9). Las alertas las pide el navegador a /admin/review/pending."""
    return fill(read_template("pendientes.html"), nav=nav(user, "pendientes"), nonce=esc(nonce),
                script=read_template("pendientes.js"))


def render_panel(user, nonce) -> str:
    """El panel de incidentes (H8). Los datos los pide el navegador a /admin/panel/datos."""
    return fill(read_template("panel.html"), nav=nav(user, "panel"), nonce=esc(nonce), script=read_template("panel.js"))


def render_mapa(user, nonce) -> str:
    """El mapa de calor por cámara (H8). Los datos los pide el navegador a /admin/mapa/datos."""
    return fill(read_template("mapa.html"), nav=nav(user, "mapa"), nonce=esc(nonce),
                script=read_template("coordenadas.js") + "\n" + read_template("mapa.js"))


def ubicacion_block(cam, ubic, nonce) -> str:
    """La sección «Ubicación de la cámara» de la ficha. Solo la ve el dueño de una cámara privada (ubic es None para los demás)."""
    if not ubic:
        return ""
    def num(v):
        return "" if v is None else repr(float(v))
    centro = ubic["centro"]
    atributos = (f'data-camara="{esc(cam["id"])}" data-lat="{num(ubic["lat"])}" data-lng="{num(ubic["lng"])}" '
                 f'data-precision="{"" if ubic["precision_m"] is None else int(ubic["precision_m"])}" '
                 f'data-visible="{1 if ubic["visible_en_mapa"] else 0}" data-admin-ve="{1 if ubic["admin_puede_ver"] else 0}" '
                 f'data-fijada="{esc(ubic["fijada_en"] or "")}" data-centro-lat="{num(centro["lat"])}" data-centro-lng="{num(centro["lng"])}" '
                 f'data-centro-definido="{1 if centro["definido"] else 0}"')
    return fill(read_template("ubicacion.html"), atributos=atributos, nonce=esc(nonce),
                script=read_template("coordenadas.js") + "\n" + read_template("ubicacion.js"))


def render_equipos(user, nonce) -> str:
    """La pantalla «Equipos y cámaras» (H5). Los datos los pide el navegador a /admin/equipos/datos."""
    return fill(read_template("equipos.html"), nav=nav(user, "equipos"), nonce=esc(nonce),
                script=read_template("equipos.js"))


def _linked_panel(channel) -> str:
    activo = channel["is_active"]
    estado = ('<span class="punto punto--activa"></span> Vinculado · alertas activas' if activo
              else '<span class="punto"></span> Vinculado · alertas en pausa')

    def radio(valor, texto):
        marcado = " checked" if channel["min_level"] == valor else ""
        return f'<label class="opcion"><input type="radio" name="min_level" value="{valor}"{marcado}> {texto}</label>'
    return (f'<form class="canal" method="post" action="/alerts/settings">'
            f'<p class="canal__estado">{estado}</p>'
            f'<fieldset class="opciones"><legend class="campo__etiqueta">Avisarme desde el nivel</legend>'
            f'{radio("MEDIO", "Medio y alto")}{radio("ALTO", "Solo alto")}</fieldset>'
            f'<label class="opcion"><input type="checkbox" name="active" value="1"{" checked" if activo else ""}> Alertas activas</label>'
            f'<div><button class="btn" type="submit">Guardar</button></div></form>'
            f'<div class="canal__acciones">'
            f'<form method="post" action="/alerts/test"><button class="btn btn--texto" type="submit">Enviar mensaje de prueba</button></form>'
            f'<form method="post" action="/alerts/unlink"><button class="btn btn--texto" type="submit">Desvincular</button></form></div>'
            f'<p class="zona__ayuda">Desvincular detiene los avisos nuevos; los mensajes que ya llegaron siguen en tu Telegram.</p>')


def duration_text(segundos) -> str:
    """'30 minutos', '12 horas', '7 días': la duración con la unidad que se lee mejor."""
    for unidad, tam, singular, plural in (("d", 86400, "día", "días"), ("h", 3600, "hora", "horas"), ("m", 60, "minuto", "minutos")):
        if segundos >= tam and (segundos / tam) == int(segundos / tam):
            n = int(segundos / tam)
            return f"{n} {singular if n == 1 else plural}"
    n = max(1, round(segundos / 60))
    return f"{n} {'minuto' if n == 1 else 'minutos'}"


def security_block(user) -> str:
    """La política de sesión de su rol, dicha en simple, y el botón para cerrar la sesión en todos sus dispositivos."""
    idle, maximo = auth.session_policy(user["role"])
    return ('<p class="analisis__descripcion">'
            f'Tu sesión se cierra sola después de {duration_text(idle)} sin actividad y, en todo caso, a los {duration_text(maximo)} de haber entrado. '
            'Si perdiste un celular o usaste una computadora ajena, ciérrala en todos tus dispositivos.</p>'
            '<form method="post" action="/logout/todos"><button class="btn" type="submit">Cerrar sesión en todos mis dispositivos</button></form>')


def render_alerts(user, channel, configured, recent, notice=None, link=None) -> str:
    if not configured:
        panel = alert_block("Telegram no está configurado en este servidor: falta definir TELEGRAM_TOKEN.", "alerta")
    elif link:
        panel = (f'<div class="canal"><p class="canal__texto">Abre este enlace en tu celular y pulsa <strong>Iniciar</strong> '
                 f'en Telegram. Vale {alerts.CODE_MINUTES} minutos y se usa una sola vez.</p>'
                 f'<div><a class="btn" href="{esc(link)}">Abrir Telegram</a></div>'
                 f'<p class="canal__enlace">{esc(link)}</p>'
                 f'<div><a class="btn btn--texto" href="/alerts">Ya lo abrí, actualizar</a></div></div>')
    elif channel and channel["verified_at"]:
        panel = _linked_panel(channel)
    else:
        pendiente = ""
        if channel and channel["link_pending"]:
            pendiente = (f'<p class="canal__texto">Hay un enlace pendiente (vence en {channel["link_minutes"]} min). '
                         f'Si lo perdiste, genera otro.</p>')
        panel = ('<div class="canal"><p class="canal__texto">Recibe en tu celular una foto cada vez que se detecte algo en '
                 'tus cámaras. Los avisos de una cámara los recibe solo su dueño.</p>' + pendiente +
                 '<form method="post" action="/alerts/link"><button class="btn" type="submit">'
                 + ('Generar otro enlace' if pendiente else 'Vincular Telegram') + '</button></form></div>')
    if recent:
        avisos = "".join(delivery_row(r) for r in recent)
    else:
        avisos = ('<div class="vacio"><p class="vacio__titulo">Aún no hay avisos</p>'
                  '<p class="vacio__texto">Cuando se detecte algo en tus cámaras, aquí verás si el aviso se envió.</p></div>')
    return fill(
        read_template("alertas.html"),
        nav=nav(user, "alertas"),
        descripcion=("Vincula tu Telegram para recibir en tu celular la foto y los datos de cada alerta. "
                     "Elige desde qué nivel quieres que te avisemos."),
        aviso=alert_block(notice[1], notice[0]) if notice else "", panel=panel, seguridad=security_block(user),
        total=esc(count_text(len(recent), "aviso", "avisos")) if recent else "", avisos=avisos)


# ------------------------------------------------------------------ Mi plan

def _beneficios(p, comunidad=False) -> str:
    if p.get("code") == "premium":
        items = [f"Clip de video de {p.get('clip_seconds') or 20} s en Telegram" if p.get("highlight_clips") else "",
                 "Detección de posibles armas" if p.get("weapon_detection") else "",
                 "Detección más precisa" if p.get("high_precision") else "",
                 f"Fotos y clips por {p.get('retention_days')} días",
                 "Resumen semanal por Telegram" if p.get("weekly_report") else "",
                 "Descarga de evidencia con huella SHA-256" if p.get("evidence_export") else ""]
        items.insert(0, "Todas las cámaras de la comunidad" if comunidad else "Tus cámaras privadas")
    else:
        items = ["Detección de personas y posturas de riesgo", "Alertas con foto por Telegram", "Revisión del administrador",
                 f"Fotos por {p.get('retention_days') or 7} días"]
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items if i) + "</ul>"


def plan_estado(est) -> str:
    """El recuadro de arriba: qué plan tiene hoy esta persona y qué va a pasar."""
    v = est["vivienda"]
    if est["vivienda_premium"]:
        lineas = [f'<strong>Premium de tu vivienda</strong>',
                  f'<span>{esc(suscripciones_soles(est["premium"]["price_pen_cents"]))} al mes'
                  + (f' · tarjeta {esc(v["card_brand"])} •••• {esc(v["card_last4"])}' if v and v.get("card_last4") else "")
                  + (' · se cobra con Mercado Pago' if v and v.get("provider") == "mercadopago" else "") + '</span>']
        if v["scheduled_action"] == "dar_de_baja":
            lineas.append(f'<span>Diste de baja el servicio: tu cuenta se cierra el {esc(suscripciones_fecha(v["current_period_end"]))}.</span>')
        elif v["cancel_at_period_end"]:
            lineas.append(f'<span>Vuelves al Plan Base el {esc(suscripciones_fecha(v["current_period_end"]))}. Hasta entonces sigues con Premium.</span>')
        elif v["status"] == "past_due":
            lineas.append(f'<span class="estado-pago--falla">No pudimos cobrar la renovación. Reintentaremos; si no se cobra, volverás al Plan Base.</span>')
        else:
            lineas.append(f'<span>Se renueva el {esc(suscripciones_fecha(v["current_period_end"]))}.</span>')
    elif est["comunidad_premium"]:
        lineas = ['<strong>Premium de tu comunidad</strong>', '<span>Tu comunidad tiene el Plan Premium: tus cámaras lo incluyen.</span>']
    else:
        lineas = ['<strong>Plan Base</strong>', '<span>Incluido en la cuota de tu comunidad.</span>']
    return '<div class="estado-plan">' + "".join(lineas) + "</div>"


def plan_tarjetas(est) -> str:
    base, prem = est["base"], est["premium"]
    actual_base = not est["vivienda_premium"] and not est["comunidad_premium"]
    tarjetas = [
        f'<article class="plan-tarjeta{" plan-tarjeta--actual" if actual_base else ""}"><h3 class="plan-tarjeta__nombre">Plan Base</h3>'
        f'<p class="plan-tarjeta__precio">{esc(suscripciones_soles(base.get("price_pen_cents")))} <small>por vivienda al mes</small></p>'
        f'<p class="plan-tarjeta__nota">Lo cobra tu junta vecinal en la cuota de la comunidad.</p>{_beneficios(base)}{plan_boton_base(est)}</article>',
        f'<article class="plan-tarjeta{" plan-tarjeta--actual" if est["vivienda_premium"] else ""}"><h3 class="plan-tarjeta__nombre">Premium · vivienda</h3>'
        f'<p class="plan-tarjeta__precio">{esc(suscripciones_soles(prem.get("price_pen_cents")))} <small>al mes</small></p>'
        f'<p class="plan-tarjeta__nota">Lo pagas tú; vale para tus cámaras privadas. Cancela cuando quieras.</p>'
        f'{_beneficios(prem)}{plan_boton_premium(est)}</article>']
    if est["es_admin"]:
        tarjetas.append(
            f'<article class="plan-tarjeta{" plan-tarjeta--actual" if est["comunidad_premium"] else ""}"><h3 class="plan-tarjeta__nombre">Premium · comunidad</h3>'
            f'<p class="plan-tarjeta__precio">{esc(suscripciones_soles(prem.get("community_price_pen_cents")))} <small>al mes</small></p>'
            f'<p class="plan-tarjeta__nota">Lo paga la junta; vale para todas las cámaras de la comunidad.</p>'
            f'{_beneficios(prem, comunidad=True)}{plan_boton_comunidad(est)}</article>')
    return "".join(tarjetas)


def plan_pagos(est) -> str:
    if not est["pagos"]:
        return '<div class="vacio"><p class="vacio__titulo">Aún no hay pagos</p><p class="vacio__texto">Aquí verás cada cobro y su comprobante.</p></div>'
    filas = []
    for p in est["pagos"]:
        ok = p["status"] == "succeeded"
        estado = "Pagado" if ok else ("Rechazado" if p["status"] == "failed" else "Pendiente")
        detalle = esc(p["failure_message"] or "") if not ok else plan_comprobante_link(p)
        filas.append(f'<tr><td>{esc(suscripciones_fecha(p["created_at"]))}</td>'
                     f'<td>{"Premium comunidad" if p["scope"] == "comunidad" else "Premium vivienda"} · {"renovación" if p["kind"] == "renovacion" else "alta"}</td>'
                     f'<td>{esc(suscripciones_soles(p["amount_cents"]))}</td><td>{esc(p["card_brand"] or "")}{(" •••• " + esc(p["card_last4"])) if p.get("card_last4") else ""}</td>'
                     f'<td class="{"estado-pago--ok" if ok else "estado-pago--falla"}">{estado}</td><td>{detalle}</td></tr>')
    return ('<div class="pagos__tabla"><table class="pagos"><thead><tr><th>Fecha</th><th>Concepto</th><th>Monto</th><th>Tarjeta</th>'
            '<th>Estado</th><th></th></tr></thead><tbody>' + "".join(filas) + '</tbody></table></div>')


# Ganchos que completan los PR de pagos y de cambios de plan
def plan_boton_base(est) -> str:
    v = est["vivienda"]
    if not est["vivienda_premium"]:
        return ""
    if v["scheduled_action"] in ("bajar_a_base", "dar_de_baja"):
        return ('<form method="post" action="/plan/reactivar"><button class="btn" type="submit">Seguir con Premium</button></form>')
    return ('<form method="post" action="/plan/cancelar"><button class="btn btn--texto" type="submit">Bajar a Base al final del período</button></form>'
            f'<p class="plan-tarjeta__nota">Sigues con Premium hasta el {esc(suscripciones_fecha(v["current_period_end"]))}. No hay reembolso del mes en curso.</p>')


def plan_baja(est) -> str:
    return ('<section class="seccion" aria-labelledby="titulo-baja"><div class="contenedor"><div class="historial__cabecera">'
            '<h2 class="historial__titulo" id="titulo-baja"><span class="pixel">Darte de baja</span></h2></div>'
            '<div class="zona-peligro"><p>Si dejas INTIVISION:</p><ul>'
            '<li>Tus cámaras privadas se desasignan y quedan en «Equipos y cámaras» para que el administrador decida qué hacer.</li>'
            '<li>Dejas de pertenecer a la comunidad: se cierra tu cuenta y todas tus sesiones.</li>'
            '<li>Se desvincula tu Telegram y se borran tus tarjetas guardadas.</li>'
            '<li>Tus eventos se conservan el tiempo de retención de tu plan y luego se borran.</li>'
            '<li>Si tienes Premium pagado, la baja ocurre al final de ese período y puedes deshacerla hasta entonces.</li></ul>'
            '<form method="post" action="/plan/baja" class="estado-plan"><label for="confirmacion">Escribe <strong>DAR DE BAJA</strong> para confirmar</label>'
            '<input type="text" id="confirmacion" name="confirmacion" autocomplete="off" required>'
            '<button class="btn btn--peligro" type="submit">Darme de baja</button></form></div></div></section>')


def plan_boton_premium(est) -> str:
    if est["vivienda_premium"]:
        return ""
    if est["comunidad_premium"]:
        return '<p class="plan-tarjeta__nota">Ya lo tienes por el plan de tu comunidad.</p>'
    return ('<form method="post" action="/plan/checkout"><input type="hidden" name="scope" value="vivienda">'
            '<button class="btn" type="submit">Pasar a Premium</button></form>')


def plan_boton_comunidad(est) -> str:
    c = est["comunidad"]
    if c:
        if c["cancel_at_period_end"]:
            return (f'<p class="plan-tarjeta__nota">Termina el {esc(suscripciones_fecha(c["current_period_end"]))}.</p>'
                    '<form method="post" action="/plan/comunidad/reactivar"><button class="btn" type="submit">Seguir con Premium</button></form>')
        return (f'<p class="plan-tarjeta__nota">Activo · se renueva el {esc(suscripciones_fecha(c["current_period_end"]))}'
                + (f' · {esc(c["card_brand"])} •••• {esc(c["card_last4"])}' if c.get("card_last4") else "") + '</p>'
                '<form method="post" action="/plan/comunidad/cancelar"><button class="btn btn--texto" type="submit">Bajar a Base al final del período</button></form>')
    return ('<form method="post" action="/plan/checkout"><input type="hidden" name="scope" value="comunidad">'
            '<button class="btn" type="submit">Activar Premium para la comunidad</button></form>')


def plan_comprobante_link(p) -> str:
    return f'<a href="/pagos/comprobante/{esc(p["id"])}">Comprobante {esc(p["receipt_number"] or "")}</a>' if p.get("receipt_number") else ""


# ------------------------------------------------------------------ pasarela simulada (páginas propias, como Stripe Checkout)

_PRUEBA = ('<details class="tarjetas-prueba"><summary>Tarjetas de prueba</summary><p>'
           '<code>4242 4242 4242 4242</code> pago aprobado · <code>4000 0027 6000 3184</code> pide autenticación del banco · '
           '<code>4000 0000 0000 0002</code> rechazada · <code>4000 0000 0000 9995</code> sin fondos · '
           '<code>4000 0000 0000 0341</code> aprobada, pero fallará la renovación. '
           'Cualquier fecha futura, cualquier CVC de 3 dígitos y cualquier nombre.</p></details>')


def _resumen(s, alcance) -> str:
    total = int(s["amount_cents"]); base = round(total / 1.18)
    return (f'<section class="resumen"><a href="/plan">← Volver a Mi plan</a><p class="resumen__producto">INTIVISION · Plan Premium {alcance}</p>'
            f'<p class="resumen__monto">{esc(suscripciones_soles(total))}</p><p class="resumen__detalle">al mes · se renueva cada 30 días · cancela cuando quieras</p>'
            f'<div class="lineas"><div><span>Subtotal</span><span>{esc(suscripciones_soles(base))}</span></div>'
            f'<div><span>IGV (18 %)</span><span>{esc(suscripciones_soles(total - base))}</span></div>'
            f'<div><strong>Total hoy</strong><strong>{esc(suscripciones_soles(total))}</strong></div></div></section>')


def render_checkout(s, error=None, nombre="") -> str:
    alcance = "de la comunidad" if s["scope"] == "comunidad" else "de tu vivienda"
    formulario = (f'<section class="caja"><h2>Pagar con tarjeta</h2>'
                  + (f'<p class="error" role="alert">{esc(error)}</p>' if error else "") +
                  f'<form method="post" action="/pagos/checkout/{esc(s["id"])}" autocomplete="off" class="caja" style="padding:0;border:0;box-shadow:none">'
                  '<label>Número de tarjeta<input name="numero" inputmode="numeric" autocomplete="cc-number" placeholder="1234 1234 1234 1234" required maxlength="23"></label>'
                  '<div class="fila"><label>Vence<input name="vence" placeholder="MM/AA" autocomplete="cc-exp" required maxlength="7"></label>'
                  '<label>CVC<input name="cvc" inputmode="numeric" autocomplete="cc-csc" placeholder="123" required maxlength="4"></label></div>'
                  f'<label>Nombre en la tarjeta<input name="nombre" autocomplete="cc-name" required maxlength="80" value="{esc(nombre)}"></label>'
                  f'<button class="boton" type="submit">Pagar {esc(suscripciones_soles(s["amount_cents"]))}</button></form>'
                  '<p class="nota">Tu tarjeta se guarda como un token para la renovación mensual: solo vemos la marca y los últimos 4 dígitos.</p>'
                  + _PRUEBA + '</section>')
    return fill(read_template("pago.html"), titulo="Pagar Premium", contenido=_resumen(s, alcance) + formulario)


def render_3ds(pid, pago) -> str:
    caja = (f'<section class="resumen"><p class="resumen__producto">Banco emisor (simulado) · 3-D Secure</p>'
            f'<p class="resumen__monto">{esc(suscripciones_soles(pago["amount_cents"]))}</p>'
            f'<p class="resumen__detalle">Tarjeta {esc(pago["card_brand"])} •••• {esc(pago["card_last4"])} · comercio: INTIVISION</p></section>'
            f'<section class="caja"><h2>Confirma tu compra</h2><p class="nota">Tu banco pide que confirmes este pago. En la vida real llegaría '
            f'un código o una notificación a tu celular.</p>'
            f'<form method="post" action="/pagos/3ds/{esc(pid)}"><input type="hidden" name="decision" value="autorizar">'
            f'<button class="boton" type="submit" style="width:100%">Autorizar el pago</button></form>'
            f'<form method="post" action="/pagos/3ds/{esc(pid)}"><input type="hidden" name="decision" value="rechazar">'
            f'<button class="boton boton--secundario" type="submit" style="width:100%">No reconozco este pago</button></form></section>')
    return fill(read_template("pago.html"), titulo="Autenticación del banco", contenido=caja)


def render_comprobante(c) -> str:
    total = int(c["amount_cents"]); base = round(total / 1.18)
    concepto = "Plan Premium de la comunidad" if c["scope"] == "comunidad" else "Plan Premium de la vivienda"
    caja = (f'<section class="caja" style="grid-column: 1 / -1"><h2>Boleta de venta electrónica {esc(c["receipt_number"])}</h2>'
            + ('<p class="nota">Comprobante interno de INTIVISION; el cobro lo procesó Mercado Pago. No tiene valor tributario.</p>'
               if c.get("provider") == "mercadopago" else
               '<p class="nota">Documento de PRUEBA emitido por la pasarela simulada; no tiene valor tributario.</p>') +
            f'<table><tr><td>Emisor</td><td>INTIVISION (proyecto académico)</td></tr>'
            f'<tr><td>Cliente</td><td>{esc(c["full_name"])} · {esc(c["contacto"])}</td></tr>'
            f'<tr><td>Comunidad</td><td>{esc(c["comunidad"])}</td></tr>'
            f'<tr><td>Fecha</td><td>{esc(suscripciones_fecha(c["paid_at"]))}</td></tr>'
            f'<tr><td>Concepto</td><td>{concepto} · {"renovación" if c["kind"] == "renovacion" else "alta"} · 30 días</td></tr>'
            f'<tr><td>Medio de pago</td><td>{esc(c["card_brand"])}{(" •••• " + esc(c["card_last4"])) if c.get("card_last4") else ""}</td></tr>'
            f'<tr><td>Operación</td><td>{esc(c["id"])}</td></tr>'
            f'<tr><td>Valor de venta</td><td>{esc(suscripciones_soles(base))}</td></tr>'
            f'<tr><td>IGV (18 %)</td><td>{esc(suscripciones_soles(total - base))}</td></tr>'
            f'<tr><td><strong>Importe total</strong></td><td><strong>{esc(suscripciones_soles(total))}</strong></td></tr></table>'
            f'<p class="no-imprimir"><a href="/plan">← Volver a Mi plan</a></p></section>')
    return fill(read_template("pago.html"), titulo=f"Comprobante {c['receipt_number']}", contenido=caja)


def render_plan(user, est, notice=None) -> str:
    return fill(read_template("plan.html"), nav=nav(user, "plan"),
                descripcion="Tu plan, lo que incluye y tus pagos. El Plan Premium suma clip de video, posibles armas y más.",
                aviso=alert_block(notice[1], notice[0]) if notice else "", estado=plan_estado(est), planes=plan_tarjetas(est),
                pagos=plan_pagos(est), baja=plan_baja(est))



def render_mp_ir(s, url) -> str:
    """Paso intermedio hacia Mercado Pago: resumen y botón (y redirección automática a los 2 segundos)."""
    alcance = "de la comunidad" if s["scope"] == "comunidad" else "de tu vivienda"
    caja = (f'<section class="caja"><h2>Continúa en Mercado Pago</h2>'
            f'<p class="nota">Vas a autorizar un cobro mensual de {esc(suscripciones_soles(s["amount_cents"]))}. Tus datos de pago los '
            f'recibe solo Mercado Pago; INTIVISION nunca los ve. Puedes cancelar cuando quieras desde «Mi plan».</p>'
            f'<a class="boton" style="display:grid;place-items:center;text-decoration:none" href="{esc(url)}" rel="noopener">Ir a Mercado Pago</a>'
            f'<p class="nota"><a href="/plan">Cancelar y volver</a></p></section>')
    pagina = fill(read_template("pago.html"), titulo="Pagar con Mercado Pago", contenido=_resumen(s, alcance) + caja)
    pagina = pagina.replace("Modo de prueba · pasarela de pagos simulada · no se cobra dinero real",
                            "Pago seguro con Mercado Pago · si usas credenciales de prueba no se cobra dinero real")
    return pagina.replace("</head>", f'<meta http-equiv="refresh" content="2;url={esc(url)}">\n</head>', 1)



# ------------------------------------------------------------------ contraseña: cambiar y recuperar

def _campo_clave(id_, etiqueta, auto, minimo=False) -> str:
    return (f'<div class="campo"><label class="campo__etiqueta" for="{id_}">{etiqueta}</label>'
            f'<input class="campo__entrada" id="{id_}" name="{id_}" type="password" autocomplete="{auto}" maxlength="1024"'
            + (f' minlength="{auth.MIN_PASSWORD_CHARS}"' if minimo else "") + ' required></div>')


def _reglas_clave() -> str:
    return (f'<p class="analisis__descripcion">Mínimo {auth.MIN_PASSWORD_CHARS} caracteres, sin tu nombre, tu correo ni tu teléfono, '
            'y que no sea de las más comunes.</p>')


def render_cuenta_contrasena(user, notice=None) -> str:
    formulario = ('<form method="post" action="/cuenta/contrasena" autocomplete="off" class="formulario">'
                  + _campo_clave("actual", "Contraseña actual", "current-password")
                  + _campo_clave("nueva", "Contraseña nueva", "new-password", minimo=True)
                  + _campo_clave("confirmacion", "Repite la contraseña nueva", "new-password", minimo=True)
                  + _reglas_clave() + '<div><button class="btn" type="submit">Cambiar contraseña</button></div></form>')
    return fill(read_template("cuenta.html"), nav=nav(user, "cuenta"),
                descripcion="Al cambiarla cerramos tus sesiones en los demás dispositivos; esta sigue abierta y te avisamos por Telegram.",
                aviso=alert_block(notice[1], notice[0]) if notice else "", formulario=formulario)


def _estado_login(mensaje, tipo="error") -> str:
    etiqueta = {"alerta": "Aviso", "ok": "Listo"}.get(tipo, "Error")
    return (f'<p class="estado" data-tipo="{tipo}" role="alert"><span class="estado__tag">{etiqueta}</span><span>{esc(mensaje)}</span></p>')


def render_recuperar(modo, error=None, token="", identifier="") -> str:
    """modo: 'pedir' (formulario), 'enviado' (respuesta única), 'nueva' (elegir contraseña con el enlace) o 'invalido'."""
    def cabecera(titulo, texto):
        return (f'<div class="analisis__texto"><h1 class="analisis__titulo pixel" id="titulo-acceso">{titulo}</h1>'
                f'<p class="analisis__descripcion">{texto}</p></div>')
    volver = '<p class="analisis__descripcion acceso__nota"><a href="/login" style="color:inherit;text-decoration:underline;text-underline-offset:4px">Volver a iniciar sesión</a></p>'
    if modo == "enviado":
        cuerpo = (cabecera("Revisa tu Telegram", "Si esa cuenta existe y tiene Telegram vinculado, te enviamos un enlace para cambiar la contraseña. "
                           f"Vale {cuenta_minutos()} minutos y solo se puede usar una vez.")
                  + '<div class="formulario">' + _estado_login("Si no te llega nada, tu cuenta no tiene Telegram vinculado: pídele al administrador de tu comunidad "
                                                               "que te genere un enlace.", "alerta") + volver + "</div>")
    elif modo == "nueva":
        cuerpo = (cabecera("Elige tu contraseña nueva", "Al guardarla cerramos tus sesiones en todos los dispositivos.")
                  + f'<form class="formulario" method="post" action="/recuperar/{esc(token)}" autocomplete="off">'
                  + _campo_clave("nueva", "Contraseña nueva", "new-password", minimo=True)
                  + _campo_clave("confirmacion", "Repite la contraseña nueva", "new-password", minimo=True)
                  + (_estado_login(error) if error else "") + _reglas_clave()
                  + '<div><button class="btn" type="submit">Guardar contraseña</button></div></form>')
    elif modo == "invalido":
        cuerpo = (cabecera("Este enlace ya no sirve", "Pudo vencer (dura unos minutos) o ya se usó.")
                  + '<div class="formulario"><div><a class="btn" href="/recuperar" style="text-decoration:none;display:inline-block">Pedir un enlace nuevo</a></div></div>')
    else:
        cuerpo = (cabecera("Recupera tu contraseña", "Escribe el correo o el teléfono de tu cuenta. Si tienes Telegram vinculado, te mandamos un enlace.")
                  + '<form class="formulario" method="post" action="/recuperar" autocomplete="on"><div class="campo">'
                  '<label class="campo__etiqueta" for="identifier">Correo o teléfono</label>'
                  '<input class="campo__entrada" id="identifier" name="identifier" type="text" inputmode="email" autocomplete="username" '
                  f'autocapitalize="none" spellcheck="false" maxlength="254" value="{esc(identifier)}" required autofocus></div>'
                  + (_estado_login(error, "alerta") if error else "")
                  + '<div><button class="btn" type="submit">Enviarme el enlace</button></div>' + volver + "</form>")
    return fill(read_template("recuperar.html"), contenido='<div class="contenedor analisis">' + cuerpo + "</div>")


def cuenta_minutos() -> int:
    from backend import cuenta
    return cuenta.TOKEN_MINUTOS
