-- Reversa de 010: quita los beneficios por plan, el plan de la comunidad, la señal «Posible arma» y el estado del envío de clips.
-- Los clips ya guardados (media_files de tipo 'clip') y sus archivos NO se borran.

BEGIN;

DROP INDEX IF EXISTS ux_media_clip_event;
DROP INDEX IF EXISTS ix_deliveries_clip;
ALTER TABLE alert_deliveries DROP CONSTRAINT IF EXISTS ck_alert_deliveries_clip;
ALTER TABLE alert_deliveries
    DROP COLUMN IF EXISTS clip_status,
    DROP COLUMN IF EXISTS clip_attempts;

DELETE FROM event_signals WHERE signal_code = 'weapon';
DELETE FROM signals WHERE code = 'weapon';

ALTER TABLE communities DROP COLUMN IF EXISTS plan_code;

ALTER TABLE plans DROP CONSTRAINT IF EXISTS ck_plans_clip_seconds;
ALTER TABLE plans DROP CONSTRAINT IF EXISTS ck_plans_retention_days;
ALTER TABLE plans
    DROP COLUMN IF EXISTS weapon_detection,
    DROP COLUMN IF EXISTS high_precision,
    DROP COLUMN IF EXISTS clip_seconds,
    DROP COLUMN IF EXISTS retention_days,
    DROP COLUMN IF EXISTS weekly_report,
    DROP COLUMN IF EXISTS evidence_export;
UPDATE plans SET ai_detection = false, realtime_alerts = false, highlight_clips = false, event_history = false, storage_gb = 0
 WHERE code = 'base';

COMMIT;
