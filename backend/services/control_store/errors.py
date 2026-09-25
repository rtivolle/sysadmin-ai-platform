#!/usr/bin/env python3
"""Failure types for the durable control store (the PostgreSQL leg).

The platform's guardrail (AGENTS.md §4) is that a store which is *required*
must fail closed. Every "the configured durable store cannot be reached"
condition therefore derives from :class:`ConnectionError`: the auth gateway,
the agent platform and the quota manager already translate that type into HTTP
503, so an outage can never silently degrade into an unverified in-memory
ledger.
"""


class ControlStoreError(RuntimeError):
    """A configuration or programming error in the durable control store."""


class ControlStoreUnavailable(ConnectionError):
    """The store is configured but unreachable, or its driver is not installed.

    Deriving from :class:`ConnectionError` is deliberate: callers fail closed
    (503) instead of continuing with local state that the durable store exists
    to make authoritative.
    """


class ControlStoreIntegrityError(ControlStoreError):
    """A write that must not be dropped was rejected by the store."""
