-- 010 · Plan Premium: los beneficios de cada plan, el plan de la comunidad, la señal «Posible arma» y el envío de clips por Telegram.
-- Es idempotente: se puede aplicar más de una vez. La reversa es 010_plan_premium_rollback.sql.
-- (Los números 007, 008 y 009 quedan reservados para H14, H15 y H17.)

BEGIN;

-- Beneficios de cada plan
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS weapon_detection boolean  NOT NULL DEFAULT false,   -- detección de posibles armas
    ADD COLUMN IF NOT EXISTS high_precision   boolean  NOT NULL DEFAULT false,   -- modelo de detección más preciso
    ADD COLUMN IF NOT EXISTS clip_seconds     smallint NOT NULL DEFAULT 0,       -- duración del clip del evento (0 = sin clip)
    ADD COLUMN IF NOT EXISTS retention_days   smallint NOT NULL DEFAULT 7,       -- cuántos días se guardan fotos y clips
    ADD COLUMN IF NOT EXISTS weekly_report    boolean  NOT NULL DEFAULT false,   -- resumen semanal por Telegram
    ADD COLUMN IF NOT EXISTS evidence_export  boolean  NOT NULL DEFAULT false;   -- descarga de evidencia con huella SHA-256
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plans_clip_seconds') THEN
        ALTER TABLE plans ADD CONSTRAINT ck_plans_clip_seconds CHECK (clip_seconds BETWEEN 0 AND 60);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plans_retention_days') THEN
        ALTER TABLE plans ADD CONSTRAINT ck_plans_retention_days CHECK (retention_days BETWEEN 1 AND 3650);
    END IF;
END $$;

-- Base: lo que la plataforma ya ofrece (personas y postura, alertas con foto, historial de 7 días).
-- Premium: además clip de 20 s, posibles armas, modelo más preciso, 90 días, resumen semanal y descarga de evidencia.
INSERT INTO plans (code, name, price_pen_cents, live_view, ai_detection, realtime_alerts, highlight_clips, event_history, storage_gb,
                   weapon_detection, high_precision, clip_seconds, retention_days, weekly_report, evidence_export)
VALUES ('base',    'Plan Base',    1490,  true, true, true, false, true, 0,  false, false, 0,  7,  false, false),
       ('premium', 'Plan Premium', 10990, true, true, true, true,  true, 50, true,  true,  20, 90, true,  true)
ON CONFLICT (code) DO UPDATE SET
    live_view = EXCLUDED.live_view, ai_detection = EXCLUDED.ai_detection, realtime_alerts = EXCLUDED.realtime_alerts,
    highlight_clips = EXCLUDED.highlight_clips, event_history = EXCLUDED.event_history, storage_gb = EXCLUDED.storage_gb,
    weapon_detection = EXCLUDED.weapon_detection, high_precision = EXCLUDED.high_precision, clip_seconds = EXCLUDED.clip_seconds,
    retention_days = EXCLUDED.retention_days, weekly_report = EXCLUDED.weekly_report, evidence_export = EXCLUDED.evidence_export;

-- Plan de la comunidad (cobro agrupado). Las cámaras privadas también pueden ser Premium por la suscripción de su dueño.
ALTER TABLE communities ADD COLUMN IF NOT EXISTS plan_code text NOT NULL DEFAULT 'base' REFERENCES plans (code);

-- Señal del detector de armas
INSERT INTO signals (code, description) VALUES ('weapon', 'Posible arma') ON CONFLICT (code) DO NOTHING;

-- Envío del clip por Telegram, como respuesta al aviso con foto
ALTER TABLE alert_deliveries
    ADD COLUMN IF NOT EXISTS clip_status   text,
    ADD COLUMN IF NOT EXISTS clip_attempts smallint NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_alert_deliveries_clip') THEN
        ALTER TABLE alert_deliveries ADD CONSTRAINT ck_alert_deliveries_clip
            CHECK (clip_status IS NULL OR clip_status IN ('pendiente', 'enviado', 'error', 'omitido'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_deliveries_clip ON alert_deliveries (id) WHERE clip_status = 'pendiente';

-- Un solo clip por evento
CREATE UNIQUE INDEX IF NOT EXISTS ux_media_clip_event ON media_files (event_id) WHERE kind = 'clip';

COMMIT;
