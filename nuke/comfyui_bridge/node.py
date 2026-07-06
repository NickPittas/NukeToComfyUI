"""ComfyUIBridge knob layout + creation helpers.

The node itself is a real Nuke gizmo (`nuke/nodes/ComfyUIBridge.gizmo`), so
creation, input attachment, and graph placement are handled natively by Nuke.
This module owns:

  - the canonical user-knob layout (used by the gizmo's onCreate callback and
    by the Group fallback),
  - dynamic per-instance defaults (bridge_id uuid + host/port from settings),
  - a thin `create_bridge_node()` that prefers native `createNode('ComfyUIBridge')`
    and falls back to a Python-built Group if the gizmo is not on pluginPath.

The earlier manual selected-node setInput/xpos/ypos hack has been removed:
Nuke attaches and positions the gizmo itself when created from the Tab menu.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, List, Tuple

from . import napi
from .settings import load_settings


NODE_CLASS_NAME = "ComfyUIBridge"

MASK_SOURCES = (
    "source alpha",
    "invert source alpha",
    "mask input",
    "invert mask input",
)
SEND_FORMATS = ("png8", "exr16")  # Phase 2: half-float EXR transport
VIDEO_FORMATS = ("mov", "mp4")
VIDEO_MOV_CODECS = ("prores_422hq", "prores_4444")


def _new_bridge_id() -> str:
    return "bridge-" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# Knob specs — single source of truth for the user-knob layout.
# --------------------------------------------------------------------------

def _pyscript(nuke: Any, name: str, label: str, command: str) -> Any:
    """Build a PyScript_Knob, or None on Nuke builds without it."""
    Py = getattr(nuke, "PyScript_Knob", None)
    if Py is None:
        return None
    k = Py(name, label)
    k.setValue(command)
    return k


def _knob_specs() -> List[Tuple[str, Callable[[Any], Any]]]:
    """Return [(knob_name, builder(nuke) -> knob | None), ...].

    The builder may return None (e.g. PyScript on a stripped Nuke build); such
    entries are skipped when adding knobs.
    """
    def _file_or_string(nuke: Any, name: str, label: str) -> Any:
        File = getattr(nuke, "File_Knob", None)
        if File is not None:
            return File(name, label)
        return nuke.String_Knob(name, label)

    def _multiline_or_string(nuke: Any, name: str, label: str) -> Any:
        Ml = getattr(nuke, "Multiline_Eval_String_Knob", None)
        if Ml is not None:
            return Ml(name, label)
        return nuke.String_Knob(name, label)

    return [
        ("ComfyUI", lambda n: n.Tab_Knob("ComfyUI")),
        ("bridge_id", lambda n: n.String_Knob("bridge_id", "bridge id")),
        ("host", lambda n: n.String_Knob("host", "host")),
        ("port", lambda n: n.Int_Knob("port", "port")),
        ("comfyui_host", lambda n: n.String_Knob("comfyui_host", "ComfyUI host")),
        ("comfyui_port", lambda n: n.Int_Knob("comfyui_port", "ComfyUI port")),
        ("output_directory", lambda n: _file_or_string(n, "output_directory", "output directory")),
        ("prompt", lambda n: _multiline_or_string(n, "prompt", "prompt")),
        ("mask_source", lambda n: n.Enumeration_Knob("mask_source", "mask_source", list(MASK_SOURCES))),
        ("send_format", lambda n: n.Enumeration_Knob("send_format", "send_format", list(SEND_FORMATS))),
        ("send_colorspace", lambda n: n.Enumeration_Knob("send_colorspace", "send_colorspace", [])),
        (
            "refresh_colorspaces",
            lambda n: _pyscript(
                n, "refresh_colorspaces", "Refresh colorspaces",
                "from comfyui_bridge import node; "
                "node.refresh_colorspace_choices(nuke.thisNode())",
            ),
        ),
        ("workflow_choices", lambda n: n.Enumeration_Knob("workflow_choices", "workflow", ["(none)"])),
        ("create_read_on_result", lambda n: n.Boolean_Knob("create_read_on_result", "create_read_on_result")),
        ("status", lambda n: n.String_Knob("status", "status")),
        ("last_result", lambda n: n.String_Knob("last_result", "last_result")),
        (
            "save_defaults",
            lambda n: _pyscript(
                n, "save_defaults", "Save defaults",
                "from comfyui_bridge import node_settings; "
                "node_settings.save_defaults_from_node(nuke.thisNode())",
            ),
        ),
        (
            "clear_frame_cache",
            lambda n: _pyscript(
                n, "clear_frame_cache", "Clear frame cache",
                "from comfyui_bridge import render; "
                "render.clear_cache_from_node(nuke.thisNode())",
            ),
        ),
        (
            "refresh_workflows",
            lambda n: _pyscript(
                n, "refresh_workflows", "Refresh workflows",
                "from comfyui_bridge import workflow_selection; "
                "workflow_selection.refresh_workflow_choices(nuke.thisNode())",
            ),
        ),
        (
            "run_selected_workflow",
            lambda n: _pyscript(
                n, "run_selected_workflow", "Run selected workflow",
                "from comfyui_bridge import workflow_selection; "
                "workflow_selection.run_selected_workflow(nuke.thisNode())",
            ),
        ),
        ("Video", lambda n: n.Tab_Knob("Video")),
        ("video_format", lambda n: n.Enumeration_Knob("video_format", "video_format", list(VIDEO_FORMATS))),
        ("video_mov_codec", lambda n: n.Enumeration_Knob("video_mov_codec", "mov codec", list(VIDEO_MOV_CODECS))),
        ("video_first", lambda n: n.Int_Knob("video_first", "first")),
        ("video_last", lambda n: n.Int_Knob("video_last", "last")),
        ("video_fps", lambda n: n.Double_Knob("video_fps", "fps")),
        ("video_colorspace", lambda n: n.Enumeration_Knob("video_colorspace", "video colorspace", [])),
        (
            "refresh_video_colorspaces",
            lambda n: _pyscript(
                n, "refresh_video_colorspaces", "Refresh video colorspaces",
                "from comfyui_bridge import node; "
                "node.refresh_video_colorspace_choices(nuke.thisNode())",
            ),
        ),
    ]


def _append_all_knobs(group_node: Any, nuke: Any) -> None:
    """Add every knob from the spec (used for the Group fallback)."""
    for _name, builder in _knob_specs():
        knob = builder(nuke)
        if knob is not None:
            group_node.addKnob(knob)


def build_knobs(group_node: Any) -> None:
    """Public wrapper: add all user knobs on the main thread."""
    nuke: Any = napi._nuke
    napi.call(_append_all_knobs, group_node, nuke)


def ensure_knobs(node: Any) -> None:
    """Add any knob from the spec that is missing on `node`.

    Runs on Nuke's main thread (called from the onCreate callback). The gizmo
    does not contain static addUserKnob entries; Python creates the real knob
    types here.
    """
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    for name, builder in _knob_specs():
        try:
            if node.knob(name) is None:
                knob = builder(nuke)
                if knob is not None:
                    node.addKnob(knob)
        except Exception:
            # Never let one bad knob break node creation.
            pass


def write_colorspaces(nuke: Any) -> List[str]:
    """Return project-provided Nuke colorspaces; never invent names."""
    write = None
    temp_name = "_ComfyUIBridge_colorspace_probe_" + uuid.uuid4().hex[:8]
    root = None
    try:
        root = nuke.root()
        try:
            root.begin()
        except Exception:
            pass
        write = nuke.createNode("Write", "", inpanel=False)
        try:
            write.setName(temp_name)
        except Exception:
            pass
        knob = write.knob("colorspace")
        return _dedupe([str(v) for v in list(knob.values()) if str(v)])
    except Exception:
        pass
    finally:
        if write is not None:
            try:
                nuke.delete(write)
            except Exception:
                pass
        try:
            leaked = nuke.toNode(temp_name)
            if leaked is not None:
                nuke.delete(leaked)
        except Exception:
            pass
        if root is not None:
            try:
                root.end()
            except Exception:
                pass

    return []


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def refresh_colorspace_choices(node: Any) -> None:
    """Keep the bridge dropdown aligned with the current Nuke OCIO config."""
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        k = node.knob("send_colorspace")
        if k is None:
            return
        current = str(k.value() or "")
        values = write_colorspaces(nuke)
        if not values:
            _safe_set(node, "status", "colorspace refresh failed")
            return
        k.setValues(values)
        if current in values:
            k.setValue(current)
        else:
            _safe_set(node, "status", f"{len(values)} colorspace(s); none selected")
            return
        _safe_set(node, "status", f"{len(values)} colorspace(s)")
    except Exception:
        pass


def refresh_video_colorspace_choices(node: Any) -> None:
    refresh_colorspace_choices_for_knob(node, "video_colorspace")


def refresh_colorspace_choices_for_knob(node: Any, knob_name: str) -> None:
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        k = node.knob(knob_name)
        if k is None:
            return
        current = str(k.value() or "")
        values = write_colorspaces(nuke)
        if not values:
            _safe_set(node, "status", "colorspace refresh failed")
            return
        k.setValues(values)
        if current in values:
            k.setValue(current)
        else:
            _safe_set(node, "status", f"{len(values)} colorspace(s); none selected")
            return
        _safe_set(node, "status", f"{len(values)} colorspace(s)")
    except Exception:
        pass


def initialize_defaults(node: Any) -> None:
    """Fill empty dynamic defaults: bridge_id, host/port, output_directory,
    comfyui host/port, create_read_on_result, status.

    Idempotent: only fills when the current value is empty/zero, so reloading a
    saved script keeps the user's values intact.
    """
    if not napi.has_nuke():
        return
    settings = load_settings()

    def _str(name: str, value: str) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            cur = k.value()
            if cur is None or str(cur).strip() == "":
                k.setValue(value)
        except Exception:
            pass

    def _int(name: str, value: int) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            if not k.value():
                k.setValue(int(value))
        except Exception:
            pass

    def _bool(name: str, value: bool) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            k.setValue(bool(value))
        except Exception:
            pass

    _str("bridge_id", _new_bridge_id())
    _str("host", str(settings.get("host") or "127.0.0.1"))
    _int("port", int(settings.get("port") or 8765))
    _str("comfyui_host", "127.0.0.1")
    _int("comfyui_port", 8188)
    _str("output_directory", str(settings.get("output_directory") or ""))
    _bool("create_read_on_result", True)
    _str("status", "ready")
    try:
        root = napi._nuke.root()
        _int("video_first", int(root.firstFrame()))
        _int("video_last", int(root.lastFrame()))
        _int("video_fps", int(root.fps()))
    except Exception:
        pass


# --------------------------------------------------------------------------
# Node creation
# --------------------------------------------------------------------------

def create_bridge_node() -> Any:
    """Create a ComfyUIBridge node natively and return it.

    Prefers `nuke.createNode('ComfyUIBridge')` (the gizmo), which lets Nuke
    attach to the selected node and place the node itself — no manual
    setInput/xpos/ypos. Falls back to a Python-built Group if the gizmo is not
    on pluginPath (e.g. the user only added the package, not the plugin root).
    """
    if not napi.has_nuke():
        raise napi.NukeError("nuke module is not available (running outside Nuke)")
    nuke: Any = napi._nuke

    def _create() -> Any:
        try:
            return nuke.createNode(NODE_CLASS_NAME)
        except Exception:
            return _create_group_fallback(nuke)

    return napi.call(_create)


def _create_group_fallback(nuke: Any) -> Any:
    """Build a Group with the same knob layout/defaults when no gizmo is found.

    No manual input attachment or positioning — Nuke places the new node.
    """
    node = nuke.createNode("Group", inpanel=False)
    node.setName(NODE_CLASS_NAME)
    try:
        node["tile_color"].setValue(int("355F8CFF", 16))
    except Exception:
        pass

    # Real passthrough: external input 0 -> internal Output. Second Input
    # exposes the optional mask pipe; it is not wired to the output.
    node.begin()
    try:
        src = nuke.createNode("Input", inpanel=False)
        src.setName("source")
        out = nuke.createNode("Output", inpanel=False)
        out.setInput(0, src)
        mask = nuke.createNode("Input", inpanel=False)
        mask.setName("mask")
    finally:
        node.end()

    _append_all_knobs(node, nuke)
    initialize_defaults(node)
    # Selection of enum defaults matches the gizmo.
    for name, value in (
        ("mask_source", MASK_SOURCES[0]),
        ("send_format", SEND_FORMATS[0]),
        ("workflow_choices", "(none)"),
    ):
        try:
            node.knob(name).setValue(0)
        except Exception:
            pass
    _safe_set(node, "prompt", "")
    _safe_set(node, "last_result", "")
    return node


def _safe_set(node: Any, name: str, value: Any) -> None:
    try:
        k = node.knob(name)
        if k is not None:
            k.setValue(value)
    except Exception:
        pass


def register_node() -> None:
    """Backward-compatible registration helper; menu.py is the real entrypoint."""
    if not napi.has_nuke():
        return
    nuke: Any = napi._nuke
    try:
        nuke.menu("Nodes").addCommand(
            "ComfyUI/ComfyUIBridge",
            lambda: create_bridge_node(),
            icon="ComfyUIBridge.png",
        )
    except Exception:
        # Menu may not be ready in all entry points; non-fatal.
        pass
