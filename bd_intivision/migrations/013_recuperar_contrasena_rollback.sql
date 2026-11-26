-- Reversa de 013: quita los enlaces de recuperación (los vigentes dejan de servir; las contraseñas ya cambiadas se conservan).

BEGIN;

DROP TABLE IF EXISTS password_resets;

COMMIT;
