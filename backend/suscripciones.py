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


# ------------------------------------------------------------------ cambios de plan y baja del servicio

REINTENTOS_RENOVACION = 3          # cobros fallidos seguidos antes de volver al Plan Base
ESPERA_REINTENTO = datetime.timedelta(days=1)


def cancelar_premium(cur, user) -> bool:
    """Bajar a Base al final del período pagado (no hay reembolso del período en curso, como en Stripe o Tapo Care)."""
    cur.execute("""UPDATE subscriptions SET cancel_at_period_end = true, scheduled_action = 'bajar_a_base'
                   WHERE user_id = %s AND plan_code = 'premium' AND status IN ('active', 'trialing', 'past_due')
                     AND scheduled_action IS DISTINCT FROM 'dar_de_baja' RETURNING id""", (user["id"],))
    return cur.fetchone() is not None


def reactivar(cur, user) -> bool:
    """Deshace una bajada a Base o una baja programada, antes de que llegue el final del período."""
    cur.execute("""UPDATE subscriptions SET cancel_at_period_end = false, scheduled_action = NULL
                   WHERE user_id = %s AND status IN ('active', 'trialing', 'past_due') AND cancel_at_period_end RETURNING id""", (user["id"],))
    return cur.fetchone() is not None


def cancelar_comunidad(cur, admin, cancelar=True) -> bool:
    if admin["role"] != "community_admin":
        return False
    cur.execute("""UPDATE community_subscriptions SET cancel_at_period_end = %s
                   WHERE community_id = %s AND status IN ('active', 'past_due') RETURNING id""", (cancelar, admin["community_id"]))
    return cur.fetchone() is not None


def dar_de_baja(cur, user) -> tuple[str, dict | None]:
    """
    El vecino deja el servicio. Si tiene Premium pagado, la baja ocurre al final de ese período ('programada'); si no, en el acto
    ('inmediata', con la información para avisar). Un administrador solo puede irse si queda otro administrador activo.
    """
    if user["role"] == "community_admin":
        cur.execute("""SELECT count(*) AS n FROM users WHERE community_id = %s AND role = 'community_admin' AND status = 'active' AND id <> %s""",
                    (user["community_id"], user["id"]))
        if cur.fetchone()["n"] == 0:
            raise ErrorPago("Eres el único administrador de la comunidad: antes de irte, otra persona debe ser administradora.", "unico_admin")
    s = suscripcion_vivienda(cur, user["id"])
    if s and s["plan_code"] == "premium" and s["status"] in ("active", "trialing") and s["current_period_end"] and \
            s["current_period_end"] > datetime.datetime.now(datetime.timezone.utc):
        cur.execute("UPDATE subscriptions SET cancel_at_period_end = true, scheduled_action = 'dar_de_baja' WHERE id = %s", (s["id"],))
        return "programada", None
    return "inmediata", baja_efectiva(cur, user["id"], "a pedido de la persona")


def baja_efectiva(cur, user_id, motivo) -> dict:
    """
    Lo que significa dejar el servicio, en una sola operación: sus cámaras privadas se desasignan (quedan en «Equipos» para que el
    administrador las vuelva a asignar), deja de pertenecer a la comunidad (cuenta cerrada), se cierran sus sesiones, se desvincula
    su Telegram, se borran sus tarjetas guardadas y se cancela su suscripción. Sus eventos se conservan según la retención.
    """
    from backend import sources
    cur.execute("SELECT full_name, community_id::text AS community_id FROM users WHERE id = %s FOR UPDATE", (user_id,))
    u = cur.fetchone()
    cur.execute("""SELECT chat_id FROM notification_channels WHERE user_id = %s AND kind = 'telegram' AND is_active AND chat_id IS NOT NULL LIMIT 1""",
                (user_id,))
    canal = cur.fetchone()
    cur.execute("""SELECT id::text AS id, name FROM cameras WHERE user_id = %s AND kind = 'private' AND is_active FOR UPDATE""", (user_id,))
    camaras = cur.fetchall()
    for c in camaras:
        cur.execute("""UPDATE cameras SET is_active = false, source_id = NULL,
                              name = left(name, 35) || ' (archivada ' || to_char(now(), 'YYYY-MM-DD') || ' ' || left(id::text, 8) || ')'
                       WHERE id = %s""", (c["id"],))
        sources._borrar_ubicacion(cur, c["id"])
    cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    cur.execute("DELETE FROM notification_channels WHERE user_id = %s", (user_id,))     # como «Desvincular» en Alertas
    cur.execute("""UPDATE subscriptions SET status = 'canceled', canceled_at = now(), cancel_at_period_end = false
                   WHERE user_id = %s AND status IN ('trialing', 'active', 'past_due')""", (user_id,))
    cur.execute("DELETE FROM payment_methods WHERE user_id = %s", (user_id,))
    cur.execute("UPDATE users SET status = 'canceled' WHERE id = %s", (user_id,))
    cur.execute("""INSERT INTO audit_log (actor_type, actor_id, community_id, subject_user_id, action, entity, entity_id, meta)
                   VALUES ('user', %s, %s, %s, 'offboarding', 'user', %s, %s::jsonb)""",
                (user_id, u["community_id"], user_id, str(user_id), json.dumps({"camaras": len(camaras), "motivo": motivo})))
    cur.execute("""SELECT id::text AS id FROM users WHERE community_id = %s AND role = 'community_admin' AND status = 'active'""", (u["community_id"],))
    admins = [r["id"] for r in cur.fetchall()]
    return {"nombre": u["full_name"], "camaras": [c["name"] for c in camaras], "admins": admins,
            "chat_despedida": canal["chat_id"] if canal else None}


def avisos_de_baja(info) -> list[tuple[str, str]]:
    """[(destino, texto)]: destino 'chat:<id>' para la persona que se va, o el id del administrador."""
    if not info:
        return []
    salida = []
    if info.get("chat_despedida"):
        salida.append((f"chat:{info['chat_despedida']}", "Tu cuenta de INTIVISION se cerró. Gracias por cuidar tu cuadra con nosotros."))
    n = len(info["camaras"])
    texto = (f"🔔 {info['nombre']} dejó INTIVISION." + (f" Su(s) {n} cámara(s) quedaron sin asignar en «Equipos y cámaras»." if n else ""))
    salida += [(a, texto) for a in info["admins"]]
    return salida


# ------------------------------------------------------------------ renovación y fin de período

def _cobrar_renovacion(cur, quien, community_id, scope, monto, pm_id):
    """Cobra la renovación con la tarjeta guardada. Devuelve (ok, pago)."""
    from backend import pasarela
    cur.execute("SELECT brand, last4, test_behavior FROM payment_methods WHERE id = %s", (pm_id,))
    pm = cur.fetchone()
    pid = pasarela.nuevo_id("pi")
    if pm is None or pm["test_behavior"] == "falla_renovacion":
        cur.execute("""INSERT INTO payments (id, user_id, community_id, scope, plan_code, kind, amount_cents, status, failure_code, failure_message,
                                             payment_method_id, card_brand, card_last4)
                       VALUES (%s, %s, %s, %s, 'premium', 'renovacion', %s, 'failed', 'card_declined', 'El banco rechazó el cobro de la renovación.',
                               %s, %s, %s)""", (pid, quien, community_id, scope, monto, pm_id, pm and pm["brand"], pm and pm["last4"]))
        return False, None
    cur.execute("SELECT nextval('receipt_seq') AS n")
    recibo = f"B001-{cur.fetchone()['n']:08d}"
    cur.execute("""INSERT INTO payments (id, user_id, community_id, scope, plan_code, kind, amount_cents, status, payment_method_id, card_brand,
                                         card_last4, receipt_number, paid_at)
                   VALUES (%s, %s, %s, %s, 'premium', 'renovacion', %s, 'succeeded', %s, %s, %s, %s, now()) RETURNING *""",
                (pid, quien, community_id, scope, monto, pm_id, pm["brand"], pm["last4"], recibo))
    return True, dict(cur.fetchone())


def procesar_vencimientos(cur, ahora=None) -> list[tuple[str, str]]:
    """
    Aplica lo que toca al final de cada período: baja programada, bajada a Base, o renovación (con reintentos diarios: tras
    REINTENTOS_RENOVACION fallos seguidos, vuelve al Plan Base). Devuelve los avisos [(destino, texto)] para enviar luego.
    """
    ahora = ahora or datetime.datetime.now(datetime.timezone.utc)
    avisos = []
    p = plan(cur, "premium")
    cur.execute("""SELECT s.*, u.community_id FROM subscriptions s JOIN users u ON u.id = s.user_id
                   WHERE s.status IN ('active', 'trialing', 'past_due') AND s.plan_code = 'premium'
                     AND ((s.status <> 'past_due' AND s.current_period_end <= %s) OR (s.status = 'past_due' AND s.next_retry_at <= %s))
                   ORDER BY s.current_period_end FOR UPDATE OF s SKIP LOCKED""", (ahora, ahora))
    for s in [dict(r) for r in cur.fetchall()]:
        uid = str(s["user_id"])
        if s["scheduled_action"] == "dar_de_baja":
            avisos += avisos_de_baja(baja_efectiva(cur, uid, "fin del período pagado"))
        elif s["cancel_at_period_end"]:
            cur.execute("UPDATE subscriptions SET status = 'canceled', canceled_at = %s WHERE id = %s", (ahora, s["id"]))
            avisos.append((uid, "Tu Plan Premium terminó: volviste al Plan Base. Puedes volver a Premium cuando quieras desde «Mi plan»."))
        else:
            ok, pago = _cobrar_renovacion(cur, uid, s["community_id"], "vivienda", p["price_pen_cents"], s["payment_method_id"])
            if ok:
                cur.execute("""UPDATE subscriptions SET status = 'active', renewal_attempts = 0, next_retry_at = NULL,
                                      current_period_start = current_period_end,
                                      current_period_end = current_period_end + make_interval(days => %s) WHERE id = %s""", (PERIODO_DIAS, s["id"]))
                avisos.append((uid, f"✅ Renovamos tu Plan Premium: {soles(pago['amount_cents'])}, comprobante {pago['receipt_number']}."))
            else:
                intentos = s["renewal_attempts"] + 1
                if intentos >= REINTENTOS_RENOVACION:
                    cur.execute("UPDATE subscriptions SET status = 'canceled', canceled_at = %s, renewal_attempts = %s WHERE id = %s", (ahora, intentos, s["id"]))
                    avisos.append((uid, "No pudimos cobrar la renovación de tu Plan Premium: volviste al Plan Base. Puedes pagar de nuevo desde «Mi plan»."))
                else:
                    cur.execute("""UPDATE subscriptions SET status = 'past_due', renewal_attempts = %s, next_retry_at = %s WHERE id = %s""",
                                (intentos, ahora + ESPERA_REINTENTO, s["id"]))
                    avisos.append((uid, f"⚠️ No pudimos cobrar la renovación de tu Plan Premium. Lo intentaremos de nuevo mañana ({intentos} de {REINTENTOS_RENOVACION})."))
    cur.execute("""SELECT * FROM community_subscriptions WHERE status IN ('active', 'past_due')
                     AND ((status = 'active' AND current_period_end <= %s) OR (status = 'past_due' AND next_retry_at <= %s))
                   FOR UPDATE SKIP LOCKED""", (ahora, ahora))
    for s in [dict(r) for r in cur.fetchall()]:
        quien = str(s["paid_by"]) if s["paid_by"] else None
        if s["cancel_at_period_end"] or quien is None:
            cur.execute("UPDATE community_subscriptions SET status = 'canceled', canceled_at = %s WHERE id = %s", (ahora, s["id"]))
            cur.execute("UPDATE communities SET plan_code = 'base' WHERE id = %s", (s["community_id"],))
            if quien:
                avisos.append((quien, "El Plan Premium de la comunidad terminó: las cámaras volvieron al Plan Base."))
            continue
        ok, pago = _cobrar_renovacion(cur, quien, s["community_id"], "comunidad", p["community_price_pen_cents"], s["payment_method_id"])
        if ok:
            cur.execute("""UPDATE community_subscriptions SET status = 'active', renewal_attempts = 0, next_retry_at = NULL,
                                  current_period_start = current_period_end, current_period_end = current_period_end + make_interval(days => %s)
                           WHERE id = %s""", (PERIODO_DIAS, s["id"]))
            avisos.append((quien, f"✅ Renovamos el Plan Premium de la comunidad: {soles(pago['amount_cents'])}, comprobante {pago['receipt_number']}."))
        else:
            intentos = s["renewal_attempts"] + 1
            if intentos >= REINTENTOS_RENOVACION:
                cur.execute("UPDATE community_subscriptions SET status = 'canceled', canceled_at = %s, renewal_attempts = %s WHERE id = %s", (ahora, intentos, s["id"]))
                cur.execute("UPDATE communities SET plan_code = 'base' WHERE id = %s", (s["community_id"],))
                avisos.append((quien, "No pudimos cobrar el Plan Premium de la comunidad: las cámaras volvieron al Plan Base."))
            else:
                cur.execute("UPDATE community_subscriptions SET status = 'past_due', renewal_attempts = %s, next_retry_at = %s WHERE id = %s",
                            (intentos, ahora + ESPERA_REINTENTO, s["id"]))
                avisos.append((quien, f"⚠️ No pudimos cobrar el Plan Premium de la comunidad. Reintentaremos mañana ({intentos} de {REINTENTOS_RENOVACION})."))
    return avisos


def enviar_avisos(avisos) -> None:
    from backend import alerts
    for destino, texto in avisos:
        try:
            if destino.startswith("chat:"):
                alerts._send_text(destino[5:], texto)
            else:
                alerts.notify_user(destino, texto)
        except Exception:       # noqa: BLE001 - un aviso que no sale no frena el ciclo
            pass


def ciclo_forever(stop, cada_s: float = 600):
    """Revisa cada 10 minutos los períodos que terminaron (lo arranca el servidor)."""
    import logging
    from backend import db
    log = logging.getLogger("backend.suscripciones")
    while not stop.is_set():
        try:
            with db.cursor() as cur:
                avisos = procesar_vencimientos(cur)
            enviar_avisos(avisos)
        except Exception:       # noqa: BLE001
            log.exception("Fallo al procesar los vencimientos de las suscripciones")
        stop.wait(cada_s)
