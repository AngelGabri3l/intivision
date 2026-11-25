"""
Planes (Base y Premium): qué puede hacer cada cámara.

Una cámara es Premium si su comunidad tiene el plan Premium (cobro agrupado por la junta) o si es privada y su dueño tiene una
suscripción Premium vigente. Lo decide siempre el servidor: la mini PC recibe, cámara por cámara, solo lo que tiene activado.
"""
from __future__ import annotations

import datetime

PLANES = ("base", "premium")
COLUMNAS = ("code, name, price_pen_cents, ai_detection, realtime_alerts, highlight_clips, event_history, weapon_detection, "
            "high_precision, clip_seconds, retention_days, weekly_report, evidence_export, ai_description, ai_daily_cap")
_ACTIVA = "ps.status IN ('active', 'trialing') AND (ps.current_period_end IS NULL OR ps.current_period_end > now())"


def sql_es_premium(c: str = "c") -> str:
    """Expresión SQL (booleana) que dice si la cámara con alias `c` es Premium."""
    return (f"(EXISTS (SELECT 1 FROM communities pc WHERE pc.id = {c}.community_id AND pc.plan_code = 'premium')"
            f" OR ({c}.kind = 'private' AND {c}.user_id IS NOT NULL AND EXISTS (SELECT 1 FROM subscriptions ps"
            f" WHERE ps.user_id = {c}.user_id AND ps.plan_code = 'premium' AND {_ACTIVA})))")


def sql_usuario_premium(u: str = "u") -> str:
    """Expresión SQL (booleana): el usuario con alias `u` tiene Premium, propio o por su comunidad."""
    return (f"(EXISTS (SELECT 1 FROM communities pc WHERE pc.id = {u}.community_id AND pc.plan_code = 'premium')"
            f" OR EXISTS (SELECT 1 FROM subscriptions ps WHERE ps.user_id = {u}.id AND ps.plan_code = 'premium' AND {_ACTIVA}))")


def todos(cur) -> dict:
    """{código: fila del plan}."""
    cur.execute(f"SELECT {COLUMNAS} FROM plans WHERE code IN ('base', 'premium')")
    return {r["code"]: dict(r) for r in cur.fetchall()}


def de_camara(cur, camera_id) -> dict:
    """La fila del plan que rige para una cámara."""
    cur.execute(f"""SELECT {COLUMNAS} FROM plans
                    WHERE code = (SELECT CASE WHEN {sql_es_premium('c')} THEN 'premium' ELSE 'base' END FROM cameras c WHERE c.id = %s)""",
                (str(camera_id),))
    fila = cur.fetchone()
    if fila is None:
        cur.execute(f"SELECT {COLUMNAS} FROM plans WHERE code = 'base'")
        fila = cur.fetchone()
    return dict(fila) if fila else {"code": "base", "highlight_clips": False, "clip_seconds": 0, "weapon_detection": False,
                                    "high_precision": False, "retention_days": 7, "weekly_report": False, "evidence_export": False}


def funciones(plan: dict) -> dict:
    """Lo que la mini PC necesita saber de una cámara: plan, segundos de clip, armas y precisión."""
    clip = int(plan.get("clip_seconds") or 0) if plan.get("highlight_clips") else 0
    return {"code": plan.get("code", "base"), "clip_s": clip, "armas": bool(plan.get("weapon_detection")),
            "precision": "alta" if plan.get("high_precision") else "normal"}


def nombre(code: str) -> str:
    return "Premium" if code == "premium" else "Base"


# ------------------------------------------------------------------ cambios de plan (los usa manage.py)

def asignar_comunidad(cur, community_id, code: str) -> None:
    if code not in PLANES:
        raise ValueError("plan desconocido")
    cur.execute("UPDATE communities SET plan_code = %s WHERE id = %s", (code, community_id))


def asignar_usuario(cur, user_id, code: str, dias: int | None = 30) -> None:
    """Premium: cierra la suscripción vigente y abre una nueva (dias=0 o None: sin vencimiento). Base: cancela la vigente."""
    if code not in PLANES:
        raise ValueError("plan desconocido")
    cur.execute("""UPDATE subscriptions SET status = 'canceled', canceled_at = now()
                   WHERE user_id = %s AND status IN ('trialing', 'active', 'past_due')""", (user_id,))
    if code == "premium":
        fin = None
        if dias:
            fin = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=int(dias))
        cur.execute("""INSERT INTO subscriptions (user_id, plan_code, status, current_period_start, current_period_end, provider)
                       VALUES (%s, 'premium', 'active', now(), %s, 'manual')""", (user_id, fin))
