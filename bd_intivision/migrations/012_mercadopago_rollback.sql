-- Reversa de 012. Los cobros y suscripciones hechos con Mercado Pago se conservan, sin la columna que los vincula.

BEGIN;

DROP INDEX IF EXISTS ix_community_subs_provider_ref;
DROP INDEX IF EXISTS ix_subscriptions_provider_ref;
DROP INDEX IF EXISTS ix_payments_provider_ref;
DROP INDEX IF EXISTS ix_checkout_provider_ref;
ALTER TABLE community_subscriptions DROP COLUMN IF EXISTS provider_ref, DROP COLUMN IF EXISTS provider;
ALTER TABLE payments DROP COLUMN IF EXISTS provider_ref, DROP COLUMN IF EXISTS provider;
ALTER TABLE checkout_sessions DROP COLUMN IF EXISTS provider_ref, DROP COLUMN IF EXISTS provider;

COMMIT;
