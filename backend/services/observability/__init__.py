"""
Local observability: stdlib-only metrics collector and alert evaluator.

The collector scrapes service health, the audit outbox, backup freshness,
host resources (disk/RAM/load/GPU) and Valkey from *outside* the services —
it imports nothing from the service modules and mutates nothing. The alert
evaluator turns the collected samples into Prometheus-style alerts with
pending -> firing -> resolved state transitions, persisted under
``backend/data/observability/``.

Both tools are pure standard-library Python (no ``httpx``, no ``redis``, no
third-party runtime dependencies) so they can run from a bare ``python3``
outside the service venv.
"""

import importlib

__all__ = ["collector", "alerts"]


def __getattr__(name):
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
