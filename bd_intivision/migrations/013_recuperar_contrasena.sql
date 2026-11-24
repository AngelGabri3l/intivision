-- 013 · Recuperar la contraseña: enlaces de un solo uso (se guardan como huella, nunca el enlace). Es idempotente.
-- La reversa es 013_recuperar_contrasena_rollback.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS password_resets (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash   text        NOT NULL UNIQUE,                       -- sha256 del enlace
    channel      text        NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram', 'soporte')),
    requested_ip inet,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz
);
CREATE INDEX IF NOT EXISTS ix_password_resets_user ON password_resets (user_id, created_at DESC);

COMMIT;
