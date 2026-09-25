#!/usr/bin/env python3
"""Execution surfaces the control store is written against.

Stores depend on this narrow protocol (query / execute / transaction) instead of
a driver, which keeps the whole PostgreSQL leg unit-testable on a host with no
database and no driver installed.
"""
import contextlib
from typing import Any, ContextManager, Optional, Protocol, Sequence, runtime_checkable

from services.control_store.connection import Connection, connect


@runtime_checkable
class Executor(Protocol):
    """The three operations :class:`KeyStore` and the ledger need."""

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]: ...

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...

    def transaction(self) -> ContextManager[Any]: ...


class SingleConnectionExecutor:
    """Open one short-lived connection per operation.

    Nothing long-lived is pooled, so a store outage cannot leave a half-open
    handle behind, and the auth gateway's request threads never share a
    connection. The cost is one connect per operation: acceptable for the tens
    of operator requests per minute this store serves, and the honest tradeoff
    against a connection pool whose failure modes are harder to reason about.
    """

    def __init__(self, dsn: str, timeout_seconds: float = 5.0):
        self.dsn = dsn
        self.timeout_seconds = timeout_seconds

    def _connect(self) -> Connection:
        return connect(self.dsn, timeout_seconds=self.timeout_seconds)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self._connect() as conn:
            return conn.query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._connect() as conn:
            affected = conn.execute(sql, params)
            conn.commit()
            return affected

    @contextlib.contextmanager
    def transaction(self):
        with self._connect() as conn:
            with conn.transaction():
                yield conn


def open_executor(dsn: str, timeout_seconds: float = 5.0) -> SingleConnectionExecutor:
    return SingleConnectionExecutor(dsn, timeout_seconds=timeout_seconds)
