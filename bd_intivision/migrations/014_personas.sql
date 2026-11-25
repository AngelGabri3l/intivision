-- 014 · Gestión de personas: roles Vigilante y Familiar, invitaciones de un solo uso y cámaras compartidas con familiares.
-- Es idempotente. La reversa es 014_personas_rollback.sql.

BEGIN;

-- Roles nuevos: guard (vigilante: zonas comunes) y family (familiar: lo que su vecino le comparte)
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('resident', 'community_admin', 'guard', 'family'));
ALTER TABLE users ADD COLUMN IF NOT EXISTS sponsor_id uuid REFERENCES users (id) ON DELETE SET NULL;   -- el vecino de un familiar
ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by uuid REFERENCES users (id) ON DELETE SET NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_family_sponsor') THEN
        ALTER TABLE users ADD CONSTRAINT ck_users_family_sponsor CHECK (role <> 'family' OR sponsor_id IS NOT NULL OR status <> 'active');
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_users_sponsor ON users (sponsor_id) WHERE sponsor_id IS NOT NULL;

-- Cámaras que un vecino comparte con un familiar
CREATE TABLE IF NOT EXISTS camera_shares (
    camera_id  uuid        NOT NULL REFERENCES cameras (id) ON DELETE CASCADE,
    user_id    uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    granted_by uuid        REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_camera_shares_user ON camera_shares (user_id);

-- Invitaciones: el enlace se guarda como huella SHA-256, vale 72 horas y se usa una vez
CREATE TABLE IF NOT EXISTS invitations (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id     uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    invited_by       uuid        REFERENCES users (id) ON DELETE SET NULL,
    role             text        NOT NULL CHECK (role IN ('resident', 'community_admin', 'guard', 'family')),
    full_name        text        NOT NULL CHECK (length(full_name) BETWEEN 1 AND 120),
    email            text,
    phone            text,
    sponsor_id       uuid        REFERENCES users (id) ON DELETE CASCADE,
    camera_ids       uuid[]      NOT NULL DEFAULT '{}',
    token_hash       text        NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    accepted_at      timestamptz,
    accepted_user_id uuid        REFERENCES users (id) ON DELETE SET NULL,
    revoked_at       timestamptz,
    CONSTRAINT ck_invitations_contacto CHECK (email IS NOT NULL OR phone IS NOT NULL),
    CONSTRAINT ck_invitations_familia CHECK ((role = 'family') = (sponsor_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_invitations_community ON invitations (community_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_invitations_sponsor ON invitations (sponsor_id) WHERE sponsor_id IS NOT NULL;

COMMIT;
