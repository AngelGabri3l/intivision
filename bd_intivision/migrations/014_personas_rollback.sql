-- Reversa de 014. Los vigilantes y familiares quedan como cuentas canceladas de residente (no pueden entrar); se borran las
-- invitaciones y las cámaras compartidas.

BEGIN;

DROP TABLE IF EXISTS invitations;
DROP TABLE IF EXISTS camera_shares;
UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND user_id IN (SELECT id FROM users WHERE role IN ('guard', 'family'));
UPDATE users SET role = 'resident', status = 'canceled' WHERE role IN ('guard', 'family');
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_family_sponsor;
DROP INDEX IF EXISTS ix_users_sponsor;
ALTER TABLE users DROP COLUMN IF EXISTS sponsor_id, DROP COLUMN IF EXISTS invited_by;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('resident', 'community_admin'));

COMMIT;
