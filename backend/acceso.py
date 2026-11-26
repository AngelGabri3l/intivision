"""
Qué puede ver cada rol. Es el único lugar que lo decide; cámaras, eventos, evidencias, video en vivo, descargas y resúmenes lo usan.

  community_admin  las cámaras de su comunidad que son zona común, las suyas y las privadas que su dueño no le ocultó
  resident         sus cámaras privadas
  guard            (vigilante) las zonas comunes de su comunidad: en vivo, historial y alertas. Nada privado, ni el plan
  family           (familiar) solo las cámaras que su vecino le compartió, mientras sigan siendo de ese vecino y estén activas

Cada función devuelve un trozo de SQL con marcas {uid} y {com} (la persona y su comunidad). `posicional` las cambia por %s y da la
lista de valores en orden; `nombrado` las cambia por %(acc_uid)s y %(acc_com)s y da el diccionario.
"""
from __future__ import annotations

import re

ROLES = ("resident", "community_admin", "guard", "family")
ROLES_CON_PLAN = ("resident", "community_admin")          # pueden pagar, bajar de plan o darse de baja con cámaras
ROLES_DUENOS = ("resident", "community_admin")            # pueden ser dueños de una cámara privada
NOMBRES = {"community_admin": "Administrador", "resident": "Residente", "guard": "Vigilante", "family": "Familiar"}

_FAMILIAR = ("EXISTS (SELECT 1 FROM camera_shares sh JOIN users fam ON fam.id = sh.user_id "
             "JOIN users spn ON spn.id = fam.sponsor_id AND spn.status = 'active' "          # si su vecino está suspendido, no ve nada
             "WHERE sh.camera_id = {c}.id AND sh.user_id = {uid} AND {c}.user_id = fam.sponsor_id AND {c}.is_active)")


def camaras(user, c="c") -> str:
    rol = user["role"]
    if rol == "community_admin":
        return f"({c}.community_id = {{com}} AND ({c}.kind = 'common' OR {c}.user_id = {{uid}} OR {c}.admin_can_view))"
    if rol == "guard":
        return f"({c}.community_id = {{com}} AND {c}.kind = 'common')"
    if rol == "family":
        return _FAMILIAR.replace("{c}", c)
    return f"{c}.user_id = {{uid}}"


def eventos(user, e="e", c="c") -> str:
    """Para consultas de eventos con LEFT JOIN de su cámara."""
    rol = user["role"]
    if rol == "community_admin":
        return f"({e}.community_id = {{com}} AND ({c}.id IS NULL OR {c}.admin_can_view OR {c}.user_id = {{uid}}))"
    if rol in ("guard", "family"):
        return f"({c}.id IS NOT NULL AND {camaras(user, c)})"
    return f"({e}.user_id = {{uid}} OR {c}.user_id = {{uid}})"


def evidencias(user, m="m", c="c") -> str:
    """Para consultas de fotos y clips (media_files) con LEFT JOIN de su cámara."""
    rol = user["role"]
    if rol == "community_admin":
        return f"({m}.community_id = {{com}} AND ({c}.id IS NULL OR {c}.admin_can_view OR {c}.user_id = {{uid}}))"
    if rol in ("guard", "family"):
        return f"({c}.id IS NOT NULL AND {camaras(user, c)})"
    return f"({m}.user_id = {{uid}} OR {c}.user_id = {{uid}})"


def posicional(plantilla: str, user) -> tuple[str, list]:
    valores = []

    def poner(m):
        valores.append(str(user["id"]) if m.group(1) == "uid" else str(user["community_id"]))
        return "%s"
    return re.sub(r"\{(uid|com)\}", poner, plantilla), valores


def nombrado(plantilla: str, user) -> tuple[str, dict]:
    sql = plantilla.replace("{uid}", "%(acc_uid)s").replace("{com}", "%(acc_com)s")
    return sql, {"acc_uid": str(user["id"]), "acc_com": str(user["community_id"])}


def ve_todo_lo_privado_ajeno(user) -> bool:
    """Si al mirar una cámara o una evidencia de otra persona hay que dejar registro (el administrador)."""
    return user["role"] == "community_admin"


def tiene_plan(user) -> bool:
    return user["role"] in ROLES_CON_PLAN


def nombre_rol(user) -> str:
    return NOMBRES.get(user.get("role"), "Residente")
