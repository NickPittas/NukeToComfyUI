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

import threading
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


# --- Convenience wrappers used by other modules ----------------------------

def all_nodes() -> Any:
    return call(lambda: _nuke.allNodes())


def find_bridge_node(bridge_id: str) -> Any:
    """Return the ComfyUIBridge node whose bridge_id matches, or None."""
    def _find() -> Any:
        for n in _nuke.allNodes("ComfyUIBridge"):
            k = n.knob("bridge_id")
            if k is not None and k.value() == bridge_id:
                return n
        return None
    return call(_find)


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
    call(_set)


def root_frame() -> int:
    return call(lambda: int(_nuke.frame()))


def execute_node(node: Any, frame: int) -> None:
    """Trigger a Nuke render for `node` at `frame`."""
    call(lambda: _nuke.execute(node, frame, frame))
