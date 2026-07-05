"""ComfyUIBridge Nuke Group factory and knob layout."""

from __future__ import annotations

import uuid
from typing import Any

from . import napi
from .settings import load_settings


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
    Tab = nuke.Tab_Knob
    String = nuke.String_Knob
    Multiline = getattr(nuke, "Multiline_Eval_String_Knob", String)
    File = getattr(nuke, "File_Knob", String)
    Int = nuke.Int_Knob
    Bool = nuke.Boolean_Knob
    Enum = nuke.Enumeration_Knob
    Py = getattr(nuke, "PyScript_Knob", None)

    g = group_node

    g.addKnob(Tab("ComfyUI"))

    bid = String("bridge_id", "bridge id")
    bid.setFlag(0x1000)  # READ_ONLY-ish flag, harmless if unsupported
    g.addKnob(bid)

    g.addKnob(String("host", "host"))
    g.addKnob(Int("port", "port"))
    g.addKnob(String("comfyui_host", "ComfyUI host"))
    g.addKnob(Int("comfyui_port", "ComfyUI port"))
    g.addKnob(File("output_directory", "output directory"))
    g.addKnob(Multiline("prompt", "prompt"))
    g.addKnob(Enum("mask_source", "mask_source", list(MASK_SOURCES)))
    g.addKnob(Enum("send_format", "send_format", list(SEND_FORMATS)))
    g.addKnob(Enum("send_colorspace", "send_colorspace", list(SEND_COLORSPACES)))

    # Open-workflow dropdown. Choices are repopulated by `refresh_workflows`.
    g.addKnob(Enum("workflow_choices", "workflow", ["(none)"]))

    g.addKnob(Bool("create_read_on_result", "create_read_on_result"))
    g.addKnob(String("status", "status"))
    g.addKnob(String("last_result", "last_result"))

    if Py is not None:
        save = Py("save_defaults", "Save defaults")
        save.setValue(
            "from comfyui_bridge import node_settings; "
            "node_settings.save_defaults_from_node(nuke.thisNode())"
        )
        g.addKnob(save)

        clear_cache = Py("clear_frame_cache", "Clear frame cache")
        clear_cache.setValue(
            "from comfyui_bridge import render; "
            "render.clear_cache_from_node(nuke.thisNode())"
        )
        g.addKnob(clear_cache)

        refresh = Py("refresh_workflows", "Refresh workflows")
        refresh.setValue(
            "from comfyui_bridge import workflow_selection; "
            "workflow_selection.refresh_workflow_choices(nuke.thisNode())"
        )
        g.addKnob(refresh)

        run_sel = Py("run_selected_workflow", "Run selected workflow")
        run_sel.setValue(
            "from comfyui_bridge import workflow_selection; "
            "workflow_selection.run_selected_workflow(nuke.thisNode())"
        )
        g.addKnob(run_sel)


def create_bridge_node() -> Any:
    """Create a new ComfyUIBridge Group node with all knobs + defaults."""
    nuke: Any = napi._nuke

    def _create() -> Any:
        node = nuke.createNode("Group", inpanel=False)
        node.setName("ComfyUIBridge")
        try:
            node["tile_color"].setValue(int("355F8CFF", 16))
        except Exception:
            pass

        # Real passthrough Group: external input 0 -> internal Output.
        # The second Input exposes optional external input 1 for masks; it is
        # not wired to the passthrough output.
        node.begin()
        try:
            inp = nuke.createNode("Input", inpanel=False)
            inp.setName("source")
            mask = nuke.createNode("Input", inpanel=False)
            mask.setName("mask")
            out = nuke.createNode("Output", inpanel=False)
            out.setInput(0, inp)
        finally:
            node.end()
        return node

    node = napi.call(_create)
    build_knobs(node)

    # Default values.
    settings = load_settings()
    bid = _new_bridge_id()
    napi.set_knob_value(node, "bridge_id", bid)
    napi.set_knob_value(node, "host", settings.get("host") or "127.0.0.1")
    napi.set_knob_value(node, "port", int(settings.get("port") or 8765))
    napi.set_knob_value(node, "comfyui_host", "127.0.0.1")
    napi.set_knob_value(node, "comfyui_port", 8188)
    napi.set_knob_value(node, "output_directory", settings.get("output_directory") or "")
    napi.set_knob_value(node, "prompt", "")
    napi.set_knob_value(node, "mask_source", MASK_SOURCES[0])
    napi.set_knob_value(node, "send_format", SEND_FORMATS[0])
    napi.set_knob_value(node, "send_colorspace", SEND_COLORSPACES[0])
    napi.set_knob_value(node, "create_read_on_result", True)
    napi.set_knob_value(node, "status", "ready")
    napi.set_knob_value(node, "last_result", "")
    return node


def register_node() -> None:
    """Backward-compatible registration helper; menu.py is the real entrypoint."""
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
