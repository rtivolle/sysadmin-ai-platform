#!/usr/bin/env python3
"""A minimal, fail-closed PostgreSQL connection wrapper.

The driver is imported lazily. The platform's test suite must stay runnable on a
host with no PostgreSQL driver installed, so importing this module never
imports psycopg; only :func:`connect` does, and a missing driver is reported as
:class:`ControlStoreUnavailable` (fail closed) rather than a bare ImportError.
"""
import contextlib
from typing import Any, Iterable, Optional, Sequence

from services.control_store.dsn import redact
from services.control_store.errors import ControlStoreUnavailable


def _load_driver() -> tuple[str, Any]:
    """Return (name, module) for an installed PostgreSQL driver."""
    try:
        import psycopg  # type: ignore

        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return "psycopg2", psycopg2
    except ImportError:
        pass
    raise ControlStoreUnavailable(
        "No PostgreSQL driver is installed (expected 'psycopg' or 'psycopg2'). "
        "Install the platform requirements before enabling SYSADMIN_CONTROL_STORE=postgres."
    )


class Connection:
    """DB-API facade with the two operations the control store needs."""

    def __init__(self, raw: Any, driver: str, dsn: Optional[str] = None):
        self._raw = raw
        self.driver = driver
        self._dsn = dsn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        """Run a SELECT and return every row as a tuple."""
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
        except Exception as exc:  # driver errors are normalized to fail closed
            raise ControlStoreUnavailable(self._error("query", exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                cursor.close()
        return [tuple(row) for row in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement and return the affected row count."""
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, tuple(params))
            affected = cursor.rowcount
        except Exception as exc:
            raise ControlStoreUnavailable(self._error("execute", exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                cursor.close()
        return -1 if affected is None else int(affected)

    def execute_many(self, statements: Iterable[str]) -> None:
        for statement in statements:
            self.execute(statement)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        with contextlib.suppress(Exception):
            self._raw.rollback()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._raw.close()

    @contextlib.contextmanager
    def transaction(self):
        """Commit on success, roll back on any failure."""
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        self.commit()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            with contextlib.suppress(Exception):
                self.commit()
        self.close()

    def _error(self, operation: str, exc: Exception) -> str:
        return (
            f"PostgreSQL {operation} failed against {redact(self._dsn)}: "
            f"{exc.__class__.__name__}"
        )


def connect(dsn: str, timeout_seconds: float = 5.0) -> Connection:
    """Open a connection, or raise :class:`ControlStoreUnavailable`."""
    driver_name, driver = _load_driver()
    try:
        if driver_name == "psycopg":
            raw = driver.connect(dsn, connect_timeout=int(max(1, timeout_seconds)))
            raw.autocommit = False
        else:
            raw = driver.connect(dsn, connect_timeout=int(max(1, timeout_seconds)))
    except Exception as exc:
        raise ControlStoreUnavailable(
            f"PostgreSQL is unreachable at {redact(dsn)}: {exc.__class__.__name__}"
        ) from exc
    return Connection(raw, driver_name, dsn)


def check_ready(dsn: str, timeout_seconds: float = 5.0) -> bool:
    """Return True when the store answers ``SELECT 1``, False when it cannot.

    Used by health/status surfaces; callers that *require* the store must still
    call :func:`connect` and let the exception propagate.
    """
    try:
        with connect(dsn, timeout_seconds=timeout_seconds) as conn:
            conn.query("SELECT 1")
        return True
    except ControlStoreUnavailable:
        return False
