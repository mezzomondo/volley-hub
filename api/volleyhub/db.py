from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10,
                               kwargs={"row_factory": dict_row}, open=True)
    return _pool


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def fetch_all(sql: str, params=None) -> list[dict]:
    with conn() as c:
        return c.execute(sql, params or {}).fetchall()


def fetch_one(sql: str, params=None) -> dict | None:
    with conn() as c:
        return c.execute(sql, params or {}).fetchone()
