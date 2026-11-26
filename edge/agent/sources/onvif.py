"""Descubrimiento de cámaras ONVIF por WS-Discovery.

Se envía un mensaje "Probe" por multicast UDP (239.255.255.250:3702) y las cámaras ONVIF
responden por UDP con su dirección y su nombre. Solo se DESCUBRE: leer el video de una cámara
ONVIF exige su usuario y contraseña (se resuelve en H5 desde el panel del administrador).

Necesita red tipo `host` en Docker (el multicast no atraviesa la red `bridge`).

Las respuestas llegan de equipos de la red local y no son de confianza: se rechaza cualquier
XML con DOCTYPE o ENTITY (evita ataques de expansión de entidades) y se limita el tamaño y
la cantidad de respuestas.
"""
from __future__ import annotations

import hashlib
import logging
import re
import select
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

from .base import SourceInfo, ahora_iso

log = logging.getLogger("agent.onvif")

GRUPO, PUERTO = "239.255.255.250", 3702
MAX_RESPUESTAS = 64
MAX_BYTES = 65535


def construir_probe() -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>""".encode("utf-8")


def _local(etiqueta: str) -> str:
    return etiqueta.rsplit("}", 1)[-1]


def interpretar(datos: bytes) -> dict | None:
    """Extrae {epr, scopes, xaddrs, types} de una respuesta ProbeMatches, o None si no sirve."""
    cabeza = datos[:4096].lower()
    if b"<!doctype" in cabeza or b"<!entity" in datos.lower():
        log.warning("respuesta ONVIF descartada: contiene DOCTYPE o ENTITY")
        return None
    try:
        raiz = ET.fromstring(datos)
    except ET.ParseError:
        return None
    epr, scopes, xaddrs, tipos = None, [], [], ""
    encontrado = False
    for el in raiz.iter():
        nombre = _local(el.tag)
        texto = (el.text or "").strip()
        if nombre == "ProbeMatch":
            encontrado = True
        elif nombre == "Address" and epr is None and texto:
            epr = texto
        elif nombre == "Scopes":
            scopes = texto.split()
        elif nombre == "XAddrs":
            xaddrs = texto.split()
        elif nombre == "Types":
            tipos = texto
    if not encontrado or not (epr or xaddrs):
        return None
    return {"epr": epr, "scopes": scopes, "xaddrs": xaddrs, "types": tipos}


def _valor_scope(scopes: list[str], clave: str) -> str | None:
    prefijo = f"onvif://www.onvif.org/{clave}/"
    for s in scopes:
        if s.lower().startswith(prefijo):
            return unquote(s[len(prefijo):]).replace("_", " ").strip() or None
    return None


def _seguro(texto, largo: int) -> str:
    """Texto de una respuesta que viene de la red: sin caracteres de control, sin «://» ni «@» (el servidor los rechaza) y acotado."""
    t = "".join(ch for ch in str(texto or "") if ch.isprintable())
    return " ".join(t.replace("://", " ").replace("@", " ").split())[:largo]


_HOST_OK = re.compile(r"^[A-Za-z0-9.:-]{1,100}$")
_CLAVE_OK = re.compile(r"^[A-Za-z0-9:._-]{1,120}$")


def a_fuente(r: dict) -> SourceInfo:
    """Una respuesta ONVIF falsa o rara no puede romper el inventario: todo lo que viene de la red se limpia y se acota."""
    host = None
    for x in r["xaddrs"][:8]:
        try:
            candidato = urlsplit(x[:300]).hostname
        except ValueError:
            candidato = None
        if candidato and _HOST_OK.match(candidato):
            host = candidato
            break
    nombre = _seguro(_valor_scope(r["scopes"], "name") or _valor_scope(r["scopes"], "hardware"), 80) or None
    etiqueta = nombre or (f"Cámara ONVIF en {host}" if host else "Cámara ONVIF")
    if nombre and host:
        etiqueta = f"{nombre} ({host})"
    epr = r["epr"] or ""
    if epr and not _CLAVE_OK.match(epr):
        epr = "x-" + hashlib.sha1(epr.encode("utf-8", "replace")).hexdigest()[:24]     # un identificador raro se reemplaza por su huella
    clave = epr or f"ip-{host or uuid.uuid4().hex[:12]}"
    return SourceInfo(
        device_key=f"onvif:{clave}", kind="onvif", label=etiqueta, state="available",
        address=host, detail="falta usuario y contraseña para leer su video",
        last_seen=ahora_iso(), uri=None,
    )


def descubrir(espera: float = 3.0, grupo: str = GRUPO, puerto: int = PUERTO) -> list[SourceInfo]:
    """Envía el Probe y junta las respuestas durante `espera` segundos."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        sock.sendto(construir_probe(), (grupo, puerto))
        limite = time.monotonic() + espera
        encontradas: dict[str, SourceInfo] = {}
        while time.monotonic() < limite and len(encontradas) < MAX_RESPUESTAS:
            listo, _, _ = select.select([sock], [], [], max(0.0, limite - time.monotonic()))
            if not listo:
                break
            datos, origen = sock.recvfrom(MAX_BYTES)
            r = interpretar(datos)
            if r:
                f = a_fuente(r)
                encontradas.setdefault(f.device_key, f)
        return list(encontradas.values())
    except OSError as e:
        log.warning("no se pudo hacer el descubrimiento ONVIF: %s", e)
        return []
    finally:
        sock.close()
