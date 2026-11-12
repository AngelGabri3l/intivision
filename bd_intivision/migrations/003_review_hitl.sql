-- =====================================================================================================
-- Migración 003 · H6: revisión del administrador (human in the loop)
--
--   events.escalate_at        cuándo se escala una alerta que nadie revisó. Si NO es nulo, la alerta está (o estuvo) en
--                             revisión: es lo que distingue a las alertas de revisión de los demás eventos.
--   events.escalated_at       cuándo se escaló (nulo = todavía no). Cada alerta se escala una sola vez.
--   alert_deliveries.audience  a quién iba el aviso: normal (como siempre), review (a los administradores, con botones),
--                             broadcast (a todos, tras confirmarse) o escalated (a todos, «no verificada»).
--   alert_deliveries.telegram_message_id / has_photo
--                             el mensaje que se envió, para poder actualizarlo cuando alguien revisa la alerta.
--
-- SE PUEDE EJECUTAR MÁS DE UNA VEZ y va dentro de una transacción: si algo falla, no queda a medias.
-- Requiere las migraciones 001 y 002. Los eventos y avisos que ya existen quedan igual (audience = 'normal').
--
-- IMPORTANTE: aplícala ANTES de reconstruir el backend con el código de H6.
--
-- Cómo aplicarla (con el contenedor de la base en marcha; ajusta usuario y base a tu .env):
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 003_review_hitl.sql
-- Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_003.dump
-- Cómo volver atrás: 003_review_hitl_rollback.sql
-- =====================================================================================================
BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS escalate_at  timestamptz;
ALTER TABLE events ADD COLUMN IF NOT EXISTS escalated_at timestamptz;

ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS audience            text    NOT NULL DEFAULT 'normal';
ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS telegram_message_id bigint;
ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS has_photo           boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_alert_deliveries_audience') THEN
        ALTER TABLE alert_deliveries ADD CONSTRAINT ck_alert_deliveries_audience
            CHECK (audience IN ('normal', 'review', 'broadcast', 'escalated'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_events_pending_review ON events (community_id, escalate_at)
    WHERE escalate_at IS NOT NULL AND status = 'sin verificar';
CREATE INDEX IF NOT EXISTS ix_alert_deliveries_event ON alert_deliveries (event_id);

COMMIT;
