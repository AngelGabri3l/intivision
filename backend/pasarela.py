"""
Pasarela de pagos SIMULADA, con el comportamiento de Stripe en modo de prueba.

Sirve para demostrar el flujo completo sin cobrar de verdad (Stripe no opera con empresas peruanas; en producción se usaría una
pasarela local como Culqi, Izipay, Niubiz o Mercado Pago, con la misma forma: sesión de pago, cobro, evento firmado y tarjeta
guardada como token).

  • Solo acepta las tarjetas de prueba de abajo (las mismas que usa Stripe). Cualquier otra se rechaza ANTES de procesarla, para
    que nadie escriba aquí una tarjeta real. El número nunca se guarda ni se registra: solo la marca y los últimos 4 dígitos.
  • Cada pago confirmado genera un evento firmado con HMAC-SHA256 (como el encabezado Stripe-Signature) que activa el plan.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import time

# número: (marca, resultado, código, mensaje)
TARJETAS_PRUEBA = {
    "4242424242424242": ("Visa", "ok", None, None),
    "5555555555554444": ("Mastercard", "ok", None, None),
    "378282246310005": ("American Express", "ok", None, None),
    "4000002760003184": ("Visa", "3ds", None, None),
    "4000000000000341": ("Visa", "ok_falla_renovacion", None, None),
    "4000000000000002": ("Visa", "rechazada", "card_declined", "Tu banco rechazó la tarjeta."),
    "4000000000009995": ("Visa", "rechazada", "insufficient_funds", "La tarjeta no tiene fondos suficientes."),
    "4000000000000069": ("Visa", "rechazada", "expired_card", "La tarjeta está vencida."),
    "4000000000000127": ("Visa", "rechazada", "incorrect_cvc", "El código de seguridad (CVC) no es correcto."),
}
TOLERANCIA_FIRMA_S = 300


def nuevo_id(prefijo: str) -> str:
    return f"{prefijo}_test_{secrets.token_hex(12)}"


def luhn(numero: str) -> bool:
    if not numero.isdigit() or not 13 <= len(numero) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(numero)):
        n = int(d)
        if i % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def validar_tarjeta(numero: str, vence: str, cvc: str, nombre: str, hoy: datetime.date | None = None):
    """(datos, None) o (None, mensaje). `datos`: marca, últimos 4, mes, año y resultado de la tarjeta de prueba."""
    numero = re.sub(r"[\s-]", "", numero or "")
    if not luhn(numero):
        return None, "El número de tarjeta no es válido."
    m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*", vence or "")
    if not m:
        return None, "La fecha de vencimiento debe tener la forma MM/AA."
    mes, anio = int(m.group(1)), int(m.group(2))
    anio = anio + 2000 if anio < 100 else anio
    hoy = hoy or datetime.date.today()
    if not 1 <= mes <= 12 or (anio, mes) < (hoy.year, hoy.month) or anio > hoy.year + 20:
        return None, "La fecha de vencimiento no es válida o ya pasó."
    largo_cvc = 4 if numero.startswith(("34", "37")) else 3
    if not re.fullmatch(rf"\d{{{largo_cvc}}}", (cvc or "").strip()):
        return None, f"El código de seguridad (CVC) debe tener {largo_cvc} dígitos."
    if not (nombre or "").strip() or len(nombre.strip()) > 80:
        return None, "Escribe el nombre como aparece en la tarjeta."
    if numero not in TARJETAS_PRUEBA:
        return None, "Modo de prueba: usa una de las tarjetas de prueba. No escribas una tarjeta real."
    marca, resultado, codigo, mensaje = TARJETAS_PRUEBA[numero]
    return {"brand": marca, "last4": numero[-4:], "exp_month": mes, "exp_year": anio, "resultado": resultado,
            "codigo": codigo, "mensaje": mensaje}, None


# ------------------------------------------------------------------ eventos firmados (como los webhooks de Stripe)

_SECRETO_DEL_PROCESO = secrets.token_bytes(32)      # solo para firmar y verificar DENTRO de este proceso (el flujo interno de pago)
SECRETO_MINIMO = 24


def secreto_http_configurado() -> bool:
    """¿Hay un secreto propio (PASARELA_WEBHOOK_SECRET) para recibir eventos desde fuera? No existe ningún valor por defecto."""
    return len(os.environ.get("PASARELA_WEBHOOK_SECRET") or "") >= SECRETO_MINIMO


def webhook_http_habilitado() -> bool:
    """La ruta HTTP del webhook simulado está apagada salvo que se pida con PASARELA_WEBHOOK_HTTP=1 (demostraciones)."""
    return os.environ.get("PASARELA_WEBHOOK_HTTP", "").strip() == "1"


def _secreto() -> bytes:
    if secreto_http_configurado():
        return os.environ["PASARELA_WEBHOOK_SECRET"].encode()
    return _SECRETO_DEL_PROCESO      # sin secreto configurado, nadie de afuera puede firmar un evento válido


def firmar(cuerpo: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    firma = hmac.new(_secreto(), f"{ts}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return f"t={ts},v1={firma}"


def verificar(cuerpo: bytes, encabezado: str, ahora: int | None = None) -> bool:
    try:
        partes = dict(p.split("=", 1) for p in (encabezado or "").split(","))
        ts, firma = int(partes["t"]), partes["v1"]
    except (ValueError, KeyError):
        return False
    ahora = int(time.time()) if ahora is None else ahora
    if abs(ahora - ts) > TOLERANCIA_FIRMA_S:
        return False
    esperada = hmac.new(_secreto(), f"{ts}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, firma)


def evento(tipo: str, datos: dict) -> tuple[dict, bytes, str]:
    """(evento, cuerpo JSON, encabezado de firma)."""
    ev = {"id": nuevo_id("evt"), "type": tipo, "created": int(time.time()), "livemode": False, "data": {"object": datos}}
    cuerpo = json.dumps(ev, sort_keys=True, default=str).encode()
    return ev, cuerpo, firmar(cuerpo)
