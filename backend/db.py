import contextlib
import os

import psycopg2
import psycopg2.extras
import psycopg2.pool

_pool = None


def dsn():
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError(
            "Falta la variable de entorno DATABASE_URL "
            "(ej. postgresql://vigia:CLAVE@vigia-db:5432/vigia)")
    return value


def get_pool():
    global _pool
    if _pool is None:
        # Si la conexión falla, _pool sigue en None y se reintenta en la siguiente petición.
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, int(os.environ.get("DB_POOL_MAX", "10")), dsn())
    return _pool


def reset_pool():
    """Cierra el pool (se usa en las pruebas y al cambiar de base)."""
    global _pool
    if _pool is not None:
        _pool.closeall()
    _pool = None


def _checkout(pool):
    """
    Entrega una conexión que funciona. Si la base se reinició o cortó las conexiones, las que
    estaban guardadas en el pool quedan rotas: se comprueban con un SELECT 1 y las rotas se
    descartan, para que la aplicación se recupere sola sin devolver errores al usuario.
    """
    for _ in range(pool.maxconn + 1):
        conn = pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.OperationalError("conexión cerrada")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()              # cierra la transacción que abrió el SELECT
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pool.putconn(conn, close=True)
    raise psycopg2.OperationalError("No se pudo obtener una conexión válida a la base de datos")


@contextlib.contextmanager
def connection():
    pool = get_pool()
    conn = _checkout(pool)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextlib.contextmanager
def cursor():
    """Cursor que devuelve filas como diccionarios; confirma al salir sin errores."""
    with connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def check():
    with cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        return cur.fetchone()["ok"] == 1
