-- 011 · Pagos y suscripciones: precios de mercado, pasarela de pagos simulada (estilo Stripe), plan de la comunidad, bajada de
-- plan, baja del servicio y renovación automática. Es idempotente. La reversa es 011_pagos_rollback.sql.

BEGIN;

-- Precios de equilibrio de mercado (soles con IGV, mensual)
ALTER TABLE plans ADD COLUMN IF NOT EXISTS community_price_pen_cents integer NOT NULL DEFAULT 0;   -- Premium de toda la comunidad
UPDATE plans SET price_pen_cents = 1990, community_price_pen_cents = 0     WHERE code = 'base';
UPDATE plans SET price_pen_cents = 3990, community_price_pen_cents = 29900 WHERE code = 'premium';

-- Tarjetas guardadas (solo marca y últimos 4 dígitos: el número nunca se guarda)
CREATE TABLE IF NOT EXISTS payment_methods (
    id           text        PRIMARY KEY,                       -- pm_test_...
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    brand        text        NOT NULL,
    last4        char(4)     NOT NULL,
    exp_month    smallint    NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year     smallint    NOT NULL CHECK (exp_year BETWEEN 2000 AND 2100),
    test_behavior text       NOT NULL DEFAULT 'ok',             -- tarjetas de prueba: 'ok' o 'falla_renovacion'
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Sesiones de pago (como Checkout Session de Stripe)
CREATE TABLE IF NOT EXISTS checkout_sessions (
    id           text        PRIMARY KEY,                       -- cs_test_...
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    scope        text        NOT NULL CHECK (scope IN ('vivienda', 'comunidad')),
    plan_code    text        NOT NULL REFERENCES plans (code),
    amount_cents integer     NOT NULL CHECK (amount_cents > 0),
    currency     char(3)     NOT NULL DEFAULT 'PEN',
    status       text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'complete', 'expired')),
    payment_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL DEFAULT now() + interval '30 minutes',
    completed_at timestamptz
);

-- Cobros (como Payment Intent de Stripe)
CREATE TABLE IF NOT EXISTS payments (
    id             text        PRIMARY KEY,                     -- pi_test_...
    user_id        uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    community_id   uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    scope          text        NOT NULL CHECK (scope IN ('vivienda', 'comunidad')),
    plan_code      text        NOT NULL REFERENCES plans (code),
    kind           text        NOT NULL CHECK (kind IN ('alta', 'renovacion')),
    amount_cents   integer     NOT NULL CHECK (amount_cents > 0),
    currency       char(3)     NOT NULL DEFAULT 'PEN',
    status         text        NOT NULL CHECK (status IN ('requires_action', 'succeeded', 'failed', 'canceled')),
    failure_code   text,
    failure_message text,
    payment_method_id text,
    card_brand     text,
    card_last4     char(4),
    receipt_number text        UNIQUE,                          -- B001-00000001 (boleta simulada)
    created_at     timestamptz NOT NULL DEFAULT now(),
    paid_at        timestamptz
);
CREATE INDEX IF NOT EXISTS ix_payments_user ON payments (user_id, created_at DESC);
CREATE SEQUENCE IF NOT EXISTS receipt_seq;

-- Eventos de la pasarela (como los webhooks de Stripe), firmados y registrados una sola vez
CREATE TABLE IF NOT EXISTS payment_events (
    id           text        PRIMARY KEY,                       -- evt_test_...
    type         text        NOT NULL,
    data         jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

-- Suscripción de la vivienda: qué pasa al final del período y la renovación
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS scheduled_action  text,
    ADD COLUMN IF NOT EXISTS payment_method_id text,
    ADD COLUMN IF NOT EXISTS renewal_attempts  smallint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at     timestamptz;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_subscriptions_scheduled') THEN
        ALTER TABLE subscriptions ADD CONSTRAINT ck_subscriptions_scheduled
            CHECK (scheduled_action IS NULL OR scheduled_action IN ('bajar_a_base', 'dar_de_baja'));
    END IF;
END $$;

-- Suscripción Premium de toda la comunidad (la paga el administrador)
CREATE TABLE IF NOT EXISTS community_subscriptions (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id         uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    paid_by              uuid        REFERENCES users (id) ON DELETE SET NULL,
    plan_code            text        NOT NULL REFERENCES plans (code),
    status               text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'past_due', 'canceled')),
    current_period_start timestamptz NOT NULL DEFAULT now(),
    current_period_end   timestamptz NOT NULL,
    cancel_at_period_end boolean     NOT NULL DEFAULT false,
    payment_method_id    text,
    renewal_attempts     smallint    NOT NULL DEFAULT 0,
    next_retry_at        timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    canceled_at          timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_live_community_subscription
    ON community_subscriptions (community_id) WHERE status IN ('active', 'past_due');

COMMIT;
