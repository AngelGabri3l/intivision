-- =====================================================================================================
-- Migración 002 · H2: identificador del evento que manda la mini PC (para no duplicar al reenviar)
--
--   events.edge_event_id   un UUID que genera la mini PC por cada evento. Si el mismo evento llega dos
--                          veces (porque la mini PC reenvía su cola tras un corte), el servidor lo reconoce
--                          y no lo guarda otra vez.
--   ux_events_edge_event   índice único por comunidad. Es parcial: los eventos que no vienen de una mini PC
--                          (navegador o video) no tienen este valor y no se ven afectados.
--
-- SE PUEDE EJECUTAR MÁS DE UNA VEZ y va dentro de una transacción: si algo falla, no queda a medias.
-- Requiere que la migración 001 ya esté aplicada.
--
-- Cómo aplicarla (con el contenedor de la base en marcha; ajusta usuario y base a tu .env):
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 002_edge_event_id.sql
-- Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_002.dump
-- Cómo volver atrás: 002_edge_event_id_rollback.sql
-- =====================================================================================================
BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS edge_event_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_edge_event ON events (community_id, edge_event_id)
    WHERE edge_event_id IS NOT NULL;

COMMIT;
