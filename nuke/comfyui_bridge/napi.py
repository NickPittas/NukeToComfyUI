"""Nuke API isolation layer.

Every Nuke API call routes through here. When this code is imported outside of
Nuke (syntax check, smoke test), `nuke` is unavailable and all helpers degrade
gracefully. When running inside Nuke but on a worker thread, they dispatch to
the main thread via `nuke.executeInMainThreadWithResult`.

Pattern: a thin `_call(fn, *args, **kwargs)` that:
  - calls `fn` directly if `nuke` is not importable (returns None / sentinel);
  - calls `fn` directly if we are already on the Nuke main thread;
  - otherwise dispatches via `executeInMainThreadWithResult`.

Errors raised inside Nuke callbacks are captured and re-raised on the caller.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

try:  # guarded import: works outside Nuke
    import nuke as _nuke_mod  # type: ignore

    # The real Nuke python module exposes executeInMainThreadWithResult.
    # Our own `nuke/` directory can shadow the name as a namespace package,
    # so we verify it's actually Nuke, not just importable.
    if not hasattr(_nuke_mod, "executeInMainThreadWithResult"):
        raise ImportError("not the real Nuke module")
    _nuke: Any = _nuke_mod
    _HAS_NUKE = True
except Exception:  # pragma: no cover - outside Nuke
    _nuke = None
    _HAS_NUKE = False


_MAIN_THREAD_IDENT = threading.main_thread().ident


def has_nuke() -> bool:
    return _HAS_NUKE


def on_main_thread() -> bool:
    return threading.current_thread().ident == _MAIN_THREAD_IDENT


class NukeError(RuntimeError):
    """Raised when a Nuke API call fails or Nuke is unavailable."""


class _ResultBox:
    """Capture return value + exception from a main-thread call."""

    __slots__ = ("value", "error")

    def __init__(self) -> None:
        self.value: Any = None
        self.error: BaseException | None = None


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run `fn(*args, **kwargs)` on Nuke's main thread if needed.

    Outside Nuke this raises NukeError; callers that need to operate without
    Nuke should check `has_nuke()` first.
    """
    if not _HAS_NUKE:
        raise NukeError("nuke module is not available (running outside Nuke)")

    if on_main_thread():
        return fn(*args, **kwargs)

    box = _ResultBox()

    def _runner() -> None:
        try:
            box.value = fn(*args, **kwargs)
        except BaseException as exc:  # capture and re-raise on caller thread
            box.error = exc

    # executeInMainThreadWithResult returns a NukeThread; we call .result to
    # block until it finishes. Some Nuke versions return the value directly.
    task = _nuke.executeInMainThreadWithResult(_runner)
    result_getter = getattr(task, "result", None)
    if callable(result_getter):
        # Older API: pass the callable to wait. Newer: no-arg .result().
        try:
            result_getter()
        except TypeError:
            pass
    if box.error is not None:
        raise box.error
    return box.value


# --- Comp name sanitization ------------------------------------------------

_UNSAVED_STEMS = ("Root", "Untitled")


def sanitize_stem(name: str) -> str:
    """Map a comp path/name to a filename-safe stem.

    Takes the basename without extension, keeps only [A-Za-z0-9._-]
    characters, and falls back to `nuke_bridge` for empty, unsaved (Nuke's
    default root names `Root`/`Untitled`), or all-invalid names. Handles
    Windows and POSIX path separators.
    """
    base = os.path.basename(str(name or "").replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in "._-")
    if not safe or safe in _UNSAVED_STEMS:
        return "nuke_bridge"
    return safe


def comp_stem() -> str:
    """Filename-safe stem for the current comp, read via Nuke's root name.

    Falls back to `nuke_bridge` outside Nuke or for unsaved comps.
    """
    if not _HAS_NUKE:
        return "nuke_bridge"
    try:
        name = call(lambda: str(_nuke.root().name() or ""))
    except Exception:
        return "nuke_bridge"
    return sanitize_stem(name)


# --- Convenience wrappers used by other modules ----------------------------

def all_nodes() -> Any:
    return call(lambda: _nuke.allNodes())


def find_bridge_node(bridge_id: str) -> Any:
    """Return the ComfyUIBridge node whose bridge_id matches, or None."""
    def _find() -> Any:
        for n in _nuke.allNodes():
            k = n.knob("bridge_id")
            if k is not None and k.value() == bridge_id:
                return n
        return None
    return call(_find)


def find_default_bridge_node() -> Any:
    """Return selected bridge, the only bridge, or first bridge as fallback."""
    def _find() -> Any:
        bridges = [n for n in _nuke.allNodes() if n.knob("bridge_id") is not None]
        for n in _nuke.selectedNodes():
            if n.knob("bridge_id") is not None:
                return n
        if len(bridges) == 1:
            return bridges[0]
        return bridges[0] if bridges else None
    return call(_find)


def bridge_id_for_node(node: Any) -> str:
    return str(knob_value(node, "bridge_id") or "")


def knob_value(node: Any, name: str) -> Any:
    return call(lambda: node.knob(name).value())


def set_knob_value(node: Any, name: str, value: Any) -> None:
    def _set() -> None:
        k = node.knob(name)
        if k is None:
            return
        if isinstance(value, str):
            k.setValue(value)
        else:
            k.setValue(value)
        if name == "status" and isinstance(value, str):
            # Mirror status updates into the multiline log in the same
            # main-thread closure (append_log would add a redundant call hop).
            _append_log_sync(node, value)
    call(_set)


_LOG_MAX_LINES = 300


def _log_entry_text(line: str) -> str:
    """Return the level+message part of a log line, stripping the timestamp
    prefix so consecutive-duplicate detection ignores timestamps."""
    if line.startswith("[") and "] " in line:
        head, _, rest = line.partition("] ")
        if (
            len(head) == 9
            and head[1:3].isdigit()
            and head[4:6].isdigit()
            and head[7:9].isdigit()
        ):
            return rest
    return line


def _append_log_sync(node: Any, message: str, level: str = "INFO") -> None:
    """Append one `[HH:MM:SS] LEVEL message` line to the node's `log` knob.

    Main-thread only: skips consecutive duplicates of the same level+message
    (timestamp excluded) and keeps the last {_LOG_MAX_LINES} lines.
    """
    k = node.knob("log")
    if k is None:
        return
    entry = f"{level} {message}"
    current = str(k.value() or "")
    lines = current.splitlines()
    if lines and _log_entry_text(lines[-1]) == entry:
        return
    lines.append(f"[{time.strftime('%H:%M:%S')}] {entry}")
    if len(lines) > _LOG_MAX_LINES:
        del lines[: len(lines) - _LOG_MAX_LINES]
    k.setValue("\n".join(lines))


def append_log(node: Any, message: str, level: str = "INFO") -> None:
    """Append a timestamped line to the node's multiline `log` knob.

    Main-thread safe; capped to the last {_LOG_MAX_LINES} lines.
    """
    call(_append_log_sync, node, message, level)


def clear_log(node: Any) -> None:
    """Clear the node's multiline `log` knob. Main-thread safe."""
    def _clear() -> None:
        k = node.knob("log")
        if k is None:
            return
        k.setValue("")
    call(_clear)


def root_frame() -> int:
    return call(lambda: int(_nuke.frame()))


def execute_node(node: Any, frame: int) -> None:
    """Trigger a Nuke render for `node` at `frame`."""
    call(lambda: _nuke.execute(node, frame, frame))
