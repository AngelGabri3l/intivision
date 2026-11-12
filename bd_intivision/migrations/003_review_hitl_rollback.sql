-- =====================================================================================================
-- Vuelve atrás la migración 003 (H6).
--
-- QUÉ SE PIERDE: la marca de qué alertas estaban en revisión, cuándo se escalaron y el identificador de los mensajes de
-- Telegram. NO se borra ningún evento, ninguna foto ni ningún aviso. Las alertas que estaban esperando revisión dejan de
-- aparecer como pendientes (siguen como «sin verificar»).
-- Antes de volver atrás, devuelve el backend a la versión anterior a H6 (el código nuevo necesita estas columnas).
--
-- Se puede ejecutar más de una vez. Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_rollback_003.dump
-- Aplicar:
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 003_review_hitl_rollback.sql
-- =====================================================================================================
BEGIN;

DROP INDEX IF EXISTS ix_alert_deliveries_event;
DROP INDEX IF EXISTS ix_events_pending_review;
ALTER TABLE alert_deliveries DROP CONSTRAINT IF EXISTS ck_alert_deliveries_audience;
ALTER TABLE alert_deliveries DROP COLUMN IF EXISTS has_photo;
ALTER TABLE alert_deliveries DROP COLUMN IF EXISTS telegram_message_id;
ALTER TABLE alert_deliveries DROP COLUMN IF EXISTS audience;
ALTER TABLE events DROP COLUMN IF EXISTS escalated_at;
ALTER TABLE events DROP COLUMN IF EXISTS escalate_at;

COMMIT;
