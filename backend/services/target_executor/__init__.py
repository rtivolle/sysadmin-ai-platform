"""
target_executor — least-privilege privileged execution boundary.

The executor is a self-contained, standard-library-only program that is the
**only** component of the platform ever invoked with privilege. It is installed
root-owned at ``/usr/local/libexec/sysadmin-target-exec`` and invoked as::

    sudo -n /usr/local/libexec/sysadmin-target-exec <verb> <args...>

It re-validates every operation independently against its own root-owned
allowlist (``/etc/sysadmin-target-exec/allowlist.json``) and refuses any side
effect that is not exactly allow-listed — before touching the filesystem or
invoking ``systemctl``.

This package deliberately imports nothing from the rest of the repository at
runtime so that the single ``main.py`` file can be copied, root-owned, to
``/usr/local/libexec/sysadmin-target-exec`` and executed with a clean, minimal
environment. See ``docs/security.md`` → "Target adapter privilege model".
"""

__version__ = "1.0.0"
