-- =====================================================================================================
-- Migración 004 · H3: señal «cercanía entre personas» (proximity)
--
--   signals   agrega el motivo «proximity» al catálogo. El motor (H3) lo calcula cuando dos personas están muy juntas durante unos
--             segundos, y cada alerta lo guarda como una de sus señales (event_signals), igual que el merodeo o el grupo.
--
-- SE PUEDE EJECUTAR MÁS DE UNA VEZ y va dentro de una transacción. No modifica ningún evento, ninguna señal ni ninguna foto que ya
-- exista; si ya hay una señal con ese código (por ejemplo, con una descripción que alguien ajustó) se respeta tal cual.
-- Solo requiere la tabla signals del esquema.
--
-- ORDEN: aplícala ANTES de reconstruir el backend. El backend de esta fase NO pierde eventos si la aplicas después (un motivo que no
-- esté en el catálogo simplemente no se registra como señal), pero mientras no esté aplicada la cercanía no aparece en las alertas.
--
-- Cómo aplicarla (con el contenedor de la base en marcha; ajusta usuario y base a tu .env):
--     docker exec -i vigia-db psql -U vigia -d vigia -v ON_ERROR_STOP=1 < 004_signal_proximity.sql
-- Antes, haz un respaldo:
--     docker exec vigia-db pg_dump -U vigia -d vigia -Fc > respaldo_antes_004.dump
-- Cómo volver atrás: 004_signal_proximity_rollback.sql
-- =====================================================================================================
BEGIN;

INSERT INTO signals (code, description) VALUES ('proximity', 'Cercanía entre personas')
    ON CONFLICT (code) DO NOTHING;

COMMIT;

-- Comprobación (debe mostrar una fila):
SELECT code, description FROM signals WHERE code = 'proximity';
