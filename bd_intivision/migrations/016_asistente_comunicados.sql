-- 016 · Asistente «Pregúntale a tus cámaras» y comunicados vecinales (redactados con IA, enviados a toda la comunidad).
-- Es idempotente. La reversa es 016_asistente_comunicados_rollback.sql.

BEGIN;

-- Preguntas al asistente por persona y por día, según el plan (0 = sin asistente)
ALTER TABLE plans ADD COLUMN IF NOT EXISTS ai_assistant_daily integer NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plans_ai_assistant_daily') THEN
        ALTER TABLE plans ADD CONSTRAINT ck_plans_ai_assistant_daily CHECK (ai_assistant_daily BETWEEN 0 AND 10000);
    END IF;
END $$;
UPDATE plans SET ai_assistant_daily = 0  WHERE code = 'base';
UPDATE plans SET ai_assistant_daily = 30 WHERE code = 'premium';

-- Registro de preguntas y respuestas (para auditoría y para contar el tope diario)
CREATE TABLE IF NOT EXISTS assistant_queries (
    id           bigserial   PRIMARY KEY,
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    channel      text        NOT NULL CHECK (channel IN ('web', 'telegram')),
    question     text        NOT NULL,
    answer       text,
    tools_used   jsonb       NOT NULL DEFAULT '[]',
    status       text        NOT NULL CHECK (status IN ('ok', 'error', 'limite')),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_assistant_queries_user ON assistant_queries (user_id, created_at DESC);

-- Comunicados de la administración
CREATE TABLE IF NOT EXISTS announcements (
    id           bigserial   PRIMARY KEY,
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    author_id    uuid        REFERENCES users (id) ON DELETE SET NULL,
    audience     text        NOT NULL CHECK (audience IN ('todos', 'vecinos', 'vigilantes', 'administradores')),
    title        text        NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    body         text        NOT NULL CHECK (length(body) BETWEEN 1 AND 1500),
    ai_assisted  boolean     NOT NULL DEFAULT false,
    recipients   integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_announcements_community ON announcements (community_id, created_at DESC);

-- Un envío por Telegram por persona y comunicado
CREATE TABLE IF NOT EXISTS announcement_deliveries (
    id                  bigserial   PRIMARY KEY,
    announcement_id     bigint      NOT NULL REFERENCES announcements (id) ON DELETE CASCADE,
    user_id             uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    channel_id          uuid        NOT NULL REFERENCES notification_channels (id) ON DELETE CASCADE,
    status              text        NOT NULL DEFAULT 'pendiente' CHECK (status IN ('pendiente', 'enviado', 'omitido', 'error')),
    attempts            smallint    NOT NULL DEFAULT 0,
    telegram_message_id bigint,
    sent_at             timestamptz,
    UNIQUE (announcement_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_announcement_deliveries_pendiente ON announcement_deliveries (id) WHERE status = 'pendiente';

COMMIT;
