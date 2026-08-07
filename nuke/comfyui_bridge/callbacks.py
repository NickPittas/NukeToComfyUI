"""Nuke onCreate callback for ComfyUIBridge gizmo/group nodes.

The `.gizmo` file intentionally does not hand-write addUserKnob entries. This
callback is the source of truth for:

  - any knob missing on a given Nuke version (added via the Python API through
    `node.ensure_knobs`),
  - dynamic per-instance defaults: bridge_id uuid + host/port/output_directory
    from settings + comfyui host/port + status.

All fills are "if empty", so reloading a saved script keeps the user's values.
Called by the gizmo's onCreate string and registered from `init.py` as a safety
net for GUI, terminal, and render-fanout modes.
"""

from __future__ import annotations

from typing import Any

from . import napi
from .node import NODE_CLASS_NAME, ensure_knobs, initialize_defaults, sync_prompts


_REGISTERED = False


_PROMPT_KNOBS = ("prompt", "video_prompt")


def _node_is_ours(node: Any) -> bool:
    """True for the ComfyUIBridge gizmo (Class == name) or any node that
    already carries a bridge_id knob (Group fallback / older saved nodes)."""
    if node is None:
        return False
    try:
        cls = node.Class()
    except Exception:
        cls = ""
    if cls == NODE_CLASS_NAME:
        return True
    try:
        return node.knob("bridge_id") is not None
    except Exception:
        return False


def _resolve_node(arg: Any) -> Any:
    """Callback signature varies across Nuke versions: may receive the node,
    or nothing (use nuke.thisNode())."""
    if arg is not None and not isinstance(arg, str):
        # Some versions pass the node directly.
        try:
            if hasattr(arg, "Class") or hasattr(arg, "knob"):
                return arg
        except Exception:
            pass
    # Fall back to the current node in this creation context.
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
        ensure_knobs(node)
        initialize_defaults(node)
    except Exception:
        # A callback must never break node creation.
        pass


def initialize_this_node() -> None:
    """Called from ComfyUIBridge.gizmo onCreate string."""
    _on_create(None)


def _on_knob_changed() -> None:
    """Keep `prompt`/`video_prompt` in sync on any edit to either knob."""
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        node = nuke.thisNode()
        knob = nuke.thisKnob()
    except Exception:
        return
    if not _node_is_ours(node) or knob is None:
        return
    try:
        name = knob.name()
    except Exception:
        return
    if name not in _PROMPT_KNOBS:
        return
    try:
        sync_prompts(node, name)
    except Exception:
        # A callback must never break the edit the user is performing.
        pass


def register() -> None:
    """Register the onCreate callback once. Idempotent; no-op outside Nuke."""
    global _REGISTERED
    if _REGISTERED or not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    # Prefer the nodeClass-filtered callback so we don't run on every node.
    try:
        nuke.addOnCreate(_on_create, nodeClass=NODE_CLASS_NAME)
    except TypeError:
        # Older signature without nodeClass — filter inside _on_create.
        try:
            nuke.addOnCreate(_on_create)
        except Exception:
            return
    except Exception:
        return
    try:
        nuke.addKnobChanged(_on_knob_changed, nodeClass=NODE_CLASS_NAME)
    except TypeError:
        try:
            nuke.addKnobChanged(_on_knob_changed)
        except Exception:
            pass
    except Exception:
        pass
    _REGISTERED = True
