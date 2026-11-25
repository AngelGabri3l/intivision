-- Reversa de 016: quita el asistente y los comunicados (se pierden los comunicados y el registro de preguntas).

BEGIN;

DROP TABLE IF EXISTS announcement_deliveries;
DROP TABLE IF EXISTS announcements;
DROP TABLE IF EXISTS assistant_queries;
ALTER TABLE plans DROP CONSTRAINT IF EXISTS ck_plans_ai_assistant_daily;
ALTER TABLE plans DROP COLUMN IF EXISTS ai_assistant_daily;

COMMIT;
