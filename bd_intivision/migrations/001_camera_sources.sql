-- =====================================================================================================
-- Migración 001 · H4: fuentes de cámara, asignación y aviso de "cámara sin señal"
--
--   camera_sources              las cámaras tal como las ve cada mini PC (USB, IP, ONVIF)
--   cameras.source_id           de qué fuente viene una cámara (única: una fuente = una cámara activa)
--   cameras.offline_notified_at cuándo se avisó de que dejó de dar señal (evita avisar dos veces)
--   events.reviewed_by / _at    quién revisó un evento y cuándo (lo usa H6)
--
-- SE PUEDE EJECUTAR MÁS DE UNA VEZ: no duplica nada ni pierde datos. Va dentro de una transacción:
-- si algo falla, no queda a medias.
--
-- Cómo aplicarla (con el contenedor de la base en marcha; ajusta usuario y base a tu .env):
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 001_camera_sources.sql
-- Antes de aplicarla, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_001.dump
-- Cómo volver atrás: 001_camera_sources_rollback.sql
-- =====================================================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS camera_sources (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id   uuid        NOT NULL,
    edge_device_id uuid        NOT NULL,
    device_key     text        NOT NULL CHECK (char_length(device_key) BETWEEN 1 AND 300),
    kind           text        NOT NULL CHECK (kind IN ('usb', 'rtsp', 'onvif')),
    label          text        NOT NULL CHECK (char_length(label) BETWEEN 1 AND 200),
    state          text        NOT NULL CHECK (state IN ('available', 'disconnected', 'error')),
    caps           jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- solo formatos y resolución máxima
    address        text,                                       -- IP o nombre del equipo (nunca una URL)
    detail         text,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz,                                -- la última vez que la mini PC la vio disponible
    reported_at    timestamptz NOT NULL DEFAULT now(),         -- el último inventario que la mencionó
    UNIQUE (edge_device_id, device_key),
    CONSTRAINT ux_sources_id_community UNIQUE (id, community_id),
    FOREIGN KEY (edge_device_id, community_id)
        REFERENCES edge_devices (id, community_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_sources_edge ON camera_sources (edge_device_id);

ALTER TABLE cameras ADD COLUMN IF NOT EXISTS source_id           uuid;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS offline_notified_at timestamptz;
ALTER TABLE events  ADD COLUMN IF NOT EXISTS reviewed_by         uuid;
ALTER TABLE events  ADD COLUMN IF NOT EXISTS reviewed_at         timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ux_cameras_source') THEN
        ALTER TABLE cameras ADD CONSTRAINT ux_cameras_source UNIQUE (source_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_cameras_source') THEN
        ALTER TABLE cameras ADD CONSTRAINT fk_cameras_source FOREIGN KEY (source_id, community_id)
            REFERENCES camera_sources (id, community_id) ON DELETE SET NULL (source_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_events_reviewed_by') THEN
        ALTER TABLE events ADD CONSTRAINT fk_events_reviewed_by FOREIGN KEY (reviewed_by, community_id)
            REFERENCES users (id, community_id) ON DELETE SET NULL (reviewed_by);
    END IF;
END $$;

-- La revisión de "cámara sin señal" solo mira cámaras activas que dependen de una mini PC.
CREATE INDEX IF NOT EXISTS ix_cameras_watch ON cameras (last_seen_at)
    WHERE is_active AND edge_device_id IS NOT NULL;

COMMIT;
