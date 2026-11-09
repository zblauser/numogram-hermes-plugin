"""The environment every program numogram starts gets: an allowlist, never Hermes's own.

idf.py runs the project's own CMake and Python scripts, and esptool and QEMU are
third-party programs. None of them needs the provider keys and tokens Hermes holds.
"""
from __future__ import annotations

import os
import sys

BASE = ("HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR", "TZ")


def system_path() -> str:
    """PATH without the venv Hermes runs in. ESP-IDF's export.sh refuses to run inside one."""
    venv = os.environ.get("VIRTUAL_ENV") or (sys.prefix if sys.prefix != sys.base_prefix else None)
    bad = os.path.join(venv, "bin") if venv else None
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p.rstrip("/") != bad]
    return os.pathsep.join(parts) or os.defpath


def minimal_env(*keep: str, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """BASE, locale (LC_*), a clean PATH, plus the names in keep and any variable starting with prefixes."""
    env = {k: v for k, v in os.environ.items()
           if k in BASE or k in keep or k.startswith(("LC_",) + tuple(prefixes))}
    env["PATH"] = system_path()
    return env
