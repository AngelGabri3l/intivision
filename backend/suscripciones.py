"""
Suscripciones: el plan de cada vivienda y de cada comunidad, lo que se cobra, cuándo vence y qué pasa al final del período.

  • Plan Base: lo incluye la cuota de la comunidad (cobro agrupado por la junta). No hay suscripción propia.
  • Premium de la vivienda: lo paga el vecino; vale para sus cámaras privadas. Se renueva cada 30 días.
  • Premium de la comunidad: lo paga el administrador; vale para todas las cámaras de la comunidad.
"""
from __future__ import annotations

import datetime

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
