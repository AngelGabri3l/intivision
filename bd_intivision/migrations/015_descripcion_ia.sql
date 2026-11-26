-- 015 · Descripción automática con IA de la foto de cada evento (Base: una frase en ALTO; Premium: detallada en MEDIO y ALTO).
-- Es idempotente. La reversa es 015_descripcion_ia_rollback.sql.

BEGIN;

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS ai_description text    NOT NULL DEFAULT 'none',   -- none | short | detailed
    ADD COLUMN IF NOT EXISTS ai_daily_cap   integer NOT NULL DEFAULT 0;        -- descripciones por día y comunidad
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plans_ai_description') THEN
        ALTER TABLE plans ADD CONSTRAINT ck_plans_ai_description CHECK (ai_description IN ('none', 'short', 'detailed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plans_ai_daily_cap') THEN
        ALTER TABLE plans ADD CONSTRAINT ck_plans_ai_daily_cap CHECK (ai_daily_cap BETWEEN 0 AND 100000);
    END IF;
END $$;
UPDATE plans SET ai_description = 'short',    ai_daily_cap = 20  WHERE code = 'base';
UPDATE plans SET ai_description = 'detailed', ai_daily_cap = 500 WHERE code = 'premium';

-- La descripción de cada evento
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS ai_status       text,          -- pendiente | hecha | omitida | error
    ADD COLUMN IF NOT EXISTS ai_detail       text,          -- short | detailed
    ADD COLUMN IF NOT EXISTS ai_description  text,
    ADD COLUMN IF NOT EXISTS ai_sees_people  boolean,       -- false: la IA no ve personas (posible falsa alarma)
    ADD COLUMN IF NOT EXISTS ai_attempts     smallint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ai_described_at timestamptz;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_events_ai_status') THEN
        ALTER TABLE events ADD CONSTRAINT ck_events_ai_status CHECK (ai_status IS NULL OR ai_status IN ('pendiente', 'hecha', 'omitida', 'error'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_events_ai_pendiente ON events (id) WHERE ai_status = 'pendiente';
CREATE INDEX IF NOT EXISTS ix_events_ai_busqueda ON events USING gin (to_tsvector('spanish', coalesce(ai_description, '')));

-- Envío por Telegram, como respuesta al aviso con foto
ALTER TABLE alert_deliveries
    ADD COLUMN IF NOT EXISTS desc_status   text,
    ADD COLUMN IF NOT EXISTS desc_attempts smallint NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_alert_deliveries_desc') THEN
        ALTER TABLE alert_deliveries ADD CONSTRAINT ck_alert_deliveries_desc
            CHECK (desc_status IS NULL OR desc_status IN ('pendiente', 'enviado', 'omitido', 'error'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_deliveries_desc ON alert_deliveries (id) WHERE desc_status = 'pendiente';

-- El dueño (o el administrador, en zonas comunes) puede apagarla en una cámara
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS ai_enabled boolean NOT NULL DEFAULT true;

COMMIT;
