"""ComfyUIBridge Nuke node definition and knob layout.

Uses Nuke's pythonic Group API (`nuke.createNode("Group")` + knobs). All Nuke
calls are isolated in `napi`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from . import napi


NODE_CLASS_NAME = "ComfyUIBridge"

MASK_SOURCES = (
    "source alpha",
    "invert source alpha",
    "mask input",
    "invert mask input",
)
SEND_FORMATS = ("png8",)  # exr16 in Phase 2
SEND_COLORSPACES = ("raw", "sRGB")


def _new_bridge_id() -> str:
    return "bridge-" + uuid.uuid4().hex[:8]


def build_knobs(group_node: Any) -> None:
    """Add all bridge knobs to an existing Group node."""
    nuke: Any = napi._nuke  # local alias; only called inside napi.call
    napi.call(_append_knobs, group_node, nuke)


def _append_knobs(group_node: Any, nuke: Any) -> None:
    k = nuke.knob
    Tab = nuke.Tab_Knob
    Text = nuke.Text_Knob
    String = nuke.String_Knob
    Int = nuke.Int_Knob
    Bool = nuke.Boolean_Knob
    Enum = nuke.Enumeration_Knob
    Py = getattr(nuke, "PyScript_Knob", None)

    g = group_node

    g.addKnob(Tab("ComfyUI"))
    g.addKnob(Text("bridge_header", "ComfyUI Bridge"))

    bid = String("bridge_id", "bridge id")
    bid.setFlag(0x1000)  # READ_ONLY-ish flag, harmless if unsupported
    g.addKnob(bid)

    g.addKnob(String("host", "host"))
    g.addKnob(Int("port", "port"))
    g.addKnob(String("output_directory", "output_directory"))
    g.addKnob(String("prompt", "prompt"))
    g.addKnob(String("workflow_api_path", "workflow_api_path"))

    g.addKnob(Enum("mask_source", "mask_source", list(MASK_SOURCES)))
    g.addKnob(Enum("send_format", "send_format", list(SEND_FORMATS)))
    g.addKnob(Enum("send_colorspace", "send_colorspace", list(SEND_COLORSPACES)))

    g.addKnob(Bool("create_read_on_result", "create_read_on_result"))
    g.addKnob(String("status", "status"))
    g.addKnob(String("last_result", "last_result"))

    if Py is not None:
        g.addKnob(Py("run_workflow", "Run workflow"))


def create_bridge_node() -> Any:
    """Create a new ComfyUIBridge Group node with all knobs + defaults."""
    nuke: Any = napi._nuke

    def _create() -> Any:
        node = nuke.createNode("Group", "name ComfyUIBridge")
        node.addKnob(nuke.Text_Knob("help", "ComfyUI Bridge: see PROTOCOL.md"))
        return node

    node = napi.call(_create)
    build_knobs(node)

    # Default values.
    bid = _new_bridge_id()
    napi.set_knob_value(node, "bridge_id", bid)
    napi.set_knob_value(node, "host", "")
    napi.set_knob_value(node, "port", 0)
    napi.set_knob_value(node, "output_directory", "")
    napi.set_knob_value(node, "prompt", "")
    napi.set_knob_value(node, "workflow_api_path", "")
    napi.set_knob_value(node, "mask_source", MASK_SOURCES[0])
    napi.set_knob_value(node, "send_format", SEND_FORMATS[0])
    napi.set_knob_value(node, "send_colorspace", SEND_COLORSPACES[0])
    napi.set_knob_value(node, "create_read_on_result", True)
    napi.set_knob_value(node, "status", "ready")
    napi.set_knob_value(node, "last_result", "")
    return node


def register_node() -> None:
    """Register ComfyUIBridge in Nuke's node registry via Pythonic Group.

    Most Nuke versions auto-discover Group nodes named `Group`; we expose a
    convenience menu entry instead of a true IOP to keep this PNG proof simple.
    """
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        nuke.menu("Nodes").addCommand(
            "ComfyUI/ComfyUIBridge", lambda: create_bridge_node(), icon="ComfyUIBridge.png"
        )
    except Exception:
        # Menu may not be ready in all entry points; non-fatal.
        pass
