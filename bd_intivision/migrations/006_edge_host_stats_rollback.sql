-- Reversa de 006: quita lo que se guardaba del uso de cada mini PC (los equipos y sus cámaras no se tocan).

BEGIN;

ALTER TABLE edge_devices
    DROP COLUMN IF EXISTS host_stats,
    DROP COLUMN IF EXISTS host_reported_at;

COMMIT;
