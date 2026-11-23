"""
Suscripciones: el plan de cada vivienda y de cada comunidad, lo que se cobra, cuándo vence y qué pasa al final del período.

  • Plan Base: lo incluye la cuota de la comunidad (cobro agrupado por la junta). No hay suscripción propia.
  • Premium de la vivienda: lo paga el vecino; vale para sus cámaras privadas. Se renueva cada 30 días.
  • Premium de la comunidad: lo paga el administrador; vale para todas las cámaras de la comunidad.
"""
from __future__ import annotations

import datetime
import json

PERIODO_DIAS = 30
_VIGENTE = "status IN ('trialing', 'active', 'past_due')"


def soles(centimos) -> str:
    return f"S/ {int(centimos or 0) / 100:,.2f}".replace(",", " ")


def fecha(valor) -> str:
    if not valor:
        return ""
    lima = datetime.timezone(datetime.timedelta(hours=-5))
    return valor.astimezone(lima).strftime("%d/%m/%Y")


def plan(cur, code="premium") -> dict:
    cur.execute("""SELECT code, name, price_pen_cents, community_price_pen_cents, highlight_clips, clip_seconds, weapon_detection,
                          high_precision, retention_days, weekly_report, evidence_export
                   FROM plans WHERE code = %s""", (code,))
    return dict(cur.fetchone() or {})


def suscripcion_vivienda(cur, user_id) -> dict | None:
    cur.execute(f"""SELECT s.id::text AS id, s.plan_code, s.status, s.current_period_start, s.current_period_end, s.cancel_at_period_end,
                           s.scheduled_action, s.payment_method_id, s.renewal_attempts, s.next_retry_at, s.provider,
                           pm.brand AS card_brand, pm.last4 AS card_last4
                    FROM subscriptions s LEFT JOIN payment_methods pm ON pm.id = s.payment_method_id
                    WHERE s.user_id = %s AND s.{_VIGENTE} ORDER BY s.created_at DESC LIMIT 1""", (user_id,))
    fila = cur.fetchone()
    return dict(fila) if fila else None


def suscripcion_comunidad(cur, community_id) -> dict | None:
    cur.execute("""SELECT cs.id::text AS id, cs.plan_code, cs.status, cs.current_period_start, cs.current_period_end,
                          cs.cancel_at_period_end, cs.payment_method_id, cs.renewal_attempts, u.full_name AS pagado_por,
                          pm.brand AS card_brand, pm.last4 AS card_last4
                   FROM community_subscriptions cs LEFT JOIN users u ON u.id = cs.paid_by
                   LEFT JOIN payment_methods pm ON pm.id = cs.payment_method_id
                   WHERE cs.community_id = %s AND cs.status IN ('active', 'past_due') LIMIT 1""", (community_id,))
    fila = cur.fetchone()
    return dict(fila) if fila else None


def pagos(cur, user_id, limite=12) -> list[dict]:
    cur.execute("""SELECT id, scope, plan_code, kind, amount_cents, status, failure_message, card_brand, card_last4, receipt_number,
                          created_at, paid_at
                   FROM payments WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""", (user_id, limite))
    return [dict(r) for r in cur.fetchall()]


def estado(cur, user) -> dict:
    """Todo lo que muestra «Mi plan»."""
    cur.execute("SELECT plan_code FROM communities WHERE id = %s", (user["community_id"],))
    fila = cur.fetchone()
    comunidad_premium = bool(fila and fila["plan_code"] == "premium")
    propia = suscripcion_vivienda(cur, user["id"])
    propia_premium = bool(propia and propia["plan_code"] == "premium" and propia["status"] in ("active", "trialing", "past_due"))
    return {"base": plan(cur, "base"), "premium": plan(cur, "premium"), "vivienda": propia, "vivienda_premium": propia_premium,
            "comunidad_premium": comunidad_premium, "comunidad": suscripcion_comunidad(cur, user["community_id"]),
            "pagos": pagos(cur, user["id"]), "es_admin": user["role"] == "community_admin"}


# ------------------------------------------------------------------ pagos (pasarela simulada)

class ErrorPago(Exception):
    def __init__(self, mensaje, codigo="error"):
        super().__init__(mensaje)
        self.mensaje, self.codigo = mensaje, codigo


def crear_sesion(cur, user, scope: str) -> str:
    """Abre una sesión de pago para pasar a Premium (de la vivienda o de la comunidad). Devuelve su identificador."""
    from backend import pasarela
    p = plan(cur, "premium")
    if scope == "comunidad":
        if user["role"] != "community_admin":
            raise ErrorPago("Solo el administrador puede pagar el plan de la comunidad.", "prohibido")
        if suscripcion_comunidad(cur, user["community_id"]):
            raise ErrorPago("Tu comunidad ya tiene el Plan Premium.", "ya_premium")
        monto = int(p["community_price_pen_cents"])
    elif scope == "vivienda":
        s = suscripcion_vivienda(cur, user["id"])
        if s and s["plan_code"] == "premium":
            raise ErrorPago("Ya tienes el Plan Premium.", "ya_premium")
        monto = int(p["price_pen_cents"])
    else:
        raise ErrorPago("Plan desconocido.", "desconocido")
    sid = pasarela.nuevo_id("cs")
    cur.execute("""INSERT INTO checkout_sessions (id, user_id, community_id, scope, plan_code, amount_cents)
                   VALUES (%s, %s, %s, %s, 'premium', %s)""", (sid, user["id"], user["community_id"], scope, monto))
    return sid


def sesion(cur, user, sid) -> dict | None:
    """La sesión de pago, si es de esta persona; las vencidas se marcan como tales."""
    cur.execute("UPDATE checkout_sessions SET status = 'expired' WHERE id = %s AND status = 'open' AND expires_at < now()", (sid,))
    cur.execute("SELECT * FROM checkout_sessions WHERE id = %s AND user_id = %s", (sid, user["id"]))
    fila = cur.fetchone()
    return dict(fila) if fila else None


def pagar(cur, user, sid, numero, vence, cvc, nombre):
    """
    Procesa el formulario de tarjeta. Devuelve ('ok', pago) si se cobró, ('3ds', pago) si el banco pide autenticación, o lanza
    ErrorPago con el mensaje para mostrar. Un rechazo queda registrado como pago fallido.
    """
    from backend import pasarela
    s = sesion(cur, user, sid)
    if s is None or s["status"] != "open":
        raise ErrorPago("Esta sesión de pago ya no está disponible. Vuelve a «Mi plan» y empieza de nuevo.", "sesion")
    datos, problema = pasarela.validar_tarjeta(numero, vence, cvc, nombre)
    if problema:
        raise ErrorPago(problema, "formulario")
    pid = pasarela.nuevo_id("pi")
    estado = {"ok": "succeeded", "ok_falla_renovacion": "succeeded", "3ds": "requires_action"}.get(datos["resultado"], "failed")
    cur.execute("""INSERT INTO payments (id, user_id, community_id, scope, plan_code, kind, amount_cents, status, failure_code,
                                         failure_message, card_brand, card_last4)
                   VALUES (%s, %s, %s, %s, %s, 'alta', %s, %s, %s, %s, %s, %s)""",
                (pid, user["id"], user["community_id"], s["scope"], s["plan_code"], s["amount_cents"], "requires_action" if estado == "requires_action" else "failed",
                 datos["codigo"], datos["mensaje"], datos["brand"], datos["last4"]))
    if estado == "failed":
        raise ErrorPago(datos["mensaje"], datos["codigo"])
    pm = pasarela.nuevo_id("pm")
    cur.execute("""INSERT INTO payment_methods (id, user_id, brand, last4, exp_month, exp_year, test_behavior)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (pm, user["id"], datos["brand"], datos["last4"], datos["exp_month"], datos["exp_year"],
                 "falla_renovacion" if datos["resultado"] == "ok_falla_renovacion" else "ok"))
    cur.execute("UPDATE payments SET payment_method_id = %s WHERE id = %s", (pm, pid))
    if estado == "requires_action":
        return "3ds", pid
    return "ok", confirmar(cur, pid)


def confirmar(cur, pid) -> dict:
    """El cobro salió: número de comprobante, sesión completa y evento firmado que activa el plan."""
    from backend import pasarela
    cur.execute("SELECT nextval('receipt_seq') AS n")
    recibo = f"B001-{cur.fetchone()['n']:08d}"
    cur.execute("""UPDATE payments SET status = 'succeeded', paid_at = now(), receipt_number = %s, failure_code = NULL, failure_message = NULL
                   WHERE id = %s AND status IN ('requires_action', 'failed') RETURNING *""", (recibo, pid))
    pago = dict(cur.fetchone())
    cur.execute("""UPDATE checkout_sessions SET status = 'complete', completed_at = now(), payment_id = %s
                   WHERE user_id = %s AND status = 'open' AND scope = %s RETURNING id""", (pid, pago["user_id"], pago["scope"]))
    ev, cuerpo, firma = pasarela.evento("checkout.session.completed", {
        "payment_id": pid, "user_id": str(pago["user_id"]), "community_id": str(pago["community_id"]), "scope": pago["scope"],
        "plan_code": pago["plan_code"], "amount": pago["amount_cents"], "currency": "pen", "payment_method": pago["payment_method_id"]})
    procesar_evento(cur, cuerpo, firma)
    return pago


def fallar(cur, pid, codigo, mensaje):
    cur.execute("""UPDATE payments SET status = 'failed', failure_code = %s, failure_message = %s
                   WHERE id = %s AND status = 'requires_action'""", (codigo, mensaje, pid))


def procesar_evento(cur, cuerpo: bytes, firma: str) -> str:
    """
    Recibe un evento de la pasarela (lo usa también POST /pagos/webhook). Verifica la firma, lo registra una sola vez y aplica su
    efecto. Devuelve 'procesado', 'repetido' o lanza ErrorPago si la firma no es válida.
    """
    from backend import pasarela
    if not pasarela.verificar(cuerpo, firma):
        raise ErrorPago("Firma del evento no válida.", "firma")
    ev = json.loads(cuerpo)
    cur.execute("""INSERT INTO payment_events (id, type, data) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING RETURNING id""",
                (ev["id"], ev["type"], json.dumps(ev)))
    if cur.fetchone() is None:
        return "repetido"
    d = ev["data"]["object"]
    if ev["type"] == "checkout.session.completed":
        if d["scope"] == "comunidad":
            activar_comunidad(cur, d["community_id"], d["user_id"], d["payment_method"])
        else:
            activar_vivienda(cur, d["user_id"], d["payment_method"], d["payment_id"])
    cur.execute("UPDATE payment_events SET processed_at = now() WHERE id = %s", (ev["id"],))
    return "procesado"


def activar_vivienda(cur, user_id, payment_method_id, payment_id, ahora=None):
    cur.execute("""UPDATE subscriptions SET status = 'canceled', canceled_at = now()
                   WHERE user_id = %s AND status IN ('trialing', 'active', 'past_due')""", (user_id,))
    cur.execute("""INSERT INTO subscriptions (user_id, plan_code, status, current_period_start, current_period_end, provider, provider_ref,
                                              payment_method_id)
                   VALUES (%s, 'premium', 'active', coalesce(%s, now()), coalesce(%s, now()) + make_interval(days => %s),
                           'pasarela_simulada', %s, %s)""", (user_id, ahora, ahora, PERIODO_DIAS, payment_id, payment_method_id))
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id)
                   SELECT 'user', u.id, u.community_id, u.id, 'subscription_premium', 'payment', %s FROM users u WHERE u.id = %s""",
                (payment_id, user_id))


def activar_comunidad(cur, community_id, user_id, payment_method_id, ahora=None):
    cur.execute("""INSERT INTO community_subscriptions (community_id, paid_by, plan_code, current_period_start, current_period_end, payment_method_id)
                   VALUES (%s, %s, 'premium', coalesce(%s, now()), coalesce(%s, now()) + make_interval(days => %s), %s)""",
                (community_id, user_id, ahora, ahora, PERIODO_DIAS, payment_method_id))
    cur.execute("UPDATE communities SET plan_code = 'premium' WHERE id = %s", (community_id,))
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, action, entity, entity_id)
                   VALUES ('user', %s, %s, 'community_premium', 'community', %s)""", (user_id, community_id, str(community_id)))


def comprobante(cur, user, pid) -> dict | None:
    cur.execute("""SELECT p.*, u.full_name, coalesce(u.email, u.phone) AS contacto, c.name AS comunidad
                   FROM payments p JOIN users u ON u.id = p.user_id JOIN communities c ON c.id = p.community_id
                   WHERE p.id = %s AND p.user_id = %s AND p.status = 'succeeded'""", (pid, user["id"]))
    fila = cur.fetchone()
    return dict(fila) if fila else None
