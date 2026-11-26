-- 017 · SOS vecinal: usa la tabla de comunicados (un SOS es un aviso urgente a toda la comunidad), con foto opcional de un evento
-- y la marca de «atendido». Es idempotente. La reversa es 017_sos_vecinal_rollback.sql.

BEGIN;

ALTER TABLE announcements
    ADD COLUMN IF NOT EXISTS kind        text   NOT NULL DEFAULT 'comunicado',   -- comunicado | sos | sos_atendido
    ADD COLUMN IF NOT EXISTS event_id    bigint REFERENCES events (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS parent_id   bigint REFERENCES announcements (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS resolved_at timestamptz,
    ADD COLUMN IF NOT EXISTS resolved_by uuid   REFERENCES users (id) ON DELETE SET NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_announcements_kind') THEN
        ALTER TABLE announcements ADD CONSTRAINT ck_announcements_kind CHECK (kind IN ('comunicado', 'sos', 'sos_atendido'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_announcements_sos ON announcements (community_id, created_at DESC) WHERE kind = 'sos';

COMMIT;
