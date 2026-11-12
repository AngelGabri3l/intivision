-- =====================================================================================================
-- Vuelve atrás la migración 004 (H3).
--
-- QUÉ SE PIERDE: la marca «cercanía entre personas» de las alertas que ya la tenían (las filas de event_signals con ese código) y su
-- entrada en el catálogo. NO se borra ningún evento, ninguna foto ni ningún aviso: las alertas siguen con su nivel y su puntaje.
-- Si el backend de H3 sigue en marcha, no se pierde ningún evento: la cercanía simplemente deja de registrarse como señal.
--
-- Se puede ejecutar más de una vez. Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_rollback_004.dump
-- Aplicar:
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 004_signal_proximity_rollback.sql
-- =====================================================================================================
BEGIN;

DELETE FROM event_signals WHERE signal_code = 'proximity';
DELETE FROM signals WHERE code = 'proximity';

COMMIT;
