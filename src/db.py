"""
Database connection helpers for hydrex-optimiser.

All scripts should obtain connections through this module rather than
calling sqlite3.connect() directly.  This ensures:
  - WAL mode is always enabled (safe concurrent reads during writes)
  - Foreign keys are enforced
  - A consistent row_factory is set (sqlite3.Row for dict-like access)
  - Schema is applied on first open of a new database

Usage
-----
    from src.db import get_conn, db_conn

    # One-shot connection (caller manages close):
    conn = get_conn()
    conn.execute("SELECT ...")
    conn.close()

    # Context manager (auto-closes):
    with db_conn() as conn:
        conn.execute("INSERT ...")
        conn.commit()

    # Explicit path (e.g. in tests):
    with db_conn("/tmp/test.db") as conn:
        ...
"""

import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from config.settings import DATABASE_PATH


def get_conn(path: Optional[str] = None, *, row_factory: bool = False) -> sqlite3.Connection:
    """
    Open a sqlite3 connection with WAL mode and foreign key enforcement.

    Args:
        path: Path to the SQLite file.  Defaults to DATABASE_PATH from config.
        row_factory: If True, set conn.row_factory = sqlite3.Row so rows can
                     be accessed by column name as well as index.

    Returns:
        An open sqlite3.Connection.  The caller is responsible for closing it.
    """
    db_path = path or DATABASE_PATH
    conn = sqlite3.connect(db_path)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn(
    path: Optional[str] = None, *, row_factory: bool = False
) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that opens and cleanly closes a database connection.

    Rolls back any uncommitted transaction on error, then closes.

    Example::

        with db_conn() as conn:
            conn.execute("INSERT INTO ...")
            conn.commit()
    """
    conn = get_conn(path, row_factory=row_factory)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """
    Return the current schema version recorded in the database.

    Returns 0 if the schema_version table does not yet exist or is empty
    (i.e. a brand-new database).
    """
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        # Table does not exist yet.
        return 0


def apply_schema(path: Optional[str] = None) -> None:
    """
    Create all tables and indexes if they do not already exist, then run
    any pending migration steps.

    Safe to call on an existing database — base DDL uses
    CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS, and migration
    steps are only executed when the stored version is behind
    CURRENT_SCHEMA_VERSION.

    Migration log
    -------------
    Each migration that runs inserts a row into schema_version so the DB
    always knows exactly which version it is at.
    """
    import time
    from src.schema import ALL_TABLES, INDEXES, MIGRATIONS, CURRENT_SCHEMA_VERSION

    with db_conn(path) as conn:
        # 1. Ensure all tables and indexes exist (idempotent).
        for ddl in ALL_TABLES:
            conn.execute(ddl)
        for ddl in INDEXES:
            conn.execute(ddl)

        # 2. Determine current version.
        current = get_schema_version(conn)

        # 3. Run pending migration steps in order.
        for target_version, description, migration in MIGRATIONS:
            if target_version <= current:
                continue
            if callable(migration):
                migration(conn)
            elif migration:
                conn.execute(migration)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, notes) VALUES (?, ?, ?)",
                (target_version, int(time.time()), description),
            )
            current = target_version

        # 4. Stamp version 1 on a fresh database (no migrations defined yet).
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, notes) VALUES (?, ?, ?)",
                (CURRENT_SCHEMA_VERSION, int(time.time()), "initial schema"),
            )

        conn.commit()
