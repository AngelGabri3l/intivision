-- 006 · H13: cuánta CPU y memoria usa cada mini PC (se ve en «Equipos y cámaras»).
-- Es idempotente: se puede aplicar más de una vez. La reversa es 006_edge_host_stats_rollback.sql.

BEGIN;

ALTER TABLE edge_devices
    ADD COLUMN IF NOT EXISTS host_stats       jsonb,          -- lo último que reportó la mini PC: CPU promedio y pico, memoria usada y total
    ADD COLUMN IF NOT EXISTS host_reported_at timestamptz;    -- cuándo lo reportó (para marcar el dato como viejo)

COMMIT;
