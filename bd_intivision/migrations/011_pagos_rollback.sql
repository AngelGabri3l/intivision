-- Reversa de 011: quita pagos, sesiones de pago, tarjetas guardadas, eventos y la suscripción de la comunidad, y vuelve a los
-- precios anteriores. Las suscripciones de las viviendas se conservan (sin la acción programada ni la renovación).

BEGIN;

DROP TABLE IF EXISTS community_subscriptions;
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS ck_subscriptions_scheduled;
ALTER TABLE subscriptions
    DROP COLUMN IF EXISTS scheduled_action,
    DROP COLUMN IF EXISTS payment_method_id,
    DROP COLUMN IF EXISTS renewal_attempts,
    DROP COLUMN IF EXISTS next_retry_at;
DROP TABLE IF EXISTS payment_events;
DROP TABLE IF EXISTS payments;
DROP SEQUENCE IF EXISTS receipt_seq;
DROP TABLE IF EXISTS checkout_sessions;
DROP TABLE IF EXISTS payment_methods;
UPDATE plans SET price_pen_cents = 1490 WHERE code = 'base';
UPDATE plans SET price_pen_cents = 10990 WHERE code = 'premium';
ALTER TABLE plans DROP COLUMN IF EXISTS community_price_pen_cents;

COMMIT;
