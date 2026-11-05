"""
HTML de las pantallas de cámaras (/cameras y /cameras/{id}). Todo lo que viene de la base de datos o de lo
que escribió un usuario se escapa antes de entrar a la página.
"""
import datetime
import html
import os
import re
from urllib.parse import urlencode

from backend import cameras

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
    return "Administrador" if user["role"] == "community_admin" else "Residente"


def nav(user, active) -> str:
    def link(href, text, key):
        actual = ' aria-current="page"' if key == active else ""
        return f'<a class="volver" href="{href}"{actual}>{text}</a>'
    return (f'<span class="usuario">{esc(user["full_name"])} · {role_text(user)}</span>'
            + link("/", "Analizar", "analizar") + link("/cameras", "Cámaras", "camaras")
            + '<form method="post" action="/logout"><button class="volver" type="submit">Cerrar sesión</button></form>')


def alert_block(message, kind="error") -> str:
    if not message:
        return ""
    etiqueta = "Aviso" if kind == "alerta" else "Error"
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
            f'<span>{format_when(r["created_at"])}</span></div>{linea}</div></div>')


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


def camera_card(c, user) -> str:
    lugar = f'<p class="cam__lugar">{esc(c["place"])}</p>' if c["place"] else ""
    ultimo = level_badge(c["ultimo_nivel"]) if c["ultimo_nivel"] else ""
    return (f'<article class="cam" role="listitem"><div class="cam__fila">'
            f'<h3 class="cam__nombre">{esc(c["name"])}</h3>{ultimo}</div>{lugar}'
            f'<div class="cam__datos"><span>{esc(cameras.SOURCE_TEXT.get(c["source_type"], c["source_type"]))}</span>'
            f'<span>{camera_status(c)}</span>'
            f'<span>{count_text(c["hoy"], "evento hoy", "eventos hoy")} · {c["total"]} en total</span>'
            f'<span>{ownership_text(c, user)}</span></div>'
            f'<div class="cam__pie"><a class="btn" href="/cameras/{esc(c["id"])}">Abrir</a></div></article>')


# ------------------------------------------------------------------ pantallas

def render_cameras(user, cams, message=None, values=("", "")) -> str:
    admin = user["role"] == "community_admin"
    if cams:
        tarjetas = "".join(camera_card(c, user) for c in cams)
    else:
        tarjetas = ('<div class="vacio"><p class="vacio__titulo">Aún no tienes cámaras</p>'
                    '<p class="vacio__texto">Agrega la primera con el formulario de arriba, o ve a «Analizar»: '
                    'si no tienes ninguna, se crea una automáticamente.</p></div>')
    return fill(
        read_template("camaras.html"),
        nav=nav(user, "camaras"),
        titulo="Cámaras de la comunidad" if admin else "Mis cámaras",
        descripcion=("Ves las cámaras de toda tu comunidad. Las de los vecinos aparecen mientras su dueño "
                     "no te las oculte." if admin else
                     "Cada cámara tiene su propio historial de eventos. Agrega las tuyas y elige con cuál "
                     "transmitir o subir un video desde «Analizar»."),
        mensaje=alert_block(message),
        nombre=esc(values[0]), lugar=esc(values[1]),
        titulo_lista="Todas las cámaras" if admin else "Tus cámaras",
        total=esc(count_text(len(cams), "cámara", "cámaras")) if cams else "",
        tarjetas=tarjetas)


def filter_link(cam_id, nivel, desde, hasta, limite) -> str:
    params = {k: v for k, v in (("nivel", nivel), ("desde", desde), ("hasta", hasta)) if v}
    params["limite"] = limite
    return f"/cameras/{cam_id}?{urlencode(params)}"


def render_camera(user, cam, events, nivel, desde, hasta, limite, has_more, message=None) -> str:
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
             f'<span class="ficha__clave">Eventos</span><span>{cam["total"]} en total</span></div>')

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
        eventos=lista, mas=mas)
