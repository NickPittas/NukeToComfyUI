"""Process-local registry of persistent files created by this bridge session.

`clear()` deletes only exact paths tracked in this Nuke process — never a
directory scan or prefix glob — so pre-existing and prior-session files are
untouched even when their names collide.
"""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_tracked: dict[str, bool] = {}


def track(path: str) -> None:
    """Register an exact path for cleanup by `clear()`."""
    p = os.path.abspath(str(path or ""))
    if not p:
        return
    with _lock:
        _tracked[p] = True


def clear() -> tuple[int, int]:
    """Delete all tracked files. Returns (removed, failed).

    Missing files are dropped silently. Files that fail to delete are kept
    tracked so a later `clear()` can retry them.
    """
    with _lock:
        pending = list(_tracked)
    removed = 0
    failed = 0
    for path in pending:
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            failed += 1
            continue
        with _lock:
            _tracked.pop(path, None)
    return removed, failed
