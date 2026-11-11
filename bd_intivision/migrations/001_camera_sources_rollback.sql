-- =====================================================================================================
-- Vuelve atrás la migración 001 (H4).
--
-- QUÉ SE PIERDE: la tabla camera_sources completa (el inventario que reportaron las mini PC; se vuelve
-- a llenar sola cuando cada mini PC envíe su inventario), el vínculo de cada cámara con su fuente, las
-- marcas de "cámara sin señal" y quién revisó cada evento. NO se borra ninguna cámara ni ningún evento.
--
-- Se puede ejecutar más de una vez. Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_rollback_001.dump
-- Aplicar:
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 001_camera_sources_rollback.sql
-- =====================================================================================================
BEGIN;

ALTER TABLE cameras DROP CONSTRAINT IF EXISTS fk_cameras_source;
ALTER TABLE cameras DROP CONSTRAINT IF EXISTS ux_cameras_source;
ALTER TABLE events  DROP CONSTRAINT IF EXISTS fk_events_reviewed_by;
DROP INDEX IF EXISTS ix_cameras_watch;

ALTER TABLE cameras DROP COLUMN IF EXISTS source_id;
ALTER TABLE cameras DROP COLUMN IF EXISTS offline_notified_at;
ALTER TABLE events  DROP COLUMN IF EXISTS reviewed_by;
ALTER TABLE events  DROP COLUMN IF EXISTS reviewed_at;

DROP TABLE IF EXISTS camera_sources;

COMMIT;
