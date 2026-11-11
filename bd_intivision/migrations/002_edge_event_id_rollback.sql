-- =====================================================================================================
-- Vuelve atrás la migración 002 (H2).
--
-- QUÉ SE PIERDE: solo el identificador con el que la mini PC marca cada evento. NO se borra ningún evento
-- ni ninguna foto. Mientras esté revertida, un reenvío de la cola de una mini PC podría duplicar eventos.
--
-- Se puede ejecutar más de una vez. Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_rollback_002.dump
-- Aplicar:
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 002_edge_event_id_rollback.sql
-- =====================================================================================================
BEGIN;

DROP INDEX IF EXISTS ux_events_edge_event;
ALTER TABLE events DROP COLUMN IF EXISTS edge_event_id;

COMMIT;
