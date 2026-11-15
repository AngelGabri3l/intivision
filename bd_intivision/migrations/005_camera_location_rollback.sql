-- =====================================================================================================
-- Vuelve atrás la migración 005 (ubicación de las cámaras).
-- BORRA las ubicaciones y los permisos de «mostrar en el mapa» guardados: no se pueden recuperar sin un respaldo.
-- Haz un respaldo antes:  docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_de_rollback_005.dump
-- Aplícala con:  docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 005_camera_location_rollback.sql
-- Se puede ejecutar más de una vez.
-- =====================================================================================================

BEGIN;

ALTER TABLE cameras DROP CONSTRAINT IF EXISTS ck_cameras_ubicacion;
ALTER TABLE cameras
    DROP COLUMN IF EXISTS map_visible_at,
    DROP COLUMN IF EXISTS map_visible,
    DROP COLUMN IF EXISTS location_set_by,
    DROP COLUMN IF EXISTS location_set_at,
    DROP COLUMN IF EXISTS location_accuracy_m,
    DROP COLUMN IF EXISTS lng,
    DROP COLUMN IF EXISTS lat;

COMMIT;
