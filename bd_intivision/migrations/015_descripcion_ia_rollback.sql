-- Reversa de 015: quita las descripciones con IA (los textos ya generados se pierden).

BEGIN;

ALTER TABLE cameras DROP COLUMN IF EXISTS ai_enabled;
DROP INDEX IF EXISTS ix_deliveries_desc;
ALTER TABLE alert_deliveries DROP CONSTRAINT IF EXISTS ck_alert_deliveries_desc;
ALTER TABLE alert_deliveries DROP COLUMN IF EXISTS desc_status, DROP COLUMN IF EXISTS desc_attempts;
DROP INDEX IF EXISTS ix_events_ai_busqueda;
DROP INDEX IF EXISTS ix_events_ai_pendiente;
ALTER TABLE events DROP CONSTRAINT IF EXISTS ck_events_ai_status;
ALTER TABLE events DROP COLUMN IF EXISTS ai_status, DROP COLUMN IF EXISTS ai_detail, DROP COLUMN IF EXISTS ai_description,
                   DROP COLUMN IF EXISTS ai_sees_people, DROP COLUMN IF EXISTS ai_attempts, DROP COLUMN IF EXISTS ai_described_at;
ALTER TABLE plans DROP CONSTRAINT IF EXISTS ck_plans_ai_description;
ALTER TABLE plans DROP CONSTRAINT IF EXISTS ck_plans_ai_daily_cap;
ALTER TABLE plans DROP COLUMN IF EXISTS ai_description, DROP COLUMN IF EXISTS ai_daily_cap;

COMMIT;
