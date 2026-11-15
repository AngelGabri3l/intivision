-- =====================================================================================================
-- Migración 005 · H8: ubicación de las cámaras para el mapa de calor
--
--   cameras   lat, lng                 la ubicación exacta de la cámara (la fija su dueño, o el administrador si es de zona común)
--             location_accuracy_m      la precisión que informó el GPS del teléfono, si se usó
--             location_set_at/_by      cuándo y quién la fijó
--             map_visible              el CONSENTIMIENTO del dueño de una cámara privada para que el administrador la vea en el mapa
--                                      (apagado por defecto)
--             map_visible_at           cuándo lo dio
--
-- LA UBICACIÓN EXACTA DE UNA CÁMARA PRIVADA ES EL DOMICILIO DE UNA PERSONA (dato personal, Ley 29733). Por eso solo la fija el dueño, no
-- aparece en el mapa sin su permiso expreso, se puede quitar en cualquier momento y se borra si la cámara se archiva. No hay ninguna
-- ubicación existente que migrar: todas las cámaras quedan sin ubicación y sin permiso.
--
-- SE PUEDE EJECUTAR MÁS DE UNA VEZ y va dentro de una transacción. No modifica ninguna fila existente.
-- Requiere las tablas cameras y users del esquema.
--
-- ORDEN: aplícala ANTES de reconstruir el backend. Si el backend se reconstruye primero, TODO sigue funcionando (asignar, reasignar, ver
-- cámaras) pero el mapa y «Asignar ubicación» responden «falta aplicar la migración 005» hasta que se aplique.
--
-- Cómo aplicarla (con el contenedor de la base en marcha; ajusta usuario y base a tu .env):
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 005_camera_location.sql
-- Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_de_005.dump
--
-- Cómo volver atrás: 005_camera_location_rollback.sql (BORRA las ubicaciones guardadas).
-- =====================================================================================================

BEGIN;

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS lat                 double precision,
    ADD COLUMN IF NOT EXISTS lng                 double precision,
    ADD COLUMN IF NOT EXISTS location_accuracy_m integer,
    ADD COLUMN IF NOT EXISTS location_set_at     timestamptz,
    ADD COLUMN IF NOT EXISTS location_set_by     uuid REFERENCES users (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS map_visible         boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS map_visible_at      timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cameras_ubicacion' AND conrelid = 'cameras'::regclass) THEN
        ALTER TABLE cameras ADD CONSTRAINT ck_cameras_ubicacion CHECK (
        (lat IS NULL) = (lng IS NULL)
        AND (lat IS NULL OR (lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180))
        AND (location_accuracy_m IS NULL OR location_accuracy_m BETWEEN 0 AND 100000)
        AND (map_visible = false OR lat IS NOT NULL));
    END IF;
END $$;

COMMIT;
