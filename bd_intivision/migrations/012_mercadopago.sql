-- 012 · Mercado Pago: de qué pasarela es cada sesión de pago, cobro y suscripción de la comunidad, y su identificador allí.
-- Es idempotente. La reversa es 012_mercadopago_rollback.sql.

BEGIN;

ALTER TABLE checkout_sessions
    ADD COLUMN IF NOT EXISTS provider     text NOT NULL DEFAULT 'simulada',
    ADD COLUMN IF NOT EXISTS provider_ref text;                    -- id de la suscripción (preapproval) en Mercado Pago
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS provider     text NOT NULL DEFAULT 'simulada',
    ADD COLUMN IF NOT EXISTS provider_ref text;                    -- la suscripción de Mercado Pago a la que pertenece el cobro
ALTER TABLE community_subscriptions
    ADD COLUMN IF NOT EXISTS provider     text NOT NULL DEFAULT 'pasarela_simulada',
    ADD COLUMN IF NOT EXISTS provider_ref text;
CREATE INDEX IF NOT EXISTS ix_checkout_provider_ref ON checkout_sessions (provider_ref) WHERE provider_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_payments_provider_ref ON payments (provider_ref) WHERE provider_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_subscriptions_provider_ref ON subscriptions (provider_ref) WHERE provider_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_community_subs_provider_ref ON community_subscriptions (provider_ref) WHERE provider_ref IS NOT NULL;

COMMIT;
