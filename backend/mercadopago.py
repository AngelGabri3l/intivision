"""
Mercado Pago Suscripciones (cobro mensual automático), sin que los datos de la tarjeta pasen por este servidor.

  • «Pasar a Premium» crea una suscripción con pago pendiente (POST /preapproval, status=pending) y lleva a la persona a la página
    de Mercado Pago (init_point). Allí elige su tarjeta y autoriza el cobro mensual.
  • Mercado Pago avisa por webhook (firmado con x-signature) cuando la suscripción queda autorizada y cada vez que cobra una cuota.
    Los cobros rechazados los reintenta Mercado Pago (4 veces en 10 días) y tras 3 cuotas rechazadas cancela la suscripción.
  • Pausar, reanudar y cancelar se hacen con PUT /preapproval/{id}.

Variables de entorno: MP_ACCESS_TOKEN (credencial de prueba «TEST-...» o de producción), MP_WEBHOOK_SECRET (la clave de la
sección Webhooks de la aplicación) y, opcional para pruebas, MP_PAYER_EMAIL_PRUEBA (el correo de la cuenta compradora de prueba).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid

import requests

API = "https://api.mercadopago.com"
TIMEOUT = (5, 20)


class ErrorMP(Exception):
    def __init__(self, status, mensaje):
        super().__init__(mensaje)
        self.status, self.mensaje = status, mensaje


def configurado() -> bool:
    return bool(os.environ.get("MP_ACCESS_TOKEN"))


def _pedir(metodo, ruta, cuerpo=None):
    token = os.environ.get("MP_ACCESS_TOKEN", "")
    if not token:
        raise ErrorMP(0, "Falta MP_ACCESS_TOKEN.")
    cabeceras = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if metodo == "POST":
        cabeceras["X-Idempotency-Key"] = str(uuid.uuid4())
    try:
        r = requests.request(metodo, API + ruta, json=cuerpo, headers=cabeceras, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ErrorMP(0, f"No se pudo conectar con Mercado Pago ({e.__class__.__name__}).") from None
    if r.status_code >= 400:
        try:
            detalle = r.json().get("message") or r.text[:200]
        except ValueError:
            detalle = r.text[:200]
        raise ErrorMP(r.status_code, f"Mercado Pago respondió {r.status_code}: {detalle}")
    return r.json() if r.content else {}


def crear_suscripcion(motivo: str, referencia: str, correo: str, monto_centimos: int, volver_a: str) -> dict:
    """Suscripción mensual en soles con pago pendiente. Devuelve el preapproval (id, init_point, status...)."""
    return _pedir("POST", "/preapproval", {
        "reason": motivo[:255], "external_reference": referencia, "payer_email": correo, "back_url": volver_a, "status": "pending",
        "auto_recurring": {"frequency": 1, "frequency_type": "months", "transaction_amount": round(monto_centimos / 100, 2), "currency_id": "PEN"}})


def obtener(preapproval_id: str) -> dict:
    return _pedir("GET", f"/preapproval/{preapproval_id}")


def cambiar_estado(preapproval_id: str, estado: str) -> dict:
    """estado: 'paused' (deja de cobrar), 'authorized' (reanuda) o 'cancelled' (definitivo)."""
    if estado not in ("paused", "authorized", "cancelled"):
        raise ValueError("estado no válido")
    return _pedir("PUT", f"/preapproval/{preapproval_id}", {"status": estado})


def obtener_pago_autorizado(autorizado_id: str) -> dict:
    return _pedir("GET", f"/authorized_payments/{autorizado_id}")


def verificar_firma(x_signature: str, x_request_id: str, data_id: str, secreto: str | None = None) -> bool:
    """
    Valida x-signature (ts=...,v1=...): HMAC-SHA256 con la clave de Webhooks sobre «id:{data.id};request-id:{x-request-id};ts:{ts};».
    Un data.id alfanumérico va en minúsculas.
    """
    secreto = secreto if secreto is not None else os.environ.get("MP_WEBHOOK_SECRET", "")
    if not secreto or not x_signature:
        return False
    partes = {}
    for trozo in x_signature.split(","):
        if "=" in trozo:
            k, v = trozo.split("=", 1)
            partes[k.strip()] = v.strip()
    ts, v1 = partes.get("ts"), partes.get("v1")
    if not ts or not v1:
        return False
    manifiesto = ""
    if data_id:
        manifiesto += f"id:{data_id.lower() if data_id.isalnum() else data_id};"
    if x_request_id:
        manifiesto += f"request-id:{x_request_id};"
    manifiesto += f"ts:{ts};"
    esperado = hmac.new(secreto.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)
