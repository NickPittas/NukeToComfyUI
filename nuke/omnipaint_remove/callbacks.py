"""Nuke onCreate callback for the OmniPaintRemove gizmo.

The `.gizmo` file defines only the native shell; this callback is the source of
truth for the dynamic knobs and per-instance defaults. All fills are "if empty",
so reloading a saved script keeps the user's values.

Registered from `nuke/init.py` (covers GUI, terminal, render-fanout) and again
from `nuke/menu.py` (idempotent — `_REGISTERED` guards double registration).
"""

from __future__ import annotations

from typing import Any

from comfyui_bridge import napi
from .node import NODE_CLASS_NAME, ensure_knobs, initialize_defaults

_REGISTERED = False


def _node_is_ours(node: Any) -> bool:
    if node is None:
        return False
    try:
        cls = node.Class()
    except Exception:
        cls = ""
    if cls == NODE_CLASS_NAME:
        return True
    try:
        # Group fallback / older saved nodes carry our runtime/status knobs.
        return node.knob("omnipaint_steps") is not None and node.knob("status") is not None
    except Exception:
        return False


def _resolve_node(arg: Any) -> Any:
    """Callback signature varies: may receive the node, or nothing."""
    if arg is not None and not isinstance(arg, str):
        try:
            if hasattr(arg, "Class") or hasattr(arg, "knob"):
                return arg
        except Exception:
            pass
    if napi.has_nuke():
        try:
            return napi._nuke.thisNode()
        except Exception:
            return None
    return None


def _on_create(node: Any = None) -> None:
    node = _resolve_node(node)
    if not _node_is_ours(node):
        return
    try:
        added = ensure_knobs(node)
        initialize_defaults(node, added)
    except Exception:
        # A callback must never break node creation.
        pass


def initialize_this_node() -> None:
    """Called from OmniPaintRemove.gizmo onCreate string."""
    _on_create(None)


def register() -> None:
    """Register the onCreate callback once. Idempotent; no-op outside Nuke."""
    global _REGISTERED
    if _REGISTERED or not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        nuke.addOnCreate(_on_create, nodeClass=NODE_CLASS_NAME)
    except TypeError:
        try:
            nuke.addOnCreate(_on_create)
        except Exception:
            return
    except Exception:
        return
    _REGISTERED = True
