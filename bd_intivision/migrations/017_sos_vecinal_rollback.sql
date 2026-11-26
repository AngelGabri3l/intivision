-- Reversa de 017: quita los SOS (y sus avisos de «atendido») y las columnas que los soportan. Los comunicados se conservan.

BEGIN;

DELETE FROM announcements WHERE kind <> 'comunicado';
DROP INDEX IF EXISTS ix_announcements_sos;
ALTER TABLE announcements DROP CONSTRAINT IF EXISTS ck_announcements_kind;
ALTER TABLE announcements DROP COLUMN IF EXISTS kind, DROP COLUMN IF EXISTS event_id, DROP COLUMN IF EXISTS parent_id,
                          DROP COLUMN IF EXISTS resolved_at, DROP COLUMN IF EXISTS resolved_by;

COMMIT;
