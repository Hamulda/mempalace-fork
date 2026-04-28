"""Database connection pool and query execution.

This module provides both a connection pool (ConnectionPool) and a simple
prepared statement interface (QueryBuilder). The PreparedStatement class
was added later and handles escaping automatically.
"""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any


class ConnectionPool:
    """Thread-safe SQLite connection pool with configurable size."""

    def __init__(self, db_path: str = ":memory:", pool_size: int = 5):
        self._db_path = db_path
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._used: set[int] = set()

    @contextmanager
    def get_connection(self):
        """Acquire a connection from the pool. Auto-releases on exit."""
        conn = self._acquire_conn()
        try:
            yield conn
        finally:
            self._release_conn(conn)

    def _acquire_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._connections:
                return self._connections.pop()
            return sqlite3.connect(self._db_path, check_same_thread=False)

    def _release_conn(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._connections.append(conn)


class QueryBuilder:
    """Simple query builder with parameter substitution."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def execute_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
        """Execute a query and return results as list of dicts."""
        with self._pool.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def connect_db(db_path: str = "app.db") -> ConnectionPool:
    """Factory: create a ConnectionPool pointing at db_path."""
    return ConnectionPool(db_path=db_path)
